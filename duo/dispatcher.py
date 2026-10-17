"""Dispatcher (指派员): owns the two Operators, the durable submit gate, the clock, health and policy.

Runs on the GUI thread as a ``QObject``. Operator events arrive through queued signal
connections, are normalised into brief §4 dicts, written to the masked audit timeline
(``data/timeline.jsonl``) and re-emitted on ``event``. Persistent dispatcher state lives in
``data/dispatcher.json``; the order gate (policy / limit / submit claims) stays in
``OrderCoordinator`` (``data/order-coordination.json``) so 1.x records remain valid.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from . import events, stealth
from .model import CHINA, DUO_TARGET, FULFILLMENTS, Config, MissionPlan, mask
from .operator import Operator
from .orders import MODES, POLICIES, OrderCoordinator
from .storage import load_config, save_config

OPERATORS = ["pickup", "delivery"]
HEARTBEAT_LIMIT_S = 15.0
CLOCK_URL = "https://www.apple.com.cn/"
CLOCK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
START_COMMANDS = ("prepare", "arm", "now", "trial", "trial-retry", "recover", "reset-record", "login-prepare")
PENDING_DONE_STATES = ("准备就绪", "需要核对", "需要处理", "账户已登录")
BIG_FIELDS = ("text",)


def measure_clock_offset(url: str = CLOCK_URL, samples: int = 3, timeout: float = 4.0, opener=None) -> tuple[int | None, str]:
    """Median of (server Date − local midpoint) over ``samples`` HEAD requests, in ms (brief §8.2)."""
    offsets = []
    opener = opener or urllib.request.urlopen
    for _ in range(samples):
        try:
            request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": CLOCK_UA, "Cache-Control": "no-cache"})
            started = time.time()
            with opener(request, timeout=timeout) as response:
                finished = time.time()
                header = response.headers.get("Date")
            if not header:
                continue
            server = parsedate_to_datetime(header)
            if server.tzinfo is None:
                server = server.replace(tzinfo=timezone.utc)
            # The Date header is truncated to whole seconds: centre the estimate.
            offsets.append((server.timestamp() + 0.5 - (started + finished) / 2) * 1000)
        except Exception:
            continue
    if not offsets:
        return None, "校准失败，使用本机时钟"
    return int(round(statistics.median(offsets))), f"www.apple.com.cn Date 头 ×{len(offsets)} 中位数"


class Dispatcher(QObject):
    """Brief §3 public API. Every method is safe to call from the GUI thread; none blocks on the network."""

    event = Signal(dict)
    _clock_result = Signal(object)

    def __init__(self, data_root: Path, testing: bool = False, operator_factory=None, start_threads: bool = True,
                 health_interval_ms: int = 1000):
        super().__init__()
        self.root = Path(data_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.testing = testing
        self.coordinator = OrderCoordinator(self.root)
        self.state_path = self.root / "dispatcher.json"
        self.timeline_path = self.root / "timeline.jsonl"
        self._state = "idle"
        self._persisted = self._load_state()
        self._browser_mode = self._persisted.get("browser_mode", "auto")
        if self._browser_mode not in stealth.POLICIES:
            self._browser_mode = "auto"
        self._clock_offset_ms = None
        self._clock_source = ""
        self._clock_thread = None
        self._error = ""
        self._mode = self.coordinator.status().get("mode", "parallel")
        self.configs: dict[str, Config] = {}
        self.config_errors: dict[str, str] = {}
        self.ops: dict[str, Operator] = {}
        self.pending: dict[str, bool] = {op: False for op in OPERATORS}
        self.pending_command: dict[str, str] = {op: "" for op in OPERATORS}
        self.running: dict[str, bool] = {op: False for op in OPERATORS}
        self._after_prepare = ""
        self.healthy: dict[str, bool] = {op: True for op in OPERATORS}
        self.restarts: dict[str, int] = {op: 0 for op in OPERATORS}
        self.restart_stage: dict[str, int] = {op: 0 for op in OPERATORS}
        self.cap_reported: dict[str, bool] = {op: False for op in OPERATORS}
        self.last_restart_at: dict[str, float] = {op: 0.0 for op in OPERATORS}
        self.downgraded: dict[str, bool] = {op: False for op in OPERATORS}
        self.operator_mode: dict[str, str] = {}
        self.sequential_queue: list[str] = []
        self.login_required: set[str] = set()
        self._shutdown = False
        factory = operator_factory or self._make_operator
        for op in OPERATORS:
            folder = self.operator_dir(op)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                cfg = load_config(folder / "settings.dat") if (folder / "settings.dat").exists() else Config(fulfillment=op)
            except Exception:
                cfg = Config(fulfillment=op)
                self.config_errors[op] = "本地设置无法读取，已使用预设信息。请检查联系信息。"
            if cfg.fulfillment != op:
                # A settings file is per route; never inherit the other route's preset phone.
                cfg = Config.from_dict({**cfg.to_dict(), "fulfillment": op,
                                        "phone": cfg.phone if (folder / "settings.dat").exists() else ""})
            cfg.fulfillment = op
            self.configs[op] = cfg
            self.operator_mode[op] = stealth.resolve_mode(self._browser_mode)
            operator = factory(folder, op, self.coordinator, self.operator_mode[op])
            operator.event.connect(self._on_operator_event, Qt.ConnectionType.QueuedConnection)
            self.ops[op] = operator
            if start_threads:
                operator.start()
        self._clock_result.connect(self._apply_clock, Qt.ConnectionType.QueuedConnection)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.health_check)
        if health_interval_ms > 0:
            self.health_timer.start(health_interval_ms)
        self._persist()

    # ------------------------------------------------------------------ construction helpers
    def _make_operator(self, folder, op, coordinator, mode):
        return Operator(folder, op, coordinator, name=op, browser_mode=mode)

    def operator_dir(self, op: str) -> Path:
        # Keep the 1.x layout so existing journals stay valid: pickup = root, delivery = root/delivery.
        return self.root if op == "pickup" else self.root / op

    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
        except (OSError, ValueError):
            pass
        return {}

    def _persist(self):
        value = {"schema": 1, "browser_mode": self._browser_mode, "policy": self.coordinator.status()["policy"],
                 "mode": self._mode, "clock_offset_ms": self._clock_offset_ms, "clock_source": self._clock_source,
                 "state": self._state, "restarts": dict(self.restarts), "downgraded": dict(self.downgraded),
                 "updated": datetime.now(CHINA).isoformat()}
        try:
            temp = self.state_path.with_suffix(".tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            pass

    def _audit(self, value: dict):
        record = {}
        for key, item in value.items():
            if key in BIG_FIELDS and isinstance(item, str):
                item = item[:200]
            if isinstance(item, str):
                item = mask(item, self.configs.get(value.get("operator") or "pickup"))
            record[key] = item
        try:
            with self.timeline_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _emit(self, value: dict):
        self._audit(value)
        self.event.emit(value)

    # ------------------------------------------------------------------ §3 state
    def status(self) -> dict:
        gate = self.coordinator.status()
        operators = {}
        for op, operator in self.ops.items():
            info = operator.status()
            info.update(pending=self.pending[op], healthy=self.healthy[op], restarts=self.restarts[op],
                        config_error=self.config_errors.get(op, ""))
            operators[op] = info
        return {"state": self._state, "policy": gate["policy"], "single": gate["single"], "limit": gate.get("limit"),
                "attempts": gate["attempts"], "owner": gate.get("owner"), "mode": self._mode,
                "error": gate["error"] or self._error, "browser_mode": self._browser_mode,
                "clock_offset_ms": self._clock_offset_ms, "clock_source": self._clock_source,
                "operators": operators}

    def operators(self) -> list[str]:
        return list(OPERATORS)

    def config(self, operator: str) -> Config:
        return self.configs[self._check_operator(operator)]

    def save_config(self, operator: str, cfg: Config) -> None:
        operator = self._check_operator(operator)
        cfg = cfg.copy()
        cfg.fulfillment = operator
        cfg.validate()
        if not self.testing:
            save_config(self.operator_dir(operator) / "settings.dat", cfg)
        self.configs[operator] = cfg
        self.config_errors.pop(operator, None)

    def plan(self) -> MissionPlan:
        pickup = self.configs["pickup"]
        return MissionPlan(starts_at=pickup.starts_at, target=DUO_TARGET, policy=self.coordinator.status()["policy"],
                           operators={op: cfg.copy() for op, cfg in self.configs.items()},
                           browser_mode=self._browser_mode, clock_offset_ms=self._clock_offset_ms,
                           prewarm_minutes=pickup.effective_prewarm_minutes)

    def set_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError("未知的订单策略。")
        if self.is_busy():
            raise ValueError("任务进行中不能切换下单策略，请先停止全部。")
        self.coordinator.set_policy(policy)
        self._persist()
        self._emit_policy("已切换为“只下一单，两路竞速”。" if policy == "single" else "已切换为“争取两单；识别限购后只下一单”。")

    def set_browser_mode(self, mode: str) -> None:
        if mode not in stealth.POLICIES:
            raise ValueError("未知的浏览器模式。")
        self._browser_mode = mode
        self._persist()
        for op, operator in self.ops.items():
            resolved = stealth.resolve_mode(mode, downgraded=self.downgraded[op] and mode == "auto")
            if resolved != self.operator_mode[op]:
                self.operator_mode[op] = resolved
                operator.send("mode", resolved)
                self._emit(events.dispatcher_browser_mode(op, resolved, "用户选择：" + {"auto": "自动", "headless": "无头", "hidden": "隐藏窗口"}[mode]))

    # ------------------------------------------------------------------ §3 global commands
    def prepare(self) -> None:
        self._set_state("preparing", "正在预热两路浏览器并校准官网时钟…")
        self.sync_clock()
        for op in self._idle_operators():
            self._send_start(op, "prepare")

    def arm(self) -> None:
        self.sync_clock()
        # "prepare" then "arm" back-to-back is the UI's one-click flow: the operator processes its
        # command queue in order, so arming may be queued behind a pending prepare.
        for op in self._idle_operators(allow_after_prepare=True):
            self._send_start(op, "arm")
        if self._state != "preparing":
            self._set_state("armed", "已布防：按校准后的官网时间自动开始，提前预热会话，T-2 秒入口竞速。")
        else:
            self._after_prepare = "armed"

    def start_now(self) -> None:
        for op in self._idle_operators():
            self._send_start(op, "now")
        self._set_state("running", "两路立即开始正式任务。")

    def trial(self) -> None:
        for op in self._idle_operators():
            self._send_start(op, "trial")
        if all(self.configs[op].auto_submit for op in self.ops):
            self._set_state("running", "官网试跑（iPhone Air）：两路同时运行，核对通过后自动提交测试订单（不付款会被官网自动取消）。")
        else:
            self._set_state("running", "官网试跑（iPhone Air）：两路同时运行；未开启自动提交的一路会在核对页等你点提交。")

    def stop_all(self) -> None:
        if not self.is_busy():
            self._set_state("idle", "没有进行中的任务。")
            return
        self._set_state("stopping", "正在停止两路任务…")
        for op, operator in self.ops.items():
            if self.running[op] or self.pending[op]:
                operator.send("stop")
            self.pending[op] = False
            self.pending_command[op] = ""
        QTimer.singleShot(1500, self._settle_state)

    # ------------------------------------------------------------------ §3 per-operator commands
    def command(self, operator: str, name: str, payload=None) -> None:
        op = self._check_operator(operator)
        target = self.ops[op]
        if name in START_COMMANDS:
            queued_after_prepare = name == "arm" and self.pending[op] and self.pending_command[op] == "prepare"
            if (self.running[op] or self.pending[op]) and not queued_after_prepare:
                raise ValueError(FULFILLMENTS[op] + "正在运行或准备中。")
            if name == "prepare":
                if self._state == "idle":
                    self._set_state("preparing", FULFILLMENTS[op] + "正在预热浏览器…")
                self.sync_clock()
            elif name == "arm":
                if self._state in ("idle", "preparing"):
                    self._set_state("armed", FULFILLMENTS[op] + "已布防。")
            elif name in ("now", "trial", "trial-retry", "recover"):
                self._set_state("running", FULFILLMENTS[op] + ("开始官网试跑。" if name.startswith("trial") else "开始任务。"))
            self._send_start(op, name)
            return
        if name == "otp":
            payload = payload if isinstance(payload, dict) else {"request": "", "code": ""}
            target.send("otp", {"request": payload.get("request", ""), "code": payload.get("code") or ""})
            return
        if name == "consent":
            if isinstance(payload, dict):
                target.send("consent", {"request": payload.get("request", ""), "accept": bool(payload.get("accept", True))})
            else:
                target.send("consent", {"request": payload, "accept": True})
            return
        if name == "restart":
            payload = payload if isinstance(payload, dict) else {"hard": True}
            self.restarts[op] += 1
            self._emit(events.dispatcher_health(op, False, payload.get("reason") or "用户要求重启", "hard-restart" if payload.get("hard", True) else "soft-restart"))
            target.interrupt()
            target.send("restart", {"hard": bool(payload.get("hard", True)), "mode": payload.get("mode"), "reason": payload.get("reason", "用户要求")})
            return
        if name == "login":
            credentials = payload if isinstance(payload, dict) else {}
            target.send("login", {"apple_id": str(credentials.get("apple_id", "")), "password": str(credentials.get("password", ""))})
            return
        if name in ("pause", "resume", "submit", "stop", "show", "show-window", "hide-window", "login-check", "check-dispatcher"):
            target.send(name, payload)
            if name == "stop":
                self.pending[op] = False
                self.pending_command[op] = ""
                QTimer.singleShot(1500, self._settle_state)
            return
        raise ValueError("未知的操作员命令：" + str(name))

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.health_timer.stop()
        for operator in self.ops.values():
            operator.send("shutdown")
        deadline = time.monotonic() + 5.0
        for operator in self.ops.values():
            remaining = max(0.1, deadline - time.monotonic())
            if operator.isRunning():
                operator.wait(int(remaining * 1000))
        self._state = "idle"
        self._persist()

    def is_busy(self) -> bool:
        return any(self.running[op] or self.pending[op] for op in OPERATORS)

    def set_trial_options(self, sms: bool = True, auto_consent: bool = False) -> None:
        """Trial-only behaviour (brief §10.5): unattended `--live-trial` runs stop before the SMS and may
        auto-accept the privacy consent. Formal runs are unaffected."""
        for operator in self.ops.values():
            operator.trial_sms_allowed = bool(sms)
            operator.trial_auto_consent = bool(auto_consent)
        self._audit({"type": "dispatcher.trial-options", "operator": None, "time": datetime.now(CHINA).isoformat(),
                     "sms": bool(sms), "auto_consent": bool(auto_consent)})

    # ------------------------------------------------------------------ clock (§8.2)
    def sync_clock(self) -> None:
        if not any(cfg.clock_sync for cfg in self.configs.values()):
            return
        if self._clock_thread and self._clock_thread.is_alive():
            return

        def work():
            offset, source = measure_clock_offset()
            self._clock_result.emit((offset, source))

        self._clock_thread = threading.Thread(target=work, name="duo-clock-sync", daemon=True)
        self._clock_thread.start()

    def _apply_clock(self, result):
        offset, source = result
        self._clock_offset_ms, self._clock_source = offset, source
        for operator in self.ops.values():
            operator.send("clock", offset or 0)
        self._persist()
        self._emit(events.dispatcher_clock(offset if offset is not None else 0, source) if offset is not None
                   else events.event(events.DISPATCHER_CLOCK, None, offset_ms=None, source=source))

    # ------------------------------------------------------------------ health (§8.4)
    def health_check(self) -> None:
        if self._shutdown:
            return
        now = time.monotonic()
        for op, operator in self.ops.items():
            busy = self.running[op] or self.pending[op]
            age = now - operator.heartbeat
            if not busy or (not operator.isRunning() and not self.testing):
                continue
            if age <= HEARTBEAT_LIMIT_S:
                if not self.healthy[op]:
                    self.healthy[op] = True
                    self.restart_stage[op] = 0
                    self.cap_reported[op] = False
                    self._emit(events.dispatcher_health(op, True, "", "none"))
                continue
            if now - self.last_restart_at[op] < HEARTBEAT_LIMIT_S:
                continue
            cfg = self.configs[op]
            if self.restarts[op] >= cfg.max_restarts:
                # Report the exhausted budget exactly once, even though the operator was already
                # marked unhealthy by the restart that preceded this check.
                if not self.cap_reported[op]:
                    self.cap_reported[op] = True
                    self.healthy[op] = False
                    self._emit(events.dispatcher_health(op, False, f"{age:.0f} 秒无心跳，且自动重启已达上限 {cfg.max_restarts} 次", "none"))
                    self._emit(events.operator_attention(op, FULFILLMENTS[op] + "持续无响应，自动重启已达上限。",
                                                         "请点“停止”后用“恢复购物袋”继续，或重启软件（防重复记录会保留）。", False))
                continue
            hard = self.restart_stage[op] >= 1
            self.healthy[op] = False
            self.restarts[op] += 1
            self.restart_stage[op] += 1
            self.last_restart_at[op] = now
            action = "hard-restart" if hard else "soft-restart"
            self._emit(events.dispatcher_health(op, False, f"{age:.0f} 秒无心跳", action))
            operator.interrupt()
            operator.send("restart", {"hard": hard, "reason": f"健康监控：{age:.0f} 秒无心跳"})
            self._persist()

    # ------------------------------------------------------------------ operator events
    def _on_operator_event(self, value: dict):
        if not isinstance(value, dict):
            return
        kind = value.get("type", "")
        op = value.get("operator")
        if kind == events.INTERNAL_COORDINATION:
            self._on_coordination(op, value)
            return
        if kind == events.INTERNAL_LOGIN_REQUIRED:
            self._on_login_required(op, value)
            return
        if kind in (events.INTERNAL_BROWSER, events.INTERNAL_PREWARMED, events.INTERNAL_SHIELD):
            self._audit(value)
            return
        if op in self.ops:
            if kind == events.OPERATOR_RUN:
                self.running[op], self.pending[op] = True, False
                self.pending_command[op] = ""
                waiting = not value.get("trial") and self._target_in_future(value.get("target"))
                if waiting and self._state in ("idle", "preparing"):
                    self._set_state("armed", "")
                elif not waiting and self._state != "running":
                    self._set_state("running", "")
            elif kind == events.OPERATOR_STATE:
                if value.get("state") in PENDING_DONE_STATES and self.pending_command[op] in ("prepare", "reset-record", "login-prepare", ""):
                    self.pending[op] = False
                if self._state == "armed" and value.get("phase") not in ("idle", "waiting") and self.running[op]:
                    self._set_state("running", "开售时间到，两路开始入口竞速。")
            elif kind in (events.OPERATOR_FINISHED, events.OPERATOR_ERROR):
                reason = value.get("reason")
                if reason in ("prepared", "login-required", "reset-record"):
                    # Preparation finished; a queued "arm" (one-click flow) may still be pending.
                    self.pending[op] = self.pending_command[op] == "arm"
                    if not self.pending[op]:
                        self.pending_command[op] = ""
                    QTimer.singleShot(0, self._after_prepare_done)
                else:
                    self.running[op], self.pending[op] = False, False
                    self.pending_command[op] = ""
                    if self._mode == "sequential-account" and value.get("success") and kind == events.OPERATOR_FINISHED:
                        QTimer.singleShot(0, self._advance_sequential)
                QTimer.singleShot(0, self._settle_state)
            elif kind == events.OPERATOR_NETWORK:
                self._on_network(op, value)
        self._emit(value)

    @staticmethod
    def _target_in_future(target) -> bool:
        try:
            return datetime.fromisoformat(target) - datetime.now(CHINA) > timedelta(seconds=5)
        except (TypeError, ValueError):
            return False

    def _on_coordination(self, op, value):
        status = value.get("status") or self.coordinator.status()
        self._emit_policy("" if not status.get("limit") else "已识别官网限购：“" + status["limit"].get("evidence", "") + "”；两路竞速、只提交一单。")
        for other, operator in self.ops.items():
            if other != op and self.running[other]:
                operator.send("check-dispatcher")

    def _on_login_required(self, op, value):
        self._audit(value)
        cfg = self.configs[op]
        other = next(o for o in OPERATORS if o != op)
        self.login_required.add(op)
        # Each route has its own browser profile: one route signing in while the other checks out as a
        # guest uses two different bags, so both keep running in parallel. Only when both routes need
        # the same Apple ID (one shared bag) must they run one after the other (§13.2).
        if self.configs[other].checkout_mode == "guest" and other not in self.login_required:
            self._emit_policy(FULFILLMENTS[op] + "需要登录 Apple 账户；" + FULFILLMENTS[other] +
                              "仍以游客身份并行，互不影响。请在" + FULFILLMENTS[op] + "卡片点“登录账户”。")
            return
        if cfg.checkout_mode == "guest" and self._mode != "sequential-account":
            # Guest checkout gone: one Apple ID means one bag, so the two routes must run one after the other (§13.2).
            if self.coordinator.set_mode("sequential-account"):
                self._mode = "sequential-account"
                self._persist()
                self.sequential_queue = [op]          # this route waits until the other one has finished
                self._emit_policy("官网要求登录结账；已切换为同账号顺序下单：先让" + FULFILLMENTS[other] +
                                  "完成提交，再用同一账户为" + FULFILLMENTS[op] + "建单。")

    def _on_network(self, op, value):
        status = value.get("status")
        if status not in (403, 541):
            return
        cfg = self.configs[op]
        # §8.0: 541 is handled by the operator's shield re-challenge. Only after that path is exhausted
        # (the operator reports itself blocked) and only in "auto" with the fallback enabled do we
        # downgrade this operator to the hidden-headed browser, keeping the same profile and journal.
        operator = self.ops[op]
        if self._browser_mode != "auto" or not cfg.headless_fallback or self.downgraded[op]:
            return
        if operator.shield_attempts < 6 and operator.consecutive_blocks < 4:
            return
        self.downgraded[op] = True
        self.operator_mode[op] = "hidden"
        self._persist()
        self._emit(events.dispatcher_browser_mode(op, "hidden", f"安全盾重新握手仍返回 HTTP {status}：以同一 profile 切换为隐藏窗口的有头浏览器"))
        operator.send("restart", {"hard": True, "mode": "hidden", "reason": f"HTTP {status} 持续"})

    # ------------------------------------------------------------------ internals
    def _check_operator(self, operator: str) -> str:
        if operator not in self.ops:
            raise ValueError("未知的操作员：" + str(operator))
        return operator

    def _idle_operators(self, allow_after_prepare: bool = False) -> list[str]:
        return [op for op in OPERATORS if not self.running[op] and
                (not self.pending[op] or (allow_after_prepare and self.pending_command[op] == "prepare"))]

    def _send_start(self, op: str, name: str):
        cfg = self.configs[op].copy()
        cfg.validate()
        self.pending[op] = True
        self.pending_command[op] = name
        self.ops[op].send(name, cfg)

    def _operator_phase(self, op: str) -> str:
        try:
            return self.ops[op].phase
        except Exception:
            return "idle"

    def _set_state(self, state: str, message: str):
        if state not in events.DISPATCHER_STATES:
            return
        changed = state != self._state
        self._state = state
        self._persist()
        if changed or message:
            self._emit(events.dispatcher_state(state, message))

    def _after_prepare_done(self):
        if self._state != "preparing":
            return
        if any(self.pending_command[op] == "prepare" and self.pending[op] for op in OPERATORS):
            return
        if self._after_prepare == "armed" or any(self.running.values()) or any(self.pending_command[op] == "arm" for op in OPERATORS):
            self._after_prepare = ""
            self._set_state("armed", "预热完成：两路已布防，到点自动开始。")
        else:
            self._set_state("idle", "预热完成：浏览器与会话已就绪。")

    def _settle_state(self):
        if self._shutdown:
            return
        if not self.is_busy():
            if self._state != "idle":
                self._set_state("idle", "全部操作员已停止。" if self._state == "stopping" else "任务结束。")
        elif self._state == "stopping":
            QTimer.singleShot(1000, self._settle_state)

    def _advance_sequential(self):
        """Sequential-account mode (§13.2): once the first route has finished, hand the second route to the
        user — it must sign in with the same Apple ID (its bag is now empty) before it re-adds the item."""
        if self._mode != "sequential-account" or not self.sequential_queue:
            return
        op = self.sequential_queue.pop(0)
        if self.ops[op].submitted:
            return
        self._emit_policy(FULFILLMENTS[op] + "：前一路已完成，现在可用同一账户为本路建单。")
        self._emit(events.operator_attention(op, "前一路已完成提交。请在本路点“登录账户”（同一 Apple ID），然后点“继续”重新加购。",
                                             "同一账号顺序下单：登录 → 继续 → 重新加购 → 核对 → 提交。", False))

    def _emit_policy(self, message: str):
        gate = self.coordinator.status()
        self._emit(events.dispatcher_policy(gate["policy"], gate["single"], gate.get("limit"), gate.get("owner"), message, self._mode))

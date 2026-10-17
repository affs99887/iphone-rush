"""ScriptedDispatcher: a fake dispatcher for UI development, screenshots and UI tests ONLY.

It implements the §3 Dispatcher API and replays §4 events on a QTimer. It never opens a browser and
never touches Apple. It must not be used to claim that the purchase flow works.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from duo.model import DUO_TARGET, FULFILLMENTS, Config, ProductTarget

try:  # the engine's MissionPlan when available (Worker A), else a small local stand-in
    from duo.model import MissionPlan as _MissionPlan
except ImportError:  # pragma: no cover
    _MissionPlan = None

CHINA = timezone(timedelta(hours=8))
POLICIES = ("adaptive", "single")
BROWSER_MODES = ("auto", "headless", "hidden")
OPERATORS = ["pickup", "delivery"]


@dataclass
class StubPlan:
    starts_at: datetime
    target: ProductTarget = DUO_TARGET
    policy: str = "adaptive"
    operators: dict = field(default_factory=dict)


def _operator_status(op: str) -> dict:
    return {"state": "等待开始", "detail": "尚未布防", "phase": "idle", "running": False, "pending": False,
            "paused": False, "trial": False, "submitted": False, "review_ok": False, "healthy": True,
            "browser_mode": "headless", "lanes": {}, "refreshes": 0, "store": "", "checks": {}, "last_image": "",
            "timeline_ms": {}, "logged_in": False}


def _redact(payload):
    """Never keep a password or verification code anywhere, not even in the stub's command log."""
    if isinstance(payload, dict) and ("password" in payload or "code" in payload):
        return {k: ("***" if k in ("password", "code") and v else v) for k, v in payload.items()}
    return payload


class ScriptedDispatcher(QObject):
    """Development double for duo.dispatcher.Dispatcher (same public surface, scripted behaviour)."""

    event = Signal(dict)

    def __init__(self, data_root: Path | str | None = None, testing: bool = True, interval_ms: int = 350):
        super().__init__()
        self.data_root = Path(data_root) if data_root else None
        self.testing = testing
        self.interval_ms = interval_ms
        self._configs = {op: Config(fulfillment=op) for op in OPERATORS}
        self._state = "idle"
        self._policy, self._limit, self._owner = "adaptive", None, None
        self._mode = "parallel"
        self._browser_mode = "auto"
        self._clock_offset: int | None = None
        self._error = ""
        self._ops = {op: _operator_status(op) for op in OPERATORS}
        self._queues: dict[str, list] = {op: [] for op in OPERATORS}
        self._waiting: dict[str, str] = {}          # op -> "otp" | "consent" | "resume" | "submit"
        self._pending_requests: dict[str, str] = {}
        self._attempts: dict[str, dict] = {}
        self.commands: list[tuple[str, str, object]] = []
        self.saved: dict[str, Config] = {}
        self.shutdown_called = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)

    # ---- §3 API --------------------------------------------------------------------------
    def status(self) -> dict:
        single = self._policy == "single" or bool(self._limit)
        return {"state": self._state, "policy": self._policy, "single": single, "limit": self._limit,
                "attempts": dict(self._attempts), "owner": self._owner, "error": self._error, "mode": self._mode,
                "browser_mode": self._browser_mode, "clock_offset_ms": self._clock_offset,
                "operators": {op: dict(info, lanes=dict(info["lanes"]), checks=dict(info["checks"]),
                                       timeline_ms=dict(info["timeline_ms"]))
                              for op, info in self._ops.items()},
                "stub": True}

    def operators(self) -> list[str]:
        return list(OPERATORS)

    def config(self, operator: str) -> Config:
        return self._configs[operator]

    def save_config(self, operator: str, cfg: Config) -> None:
        cfg.validate()
        if cfg.fulfillment != operator:
            raise ValueError("配置的履约方式与操作员不符。")
        self._configs[operator] = cfg
        self.saved[operator] = cfg
        self._log(operator, "设置已保存（演示指派员，不写入磁盘）。")

    def plan(self):
        if _MissionPlan is not None:
            return _MissionPlan(starts_at=self._configs["pickup"].starts_at, policy=self._policy,
                                operators=dict(self._configs), browser_mode=self._browser_mode,
                                clock_offset_ms=self._clock_offset)
        return StubPlan(starts_at=self._configs["pickup"].start_time(), policy=self._policy,
                        operators=dict(self._configs))

    def set_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError("未知的订单策略。")
        if self.is_busy():
            raise ValueError("任务进行中不能切换下单策略，请先停止全部。")
        if self._attempts:
            raise ValueError("已有提交记录，请先核对订单；不能切换策略重新下单。")
        self._policy = policy
        self._emit_policy("已切换为“只下一单，两路竞速”。" if policy == "single" else "已切换为“争取两单；识别限购后只下一单”。")

    def set_browser_mode(self, mode: str) -> None:
        if mode not in BROWSER_MODES:
            raise ValueError("未知的浏览器模式。")
        self._browser_mode = mode
        for op in OPERATORS:
            if not self._ops[op]["running"]:
                self._ops[op]["browser_mode"] = "hidden" if mode == "hidden" else "headless"
        self._emit({"type": "dispatcher.browser-mode", "operator": None, "mode": mode, "reason": "用户选择"})

    def prepare(self) -> None:
        self._set_state("preparing", "正在启动两路浏览器并校准时钟…")
        for op in OPERATORS:
            if self._ops[op]["running"]:
                continue
            self._ops[op]["pending"] = True
            self._queues[op] += [
                {"type": "operator.state", "operator": op, "state": "准备中", "detail": "启动独立 Chromium 并打开商品页", "phase": "idle"},
                {"type": "operator.log", "operator": op, "message": "浏览器已启动（隐身无头）。", "level": "info"},
                {"type": "operator.state", "operator": op, "state": "准备就绪", "detail": "会话已建立，可随时布防或开始", "phase": "idle"},
                lambda op=op: self._ops[op].__setitem__("pending", False),
            ]
        self._queues["pickup"].insert(1, {"type": "dispatcher.clock", "operator": None, "offset_ms": 137, "source": "www.apple.com.cn Date 头 ×3 中位数"})
        self._queues["pickup"].append(lambda: self._set_state("idle", "预热完成：浏览器与会话已就绪。"))
        self._run()

    def arm(self) -> None:
        self._set_state("armed", "已布防：到点自动开始，开售前 10 分钟预热会话。")
        for op in OPERATORS:
            if self._ops[op]["running"]:
                continue
            target = self._configs[op].start_time()
            self._ops[op].update(running=True, pending=False, trial=False, phase="waiting", state="等待开始")
            self._queues[op] += [
                {"type": "operator.run", "operator": op, "trial": False, "target": target.isoformat()},
                {"type": "operator.state", "operator": op, "state": "等待开始", "phase": "waiting",
                 "detail": f"按校准时钟于 {target.strftime('%m月%d日 %H:%M:%S')} 开始；T-2s 发起入口导航"},
            ]
        self._run()

    def start_now(self) -> None:
        self._set_state("running", "两路立即开始正式任务。")
        for op in OPERATORS:
            if not self._ops[op]["running"] or self._ops[op]["phase"] == "waiting":
                self._queues[op] = list(self._script(op, trial=False))
        self._run()

    def trial(self) -> None:
        self._set_state("running", "官网试跑（iPhone Air）：到提交前强制停止。")
        for op in OPERATORS:
            if not self._ops[op]["running"]:
                self._queues[op] = list(self._script(op, trial=True))
        self._run()

    def stop_all(self) -> None:
        for op in OPERATORS:
            self._stop(op)
        self._set_state("idle", "已停止全部任务；浏览器进度与防重复记录保留。")

    def command(self, operator: str, name: str, payload=None) -> None:
        self.commands.append((operator, name, _redact(payload)))
        info = self._ops[operator]
        if name == "login":
            apple_id = str((payload or {}).get("apple_id") or "")
            password = (payload or {}).get("password") or ""
            if not apple_id or not password:
                self._emit({"type": "operator.attention", "operator": operator, "message": "登录信息不完整。",
                            "suggestion": "点击“登录账户”重新输入。", "review": False})
                return
            del password  # the stub never keeps or logs it
            request = uuid.uuid4().hex
            self._pending_requests[operator] = request
            self._emit({"type": "operator.state", "operator": operator, "state": "登录中", "phase": info["phase"],
                        "detail": "正在后台页面输入 Apple ID 并登录…"})
            self._emit({"type": "operator.log", "operator": operator, "message": "已在后台页面提交 Apple ID 登录（密码不记录）。", "level": "info"})
            self._emit({"type": "operator.otp", "operator": operator, "request": request, "phone_tail": "",
                        "kind": "apple-2fa", "message": "Apple 要求双重认证：请输入受信任设备上显示的 6 位验证码。", "error": False})
            self._waiting[operator] = "otp"
            return
        if name in ("show-window", "hide-window"):
            shown = name == "show-window"
            info["browser_mode"] = "hidden"
            self._emit({"type": "dispatcher.browser-mode", "operator": operator, "mode": "hidden",
                        "reason": "已显示浏览器窗口，供手工处理" if shown else "已重新隐藏浏览器窗口"})
            self._log(operator, "浏览器窗口已显示；手工处理后请点“隐藏浏览器窗口”。" if shown else "浏览器窗口已隐藏，继续后台运行。")
            return
        if name == "pause":
            if info["running"]:
                self._waiting[operator] = "resume"
                self._emit({"type": "operator.state", "operator": operator, "state": "已暂停", "phase": "paused",
                            "detail": "已暂停自动操作；点击“继续”恢复。"})
        elif name == "resume":
            if self._waiting.get(operator) in ("resume", "attention"):
                self._waiting.pop(operator, None)
                self._emit({"type": "operator.state", "operator": operator, "state": "继续执行", "phase": info["phase"],
                            "detail": "已恢复自动操作。"})
                self._run()
        elif name == "submit":
            self._submit(operator)
        elif name == "stop":
            self._stop(operator)
            if not self.is_busy():
                self._set_state("idle", "全部操作员已停止。")
        elif name == "show":
            self._emit({"type": "operator.page", "operator": operator, "url": "https://www.apple.com.cn/shop/bag",
                        "text": "（演示）购物袋：iPhone Duo 星光白色 1TB · AppleCare+ · 数量 1", "image": info.get("last_image", "")})
        elif name == "otp":
            request = (payload or {}).get("request")
            code = (payload or {}).get("code") or ""
            if self._pending_requests.get(operator) != request:
                return
            if self._waiting.get(operator) == "otp":
                if code:
                    self._waiting.pop(operator, None)
                    if (payload or {}).get("kind") == "apple-2fa":
                        info["logged_in"] = True
                        self._emit({"type": "operator.log", "operator": operator, "message": "双重认证通过，Apple 账户已登录；会话保存在本路 profile。", "level": "info"})
                        self._emit({"type": "operator.state", "operator": operator, "state": "已登录" if not info["running"] else info["state"],
                                    "phase": info["phase"], "detail": "Apple 账户已登录；预热时会复用此会话。"})
                    else:
                        self._emit({"type": "operator.log", "operator": operator, "message": "验证码已提交，官网验证通过。", "level": "info"})
                    self._run()
                else:
                    self._waiting[operator] = "resume"
                    self._emit({"type": "operator.state", "operator": operator, "state": "已暂停", "phase": "paused",
                                "detail": "等待验证码；点击“继续”重新发送。"})
        elif name == "consent":
            request = (payload or {}).get("request")
            if self._pending_requests.get(operator) != request or self._waiting.get(operator) != "consent":
                return
            if (payload or {}).get("accept"):
                self._waiting.pop(operator, None)
                self._emit({"type": "operator.log", "operator": operator, "message": "已按你的确认勾选隐私知情同意书。", "level": "info"})
                self._run()
            else:
                self._waiting[operator] = "resume"
                self._emit({"type": "operator.state", "operator": operator, "state": "已暂停", "phase": "paused",
                            "detail": "未同意知情同意书，本路保持暂停。"})
        elif name in ("prepare", "arm", "now", "trial", "trial-retry"):
            if info["running"] or info["pending"]:
                return
            if name == "prepare":
                info["pending"] = True
                self._queues[operator] += [
                    {"type": "operator.state", "operator": operator, "state": "准备中", "detail": "启动独立 Chromium", "phase": "idle"},
                    {"type": "operator.state", "operator": operator, "state": "准备就绪", "detail": "会话已建立", "phase": "idle"},
                    lambda: info.__setitem__("pending", False)]
            elif name == "arm":
                target = self._configs[operator].start_time()
                info.update(running=True, phase="waiting")
                self._queues[operator] += [{"type": "operator.run", "operator": operator, "trial": False, "target": target.isoformat()},
                                           {"type": "operator.state", "operator": operator, "state": "等待开始", "phase": "waiting", "detail": "到点自动开始"}]
            else:
                self._queues[operator] = list(self._script(operator, trial=name.startswith("trial")))
            if self._state == "idle":
                self._set_state("running" if name != "prepare" else "preparing", "")
            self._run()
        elif name in ("recover", "reset-record", "restart"):
            text = {"recover": "已回到购物袋核对现有商品；未发现重复加购。", "reset-record": "购物袋为空，已清除添加记录。",
                    "restart": "浏览器已用同一 profile 重启；防重复记录保留。"}[name]
            if name == "restart":
                self._emit({"type": "dispatcher.health", "operator": operator, "healthy": False, "reason": "用户要求重启", "action": "hard-restart"})
                self._emit({"type": "dispatcher.health", "operator": operator, "healthy": True, "reason": "", "action": "none"})
            self._log(operator, text)

    def shutdown(self) -> None:
        self.shutdown_called = True
        self._timer.stop()
        for op in OPERATORS:
            self._queues[op].clear()
            self._ops[op].update(running=False, pending=False)
        self._state = "idle"

    def is_busy(self) -> bool:
        return any(info["running"] or info["pending"] for info in self._ops.values())

    # ---- scripted scenario --------------------------------------------------------------------
    def _script(self, op: str, trial: bool):
        cfg = self._configs[op]
        info = self._ops[op]
        info.update(running=True, pending=False, trial=trial, submitted=False, review_ok=False, paused=False,
                    lanes={}, checks={}, store="")
        title = FULFILLMENTS[op]
        product = "iPhone Air 云白色" if trial else "iPhone Duo 星光白色"
        now = datetime.now(CHINA)
        yield {"type": "operator.run", "operator": op, "trial": trial, "target": now.isoformat()}
        yield {"type": "operator.state", "operator": op, "state": "入口竞速", "phase": "entry",
               "detail": f"{cfg.entry_tabs} 个入口同时刷新，官网维护页中…"}
        yield {"type": "operator.lanes", "operator": op, "total": cfg.entry_tabs, "loading": cfg.entry_tabs, "requests": 4, "cooldown_s": 0, "winner": None}
        if op == "delivery" and self._browser_mode == "auto":
            yield {"type": "operator.network", "operator": op, "status": 541, "path": "/shop/fulfillment-messages", "count": 1}
            yield {"type": "operator.log", "operator": op, "message": "官网返回 HTTP 541（疑似无头特征被识别），共享冷却 15 秒。", "level": "warn"}
            yield {"type": "operator.lanes", "operator": op, "total": cfg.entry_tabs, "loading": 0, "requests": 9, "cooldown_s": 15, "winner": None}
            yield {"type": "dispatcher.browser-mode", "operator": op, "mode": "hidden", "reason": "入口阶段 541：以同一 profile 切换为隐藏窗口的有头浏览器"}
            yield lambda: info.__setitem__("browser_mode", "hidden")
        yield {"type": "operator.lanes", "operator": op, "total": cfg.entry_tabs, "loading": 2, "requests": 17, "cooldown_s": 0, "winner": None}
        yield {"type": "operator.lanes", "operator": op, "total": cfg.entry_tabs, "loading": 1, "requests": 23, "cooldown_s": 0, "winner": 2}
        # §12.5: timing events carry both the per-click metric (action/ms) and the phase duration (phase/phase_ms).
        yield {"type": "operator.timing", "operator": op, "action": "入口 2 进入购买页", "ms": 1840, "phase": "entry", "phase_ms": 1840}
        yield {"type": "operator.step", "operator": op, "phase": "entry", "done": True}
        yield {"type": "operator.state", "operator": op, "state": "选配中", "phase": "configure", "detail": f"正在选择 {product} / 1TB / 不折抵 / AppleCare+"}
        checks = {}
        for key, ms, action in (("color", 210, "选择颜色"), ("capacity", 320, "选择 1TB"), ("trade", 180, "选择不折抵"), ("care", 460, "添加 AppleCare+")):
            checks = dict(checks, **{key: True})
            yield {"type": "operator.checks", "operator": op, "checks": dict(checks)}
            yield {"type": "operator.timing", "operator": op, "action": action, "ms": ms}
        yield {"type": "operator.timing", "operator": op, "phase": "configure", "phase_ms": 1170}
        yield {"type": "operator.step", "operator": op, "phase": "configure", "done": True}
        yield {"type": "operator.state", "operator": op, "state": "加入购物袋", "phase": "add", "detail": "点击“加入购物袋”，等待官网确认"}
        yield {"type": "operator.timing", "operator": op, "action": "加入购物袋", "ms": 612, "phase": "add", "phase_ms": 640}
        yield {"type": "operator.step", "operator": op, "phase": "add", "done": True}
        yield {"type": "operator.state", "operator": op, "state": "核对购物袋", "phase": "bag", "detail": f"购物袋：{product} 1TB ×1 + AppleCare+"}
        yield {"type": "operator.timing", "operator": op, "phase": "bag", "phase_ms": 930}
        yield {"type": "operator.step", "operator": op, "phase": "bag", "done": True}
        yield {"type": "operator.state", "operator": op, "state": "结账中", "phase": "checkout", "detail": "游客结账 · 选择取货/送货方式"}
        yield {"type": "operator.timing", "operator": op, "phase": "checkout", "phase_ms": 1480}
        if op == "pickup":
            yield {"type": "operator.store", "operator": op, "store": "环球港"}
            yield {"type": "operator.timing", "operator": op, "phase": "fulfillment", "phase_ms": 1210}
            yield {"type": "operator.state", "operator": op, "state": "填写取货信息", "phase": "contact", "detail": "已选环球港 · 填写取货人与联系方式"}
            yield {"type": "operator.timing", "operator": op, "action": "批量填写联系人", "ms": 380, "phase": "contact", "phase_ms": 890}
            request = uuid.uuid4().hex
            self._pending_requests[op] = request
            yield {"type": "operator.otp", "operator": op, "request": request, "phone_tail": cfg.phone[-4:], "kind": "sms",
                   "message": f"官网已向尾号 {cfg.phone[-4:]} 发送短信验证码，请在 2 分钟内输入。", "error": False}
            yield ("wait", "otp")
            yield {"type": "operator.state", "operator": op, "state": "手机已验证", "phase": "verify", "detail": "联系方式验证通过"}
            yield {"type": "operator.timing", "operator": op, "phase": "verify", "phase_ms": 4200}
        else:
            yield {"type": "operator.state", "operator": op, "state": "填写送货地址", "phase": "fulfillment", "detail": cfg.shipping_summary}
            request = uuid.uuid4().hex
            self._pending_requests[op] = request
            yield {"type": "operator.consent", "operator": op, "request": request, "url": "https://secure8.www.apple.com.cn/shop/checkout?_s=Shipping-init",
                   "text": "（演示文本）Apple 隐私知情同意书：为完成本次订单，Apple 需要处理你的联系方式与收货地址……有限的个人信息可能会在中国境外存储和处理。"}
            yield ("wait", "consent")
            yield {"type": "operator.timing", "operator": op, "action": "批量填写地址", "ms": 410, "phase": "fulfillment", "phase_ms": 2300}
            yield {"type": "operator.state", "operator": op, "state": "填写联系人", "phase": "contact", "detail": "填写收件人与手机号"}
            yield {"type": "operator.timing", "operator": op, "phase": "contact", "phase_ms": 760}
        yield {"type": "operator.state", "operator": op, "state": "选择付款方式", "phase": "payment", "detail": "招商银行信用卡分期 24 期 · 个人电子发票"}
        yield {"type": "operator.timing", "operator": op, "action": "选择招商银行 24 期", "ms": 412, "phase": "payment", "phase_ms": 1020}
        yield {"type": "operator.step", "operator": op, "phase": "checkout", "done": True}
        yield {"type": "operator.state", "operator": op, "state": "核对中", "phase": "review", "detail": "逐项核对订单摘要：机型 / 颜色 / 1TB / AC+ / 联系人 / 门店或地址 / 分期 / 金额"}
        if op == "delivery":
            yield {"type": "operator.review", "operator": op, "valid": False, "problems": ["送货地址与设置不一致或无法完整核对"], "total": None, "store": "", "destination": cfg.shipping_summary}
            yield {"type": "operator.log", "operator": op, "message": "官网把地址切回默认地址，已重新填写并再次核对。", "level": "warn"}
        total = 16098.0 if trial else 24098.0
        yield {"type": "operator.review", "operator": op, "valid": True, "problems": [], "total": total,
               "store": "环球港" if op == "pickup" else "", "destination": "" if op == "pickup" else cfg.shipping_summary}
        yield lambda: info.__setitem__("review_ok", True)
        if trial:
            yield {"type": "operator.attention", "operator": op, "message": f"试跑到达核对页（¥{total:,.0f}），按规定停止，未提交。",
                   "suggestion": "请到官网购物袋移除测试商品。", "review": False}
            yield lambda: info.__setitem__("review_ok", False)
            yield {"type": "operator.finished", "operator": op, "success": True, "reason": "试跑完成：到达核对页，未提交订单。"}
            yield lambda: self._finish(op, success=True)
        elif cfg.auto_submit:
            yield lambda: self._submit(op)
        else:
            yield {"type": "operator.attention", "operator": op, "message": f"订单核对通过（¥{total:,.0f}），等待你点击“提交已核对订单”。",
                   "suggestion": "确认金额与门店/地址后点击提交；付款在手机掌上生活完成。", "review": True}
            yield ("wait", "submit")

    def _submit(self, op: str) -> None:
        info = self._ops[op]
        if not info["running"] or not info["review_ok"] or info["submitted"]:
            return
        single = self._policy == "single" or bool(self._limit)
        if single and self._owner and self._owner != op:
            self._emit({"type": "operator.attention", "operator": op, "message": FULFILLMENTS[self._owner] + "已取得唯一提交权；本路暂停，不自动补单。",
                        "suggestion": "先在官网核对那一路的结果。", "review": False})
            return
        self._waiting.pop(op, None)
        self._owner = self._owner or op
        self._attempts[op] = {"phase": "submit-attempt", "time": datetime.now(CHINA).isoformat()}
        info.update(submitted=True, review_ok=False)
        self._emit_policy(f"{FULFILLMENTS[op]}已取得提交权并点击“立即下单”。" if single else "")
        self._emit({"type": "operator.state", "operator": op, "state": "提交中", "phase": "submit", "detail": "已点击一次“立即下单”，等待官网确认（不会重复点击）"})
        self._queues[op] = [
            {"type": "operator.timing", "operator": op, "action": "立即下单", "ms": 2310},
            {"type": "operator.step", "operator": op, "phase": "submit", "done": True},
            {"type": "operator.state", "operator": op, "state": "订单确认", "phase": "done", "detail": "订单已创建（演示编号 W0000000000），请在 30 分钟内用掌上生活付款"},
            {"type": "operator.finished", "operator": op, "success": True, "reason": "订单已创建，等待付款。"},
            lambda: self._finish(op, success=True),
        ]
        self._run()

    def _finish(self, op: str, success: bool) -> None:
        self._ops[op].update(running=False, pending=False, paused=False, review_ok=False)
        self._waiting.pop(op, None)
        if not self.is_busy() and self._state in ("running", "preparing"):
            self._set_state("idle", "任务结束。" if success else "已停止。")

    def _stop(self, op: str) -> None:
        info = self._ops[op]
        self._queues[op].clear()
        self._waiting.pop(op, None)
        if info["running"] or info["pending"]:
            self._emit({"type": "operator.finished", "operator": op, "success": False, "reason": "已停止任务，保留浏览器进度和防重复记录。"})
            self._emit({"type": "operator.state", "operator": op, "state": "已停止", "phase": "idle", "detail": "已停止；可重新布防或开始。"})
        info.update(running=False, pending=False, paused=False, review_ok=False)

    # ---- plumbing -----------------------------------------------------------------------------
    def _run(self) -> None:
        if self.interval_ms <= 0:
            self.pump()
        elif not self._timer.isActive():
            self._timer.start(self.interval_ms)

    def pump(self, max_steps: int = 10000) -> None:
        """Deliver queued events synchronously until all queues are empty or blocked (tests / instant mode)."""
        for _ in range(max_steps):
            if not self._step():
                break

    def _step(self) -> bool:
        progressed = False
        for op in OPERATORS:
            if self._waiting.get(op) or not self._queues[op]:
                continue
            item = self._queues[op].pop(0)
            progressed = True
            if isinstance(item, tuple) and item[0] == "wait":
                self._waiting[op] = item[1]
            elif callable(item):
                item()
            else:
                self._emit(item)
        if not progressed and self._timer.isActive() and all(not q or self._waiting.get(op) for op, q in self._queues.items()):
            self._timer.stop()
        return progressed

    def _set_state(self, state: str, message: str) -> None:
        self._state = state
        self._emit({"type": "dispatcher.state", "operator": None, "state": state, "message": message})

    def _emit_policy(self, message: str) -> None:
        single = self._policy == "single" or bool(self._limit)
        self._emit({"type": "dispatcher.policy", "operator": None, "policy": self._policy, "single": single,
                    "limit": self._limit, "owner": self._owner, "message": message, "mode": self._mode})

    def set_mode(self, mode: str, message: str = "") -> None:
        """§13.2: "parallel" | "sequential-account" (dev helper to exercise the UI)."""
        self._mode = mode
        self._emit_policy(message)

    def _log(self, op: str | None, message: str, level: str = "info") -> None:
        self._emit({"type": "operator.log", "operator": op, "message": message, "level": level})

    def _emit(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("time", datetime.now(CHINA).isoformat())
        event.setdefault("operator", None)
        op = event.get("operator")
        kind = event["type"]
        if op in self._ops:
            info = self._ops[op]
            if kind == "operator.state":
                info["state"] = event.get("state", info["state"])
                info["detail"] = event.get("detail", info["detail"])
                phase = event.get("phase")
                if phase == "paused":
                    info["paused"] = True
                elif phase:
                    info["phase"], info["paused"] = phase, False
            elif kind == "operator.lanes":
                info["lanes"] = {k: event.get(k) for k in ("total", "loading", "requests", "cooldown_s", "winner")}
            elif kind == "operator.timing" and event.get("phase") and event.get("phase_ms") is not None:
                info["timeline_ms"][event["phase"]] = int(event["phase_ms"])
            elif kind == "operator.checks":
                info["checks"] = dict(event.get("checks") or {})
            elif kind == "operator.store":
                info["store"] = event.get("store", "")
            elif kind == "operator.review":
                info["review_ok"] = bool(event.get("valid")) and info["running"] and not info["submitted"]
            elif kind == "operator.page":
                info["last_image"] = event.get("image", "") or info["last_image"]
            elif kind == "operator.run":
                info.update(running=True, pending=False, trial=bool(event.get("trial")), submitted=False, review_ok=False,
                            timeline_ms={})
            elif kind == "dispatcher.browser-mode":
                info["browser_mode"] = event.get("mode", info["browser_mode"])
            elif kind == "dispatcher.health":
                info["healthy"] = bool(event.get("healthy", True))
        if kind == "dispatcher.clock":
            self._clock_offset = event.get("offset_ms")
        self.event.emit(event)

    # ---- canned dashboard for screenshots -------------------------------------------------------
    def demo(self, image_dir: Path | str | None = None) -> None:
        """Emit a realistic mid-flight snapshot instantly (used by `app.py --ui-smoke --stub`)."""
        image = ""
        if image_dir:
            image = str(_demo_page_image(Path(image_dir)))
        self._clock_offset = 137
        self._emit({"type": "dispatcher.clock", "operator": None, "offset_ms": 137, "source": "www.apple.com.cn Date 头 ×3 中位数"})
        self._set_state("running", "两路并行中。尚未识别明确限购：各争取一单；发现每人一单／一台后自动切换竞速。")
        self._emit_policy("")
        # The demo "sale" started 41 s ago so the stopwatch column shows a plausible running total.
        target = (datetime.now(CHINA) - timedelta(seconds=41)).isoformat(timespec="seconds")
        for op in OPERATORS:
            self._ops[op].update(running=True, pending=False)
            self._emit({"type": "operator.run", "operator": op, "trial": False, "target": target})
            self._emit({"type": "operator.log", "operator": op, "message": "入口竞速开始：3 个入口同时刷新维护页。", "level": "info"})
        # pickup: review passed, waiting for the user's submit click
        p = "pickup"
        self._emit({"type": "operator.lanes", "operator": p, "total": 3, "loading": 0, "requests": 23, "cooldown_s": 0, "winner": 2})
        self._emit({"type": "operator.timing", "operator": p, "action": "入口 2 进入购买页", "ms": 1840, "phase": "entry", "phase_ms": 1840})
        for phase, ms in (("configure", 1170), ("add", 640), ("bag", 930), ("checkout", 1480), ("fulfillment", 1210),
                          ("contact", 890), ("verify", 4200), ("payment", 1020)):
            self._emit({"type": "operator.timing", "operator": p, "phase": phase, "phase_ms": ms})
        self._emit({"type": "operator.checks", "operator": p, "checks": {"color": True, "capacity": True, "trade": True, "care": True}})
        self._emit({"type": "operator.store", "operator": p, "store": "环球港"})
        self._emit({"type": "operator.log", "operator": p, "message": "已选门店：环球港（有货）。", "level": "info"})
        for phase in ("entry", "configure", "add", "bag", "checkout"):
            self._emit({"type": "operator.step", "operator": p, "phase": phase, "done": True})
        self._emit({"type": "operator.state", "operator": p, "state": "待确认", "phase": "review",
                    "detail": "订单摘要已逐项核对通过：iPhone Duo 星光白色 1TB · AppleCare+ · 环球港自取 · 招行 24 期"})
        self._emit({"type": "operator.timing", "operator": p, "action": "选择招商银行 24 期", "ms": 412})
        self._emit({"type": "operator.review", "operator": p, "valid": True, "problems": [], "total": 24098.0, "store": "环球港", "destination": ""})
        self._emit({"type": "operator.page", "operator": p, "url": "https://secure8.www.apple.com.cn/shop/checkout?_s=Review",
                    "text": "（演示）订单核对页", "image": image})
        self._emit({"type": "operator.attention", "operator": p, "message": "订单核对通过（¥24,098），等待你点击“提交已核对订单”。",
                    "suggestion": "确认金额与门店后点击提交；付款在手机掌上生活完成。", "review": True})
        self._ops[p]["review_ok"] = True
        self._waiting[p] = "submit"
        # delivery: hit a 541, fell back to hidden-headed, now at checkout filling the address
        d = "delivery"
        self._emit({"type": "operator.network", "operator": d, "status": 541, "path": "/shop/fulfillment-messages", "count": 2})
        self._emit({"type": "operator.log", "operator": d, "message": "官网返回 HTTP 541，共享冷却 15 秒后以隐藏窗口模式重试。", "level": "warn"})
        self._emit({"type": "dispatcher.browser-mode", "operator": d, "mode": "hidden", "reason": "入口阶段 541：以同一 profile 切换为隐藏窗口的有头浏览器"})
        self._emit({"type": "operator.lanes", "operator": d, "total": 3, "loading": 0, "requests": 31, "cooldown_s": 0, "winner": 3})
        self._emit({"type": "operator.timing", "operator": d, "phase": "entry", "phase_ms": 17650})
        self._emit({"type": "operator.timing", "operator": d, "phase": "configure", "phase_ms": 1240})
        self._emit({"type": "operator.timing", "operator": d, "action": "加入购物袋", "ms": 612, "phase": "add", "phase_ms": 650})
        self._emit({"type": "operator.timing", "operator": d, "phase": "bag", "phase_ms": 1010})
        self._emit({"type": "operator.checks", "operator": d, "checks": {"color": True, "capacity": True, "trade": True, "care": True}})
        for phase in ("entry", "configure", "add", "bag"):
            self._emit({"type": "operator.step", "operator": d, "phase": phase, "done": True})
        self._emit({"type": "operator.state", "operator": d, "state": "结账中", "phase": "fulfillment",
                    "detail": "游客结账 · 正在填写送货地址：上海市嘉定区示例路1号101室"})
        self._emit({"type": "operator.page", "operator": d, "url": "https://secure8.www.apple.com.cn/shop/checkout?_s=Shipping-init",
                    "text": "（演示）送货地址页", "image": image})
        self._emit({"type": "dispatcher.health", "operator": d, "healthy": True, "reason": "", "action": "none"})
        self._emit({"type": "operator.log", "operator": None, "message": "指派员：两路健康，心跳正常。", "level": "info"})


def _demo_page_image(folder: Path) -> Path:
    """A clearly labelled fake 'page screenshot' so the demo dashboard has thumbnails."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "demo-page.png"
    pix = QPixmap(900, 600)
    pix.fill(QColor("#ffffff"))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1d1d1f"))
    painter.drawRect(0, 0, 900, 44)
    painter.setBrush(QColor("#f5f5f7"))
    painter.drawRoundedRect(QRectF(60, 90, 520, 420), 12, 12)
    painter.drawRoundedRect(QRectF(610, 90, 230, 300), 12, 12)
    painter.setBrush(QColor("#d2d2d7"))
    for i in range(6):
        painter.drawRoundedRect(QRectF(90, 130 + i * 56, 360 - i * 30, 14), 7, 7)
    painter.setBrush(QColor("#0071e3"))
    painter.drawRoundedRect(QRectF(640, 330, 170, 40), 20, 20)
    painter.setPen(QColor("#6e6e73"))
    painter.setFont(QFont("Microsoft YaHei UI", 16))
    painter.drawText(QRectF(0, 530, 900, 50), Qt.AlignmentFlag.AlignCenter, "演示截图 · 非真实官网页面")
    painter.end()
    pix.save(str(path), "PNG")
    return path

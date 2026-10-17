"""The dispatcher card: overall state, policy, submit owner, browser mode, clock offset, health, timeline."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy

from duo.model import FULFILLMENTS

from . import theme
from .widgets import Card, KeyValueGrid, StatusPill, Timeline, WrapLabel, button, divider, label

try:  # Worker A's module; fall back to the same titles if it is renamed.
    from duo.orders import POLICIES
except Exception:  # pragma: no cover - defensive import
    POLICIES = {"adaptive": "争取两单；识别限购后只下一单", "single": "始终只下一单；两路竞速"}

BROWSER_MODES = {"auto": "自动（优先无头，拦截时降级）", "headless": "无头浏览器", "hidden": "隐藏窗口（有头）"}
STATE_TITLES = {"idle": ("空闲", "neutral"), "preparing": ("预热中", "info"), "armed": ("已布防", "info"),
                "running": ("运行中", "info"), "stopping": ("停止中", "warning")}


class DispatcherPanel(Card):
    policy_changed = Signal(str)
    browser_mode_changed = Signal(str)
    action = Signal(str)          # "prepare" | "start_now"

    def __init__(self):
        self.pill = StatusPill("空闲", "neutral")
        super().__init__("指派员", right=self.pill, padding=18)
        self.setFixedWidth(340)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.state = "idle"
        self.health_by_operator: dict[str, tuple[bool, str, str]] = {}
        self.message = WrapLabel("尚未识别明确限购：两路各争取一单；发现每人一单／一台后自动切换竞速。", lines=2)
        self.body.addWidget(self.message)
        grid = KeyValueGrid(caption_width=64)
        self.policy = QComboBox()
        for key, title in POLICIES.items():
            self.policy.addItem(title, key)
        self.policy.setToolTip("运行中不可切换；有提交记录后也不可切换")
        grid.add_row("下单策略", self.policy)
        self.browser_mode = QComboBox()
        for key, title in BROWSER_MODES.items():
            self.browser_mode.addItem(title, key)
        self.browser_mode.setToolTip("自动：先用隐身无头；遇到 541/403 拦截时该路自动切换为隐藏窗口的有头浏览器")
        grid.add_row("浏览器", self.browser_mode)
        self.owner = grid.add_row("提交权")
        self.owner.setText("尚未分配")
        self.mode = grid.add_row("下单模式")
        self.mode.setText("两路并行")
        self.clock = grid.add_row("时钟偏移")
        self.clock.setText("未校准 · 使用本机时钟")
        self.clock.setObjectName("muted")
        self.health = grid.add_row("健康")
        self.health.setText("两路正常")
        self.health.setStyleSheet(f"color:{theme.SUCCESS};")
        self.body.addLayout(grid)
        quick = QHBoxLayout()
        quick.setSpacing(6)
        quick.addWidget(label("快捷", "caption"))
        self.prepare_button = button("预热全部", "ghost", tip="现在启动两路浏览器、打开商品页并校准时钟（不下单）")
        self.now_button = button("立即开始全部", "ghost", tip="不等开售时间，两路立即开始正式任务")
        quick.addWidget(self.prepare_button)
        quick.addWidget(self.now_button)
        quick.addStretch(1)
        self.body.addLayout(quick)
        self.body.addWidget(divider())
        head = QHBoxLayout()
        head.addWidget(label("时间线", "sectionTitle"))
        head.addStretch(1)
        self.count = label("最近 0 条", "muted")
        head.addWidget(self.count)
        self.body.addLayout(head)
        self.timeline = Timeline(max_items=50)
        self.body.addWidget(self.timeline, 1)
        self.policy.currentIndexChanged.connect(lambda _i: self.policy_changed.emit(self.policy.currentData()))
        self.browser_mode.currentIndexChanged.connect(lambda _i: self.browser_mode_changed.emit(self.browser_mode.currentData()))
        self.prepare_button.clicked.connect(lambda: self.action.emit("prepare"))
        self.now_button.clicked.connect(lambda: self.action.emit("start_now"))

    # ---- timeline ----------------------------------------------------------------------
    def add_timeline(self, text: str, tag: str = "", tone: str = "info") -> None:
        self.timeline.add(text, tag, tone)
        self.count.setText(f"最近 {self.timeline.model_.rowCount()} 条")

    # ---- status / events -------------------------------------------------------------------
    def set_locked(self, locked: bool) -> None:
        self.policy.setEnabled(not locked)
        self.browser_mode.setEnabled(not locked)
        self.prepare_button.setEnabled(not locked)
        self.now_button.setEnabled(not locked)

    def set_state(self, state: str, message: str = "") -> None:
        self.state = state or "idle"
        title, tone = STATE_TITLES.get(self.state, (self.state, "info"))
        self.pill.set_state(title, tone)
        if message:
            self.message.setText(message)

    def set_mode(self, mode: str | None) -> None:
        """§13.2: `dispatcher.policy.mode` — "parallel" | "sequential-account"."""
        if not mode:
            return
        if mode == "sequential-account":
            self.mode.setText("同账号顺序下单")
            self.mode.setStyleSheet(f"color:{theme.WARNING}; font-weight:600;")
            self.mode.setToolTip("官网要求登录：先让已登录的一路完成提交，确认后另一路用同一账号再下一单。")
        else:
            self.mode.setText("两路并行")
            self.mode.setStyleSheet("")
            self.mode.setToolTip("两路各自独立的浏览器与购物袋，同时推进。")

    def set_policy(self, policy: str | None, single: bool | None = None, limit: dict | None = None,
                   owner: str | None = None, message: str = "", mode: str | None = None) -> None:
        if policy:
            index = self.policy.findData(policy)
            if index >= 0 and index != self.policy.currentIndex():
                self.policy.blockSignals(True)
                self.policy.setCurrentIndex(index)
                self.policy.blockSignals(False)
        self.set_mode(mode)
        if owner:
            self.owner.setText(FULFILLMENTS.get(owner, owner) + " 已取得唯一提交权")
            self.owner.setStyleSheet(f"color:{theme.PRIMARY}; font-weight:600;")
        elif owner is None and policy is not None:
            self.owner.setText("尚未分配" if (single or limit) else "各路独立提交（争取两单）")
            self.owner.setStyleSheet("")
        if message:
            self.message.setText(message)
        elif limit:
            evidence = limit.get("evidence") if isinstance(limit, dict) else ""
            self.message.setText("已识别官网限购" + (f"：“{evidence}”" if evidence else "") + "。先核对通过、允许提交的一路取得唯一提交权。")
        elif policy == "single":
            self.message.setText("两路继续并行；先核对通过、允许提交的一路取得唯一提交权。")
        elif policy == "adaptive":
            self.message.setText("尚未识别明确限购：两路各争取一单；发现每人一单／一台后自动切换竞速。")

    def set_browser_mode(self, mode: str) -> None:
        index = self.browser_mode.findData(mode)
        if index >= 0 and index != self.browser_mode.currentIndex():
            self.browser_mode.blockSignals(True)
            self.browser_mode.setCurrentIndex(index)
            self.browser_mode.blockSignals(False)

    def set_clock(self, offset_ms: int | None, source: str = "") -> None:
        if offset_ms is None:
            self.clock.setText("未校准 · 使用本机时钟")
            self.clock.setObjectName("muted")
        else:
            direction = "官网比本机快" if offset_ms > 0 else "官网比本机慢" if offset_ms < 0 else "与官网一致"
            text = f"{offset_ms:+d} ms · {direction}" if offset_ms else "0 ms · 与官网一致"
            self.clock.setText(text)
            self.clock.setToolTip(f"校准来源：{source}" if source else "")
            self.clock.setObjectName("value")
        theme.repolish(self.clock)

    def set_health(self, operator: str | None, healthy: bool, reason: str = "", action: str = "none") -> None:
        if operator:
            self.health_by_operator[operator] = (healthy, reason, action)
        bad = [(op, r, a) for op, (ok, r, a) in self.health_by_operator.items() if not ok]
        if not bad:
            self.health.setText("两路正常")
            self.health.setStyleSheet(f"color:{theme.SUCCESS};")
            self.health.setToolTip("")
            return
        parts = []
        for op, reason, action in bad:
            verb = {"soft-restart": "软重启", "hard-restart": "硬重启"}.get(action, "异常")
            parts.append(f"{FULFILLMENTS.get(op, op)}：{verb}" + (f"（{reason}）" if reason else ""))
        self.health.setText("；".join(parts))
        self.health.setStyleSheet(f"color:{theme.WARNING}; font-weight:600;")
        self.health.setToolTip("\n".join(parts))

    def set_status(self, status: dict | None) -> None:
        if not status:
            return
        self.set_state(str(status.get("state") or "idle"))
        self.set_policy(status.get("policy"), status.get("single"), status.get("limit"), status.get("owner"),
                        str(status.get("error") or ""), status.get("mode"))
        if status.get("browser_mode"):
            self.set_browser_mode(str(status["browser_mode"]))
        self.set_clock(status.get("clock_offset_ms"))
        for op, info in (status.get("operators") or {}).items():
            if isinstance(info, dict) and "healthy" in info:
                self.health_by_operator[op] = (bool(info.get("healthy", True)), "", "none")
        self.set_health(None, True)
        self.set_locked(self.state not in ("idle",))

    def apply(self, event: dict) -> None:
        kind = event.get("type", "")
        if kind == "dispatcher.state":
            self.set_state(str(event.get("state") or ""), str(event.get("message") or ""))
            self.set_locked(self.state not in ("idle",))
        elif kind == "dispatcher.policy":
            self.set_policy(event.get("policy"), event.get("single"), event.get("limit"), event.get("owner"),
                            str(event.get("message") or ""), event.get("mode"))
        elif kind == "dispatcher.clock":
            self.set_clock(event.get("offset_ms"), str(event.get("source") or ""))
        elif kind == "dispatcher.health":
            self.set_health(event.get("operator"), bool(event.get("healthy", True)), str(event.get("reason") or ""),
                            str(event.get("action") or "none"))

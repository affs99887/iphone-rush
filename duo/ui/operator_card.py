"""One card per operator (门店自取 / 送货到家): status, 6-step bar, race meter, checks, speed table, review,
page thumbnail and the action row."""
from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy

from duo.model import FULFILLMENTS, Config

from . import theme
from .dialogs import ConsentDialog, LoginDialog, PagePreviewDialog, confirm, present
from .widgets import (CHINA, Card, CheckList, Chip, KeyValueGrid, LaneMeter, ScreenshotThumb, SpeedTable, StatusPill,
                      StepBar, WrapLabel, button, divider, tone_for_state)

FLAGS = ("paused", "attention", "blocked", "failed")
BROWSER_MODE_TITLES = {"headless": "无头", "hidden": "隐藏窗口", "auto": "自动"}
PICKUP_DEFAULT_PLACE = "上海直营店 · 有货即选（优先静安 / 环贸 iapm / 环球港）"


def parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=CHINA)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA)
    return None


class OperatorCard(Card):
    """Consumes `operator.*` events for one operator and emits commands for the dispatcher."""

    command = Signal(str, str, object)      # operator, command name, payload
    needs_attention = Signal(str, str)      # operator, kind ("attention" | "otp" | "consent")

    def __init__(self, operator: str):
        self.operator = operator
        self.title = FULFILLMENTS.get(operator, operator)
        self.pill = StatusPill("等待开始", "neutral")
        super().__init__(self.title, right=self.pill, padding=16)
        self.body.setSpacing(8)
        self.setMinimumWidth(330)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # runtime flags mirrored from events / status()
        self.running = self.pending = self.paused = False
        self.review_ok = self.submitted = self.trial = False
        self.attention = False
        self.phase = "idle"
        self.cfg: Config | None = None
        self.apple_id_hint = ""            # last Apple ID typed this session (never the password)
        self.waiting_code = False          # an operator.otp is outstanding (entered via notifications)
        self.consent_dialog: ConsentDialog | None = None
        self.preview_dialog: PagePreviewDialog | None = None
        self.login_dialog: LoginDialog | None = None
        self.last_page: dict = {}
        self._build()
        self.ticker = QTimer(self)
        self.ticker.setInterval(500)
        self.ticker.timeout.connect(self.speed.tick)
        self._refresh_buttons()

    # ---- layout ---------------------------------------------------------------------
    def _build(self) -> None:
        # header chips (trial / browser mode / network / health) sit left of the pill
        self.trial_chip = Chip("试跑 · iPhone Air", "info")
        self.mode_chip = Chip("无头", "neutral")
        self.network_chip = Chip("", "danger")
        self.health_chip = Chip("心跳异常", "warning")
        for chip in (self.trial_chip, self.mode_chip, self.network_chip, self.health_chip):
            chip.setVisible(False)
            self.header.insertWidget(self.header.count() - 1, chip)
        detail_row = QHBoxLayout()
        detail_row.setSpacing(8)
        self.detail = WrapLabel("等待布防或开始；两路互不干扰，独立浏览器与购物袋。", lines=2)
        detail_row.addWidget(self.detail, 1)
        self.suggest_button = button("", "compact")
        self.suggest_button.setVisible(False)
        self.suggest_button.setMaximumHeight(26)
        detail_row.addWidget(self.suggest_button, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(detail_row)
        self.steps = StepBar()
        self.body.addWidget(self.steps)
        self.body.addWidget(divider())
        grid = KeyValueGrid(caption_width=56)
        self.lanes = LaneMeter()
        grid.add_row("入口竞速", self.lanes)
        self.checks = CheckList()
        grid.add_row("选配核对", self.checks)
        self.place = grid.add_row("门店" if self.operator == "pickup" else "地址")
        self.place.setText(PICKUP_DEFAULT_PLACE if self.operator == "pickup" else "按设置中的送货地址核对")
        self.review_label = grid.add_row("订单核对")
        self.review_label.setText("待核对")
        self.timing = grid.add_row("动作耗时")
        self.timing.setText("—")
        self.timing.setObjectName("muted")
        self.body.addLayout(grid)
        self.speed = SpeedTable()
        self.body.addWidget(self.speed)
        self.thumb = ScreenshotThumb()
        self.thumb.clicked.connect(self.show_page)
        self._update_summary()
        self.body.addWidget(self.thumb, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pause_button = button("暂停", tip="暂停本路自动操作，浏览器保持当前页面")
        self.resume_button = button("继续", tip="继续本路自动操作（人工处理后）")
        self.show_button = button("查看页面", tip="查看本路后台浏览器的当前页面")
        self.more_button = button("更多")
        self.more_button.setMenu(self._build_menu())
        for b in (self.pause_button, self.resume_button, self.show_button, self.more_button):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row.addWidget(b)
        self.body.addLayout(row)
        self.submit_button = button("提交已核对订单", "primary", tip="仅在订单核对通过后可用；点击一次，官网付款仍需在手机完成")
        self.body.addWidget(self.submit_button)
        self.pause_button.clicked.connect(lambda: self._send("pause"))
        self.resume_button.clicked.connect(self._resume)
        self.show_button.clicked.connect(self.show_page)
        self.submit_button.clicked.connect(self._submit)
        self.suggest_button.clicked.connect(self._suggestion_clicked)
        self._suggestion_command = None

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        self.actions: dict[str, QAction] = {}

        def add(key: str, text: str, handler, tip: str = ""):
            action = QAction(text, menu)
            if tip:
                action.setToolTip(tip)
            action.triggered.connect(handler)
            menu.addAction(action)
            self.actions[key] = action
            return action

        add("prepare", "单独预热（启动浏览器、校准时钟）", lambda: self._send("prepare"))
        add("arm", "单独布防（到点自动开始）", lambda: self._send("arm"))
        add("now", "单独立即开始", lambda: self._send("now"))
        add("trial", "单独官网试跑（iPhone Air）", self._trial)
        menu.addSeparator()
        add("login", "登录 Apple 账户…", self.open_login, "在后台页面内登录；密码不保存")
        add("show-window", "显示浏览器窗口", lambda: self._send("show-window"), "把本路后台浏览器窗口显示出来，手工处理后再隐藏")
        add("hide-window", "隐藏浏览器窗口", lambda: self._send("hide-window"))
        menu.addSeparator()
        add("recover", "恢复购物袋并核对订单", lambda: self._send("recover"))
        add("reset-record", "检查空购物袋（清除添加记录）", self._reset_record)
        add("restart", "重启本路浏览器", self._restart)
        menu.addSeparator()
        add("stop", "停止本路", lambda: self._send("stop"))
        return menu

    # ---- commands -------------------------------------------------------------------
    def _send(self, name: str, payload=None) -> None:
        self.command.emit(self.operator, name, payload)

    def _resume(self) -> None:
        self.set_attention_state(False)
        self._send("resume")

    def _submit(self) -> None:
        if not (self.running and self.review_ok and not self.submitted):
            return
        extra = ("这是试跑（iPhone Air）测试订单：会真实创建订单并收到邮件，不付款会被官网自动取消；"
                 "不影响 16 日正式抢购的防重复记录。\n" if self.trial else "")
        if confirm(self, "提交订单", f"{self.title}：订单核对已通过，确定现在点击一次“立即下单”？\n" + extra +
                                     "提交后不会重复点击；付款需在手机上完成。", yes="提交订单", no="再想想"):
            self.submit_button.setEnabled(False)
            self._send("submit")

    def _trial(self) -> None:
        ending = ("核对通过后自动提交一张测试订单（不付款会被官网自动取消）。" if self.cfg is not None and self.cfg.auto_submit
                  else "到订单核对页停下；要验证下单可点“提交已核对订单”创建测试订单。")
        if confirm(self, "官网试跑", f"{self.title}：将在 Apple 官网实际选购 iPhone Air 云白色 1TB + AppleCare+，" + ending,
                   yes="开始试跑"):
            self._send("trial")

    def _reset_record(self) -> None:
        if confirm(self, "检查空购物袋", f"{self.title}：确认官网购物袋已为空后，清除本路的添加记录？\n"
                                         "有提交记录时不会清除。", yes="检查并清除", danger=True):
            self._send("reset-record")

    def _restart(self) -> None:
        if not self.running or confirm(self, "重启浏览器", f"{self.title}：重启本路浏览器会中断当前页面操作，"
                                                           "防重复记录保留。确定重启？", yes="重启"):
            self._send("restart")

    def _suggestion_clicked(self) -> None:
        command = self._suggestion_command
        if command == "login":
            self.open_login()
        elif command == "code":
            self.suggest_button.setVisible(False)
            self._send("resume")            # the operator asks for the code again → a fresh notification appears
        elif command:
            self._send(command)

    def offer_code_reentry(self) -> None:
        """The user closed this route's code notification while it was waiting: one click brings it back."""
        self.waiting_code = False
        self.pill.set_state("已暂停验证", "warning")
        self.detail.setText("验证码通知已关闭，本路暂停在手机验证这一步。")
        self._suggestion_command = "code"
        self.suggest_button.setText("重新输入验证码")
        self.suggest_button.setVisible(True)
        self._refresh_buttons()

    def show_page(self) -> None:
        if self.preview_dialog is None:
            self.preview_dialog = PagePreviewDialog(self.window(), self.title)
            self.preview_dialog.refresh_requested.connect(lambda: self._send("show"))
        if self.last_page:
            self.preview_dialog.set_page(**self.last_page)
        present(self.preview_dialog)
        self._send("show")

    def open_login(self) -> LoginDialog:
        """Apple ID login (§13.2/§14.5): credentials go straight into one `login` command, never stored."""
        if self.login_dialog is not None:
            present(self.login_dialog)
            return self.login_dialog
        dialog = LoginDialog(self.window(), self.title, self.apple_id_hint)
        dialog.submitted.connect(self._login_submitted)
        dialog.finished.connect(lambda _c: setattr(self, "login_dialog", None))
        self.login_dialog = dialog
        present(dialog)
        return dialog

    def _login_submitted(self, apple_id: str, password: str) -> None:
        self.apple_id_hint = apple_id
        self._send("login", {"apple_id": apple_id, "password": password})
        self.pill.set_state("登录中", "info")
        self.detail.setText("操作员正在后台页面登录 Apple 账户；如需双重认证会弹出验证码窗口。")

    # ---- configuration ------------------------------------------------------------
    def set_config(self, cfg: Config | None) -> None:
        self.cfg = cfg
        if cfg is None:
            return
        if self.operator == "delivery":
            summary = getattr(cfg, "shipping_summary", "") or "按设置中的送货地址核对"
            self.place.setText(summary)
        else:
            preferred = " / ".join(getattr(cfg, "preferred_stores", []) or [])
            self.place.setText("上海直营店 · 有货即选" + (f"（优先 {preferred}）" if preferred else ""))
        self._update_summary()

    def checkout_mode(self) -> str:
        cfg = self.cfg
        if cfg is None:
            return "guest"
        return getattr(cfg, "checkout_mode", "guest" if getattr(cfg, "guest_checkout", True) else "account")

    def _update_summary(self) -> None:
        cfg = self.cfg
        payment = getattr(cfg, "payment", "招商银行信用卡分期 24 期") if cfg else "招商银行信用卡分期 24 期"
        if self.trial:
            title = "试跑目标 · iPhone Air（提交前强制停止）"
            lines = ["iPhone Air · 云白色 · 1TB", "不折抵换购 · AppleCare+ · 数量 1", f"个人电子发票 · {payment}"]
        else:
            title = f"本次目标 · {self.title}"
            lines = ["iPhone Duo · 星光白色 · 1TB", "不折抵换购 · AppleCare+ · 数量 1", f"个人电子发票 · {payment}"]
        if self.operator == "pickup":
            preferred = " / ".join(getattr(cfg, "preferred_stores", []) or ["静安", "上海环贸 iapm", "环球港"])
            lines.append(f"上海直营店自取 · 有货即选 · 优先 {preferred}")
        else:
            lines.append("送货到家 · " + (getattr(cfg, "shipping_summary", "") or "按设置地址"))
        lines.append("登录 Apple 账户结账" if self.checkout_mode() == "account" else "游客结账")
        self.thumb.set_summary(title, lines)

    def set_browser_mode(self, mode: str, reason: str = "") -> None:
        title = BROWSER_MODE_TITLES.get(mode or "", "")
        self.mode_chip.setVisible(bool(title) and mode != "auto")
        if title:
            self.mode_chip.set_chip(title, "warning" if mode == "hidden" else "neutral")
            self.mode_chip.setToolTip(reason or f"浏览器模式：{title}")

    def set_health(self, healthy: bool, reason: str = "", action: str = "none") -> None:
        self.health_chip.setVisible(not healthy)
        if not healthy:
            text = {"soft-restart": "软重启中", "hard-restart": "硬重启中"}.get(action, "心跳异常")
            self.health_chip.set_chip(text, "warning")
            self.health_chip.setToolTip(reason or "")

    def set_clock_offset(self, offset_ms: int | None) -> None:
        self.speed.set_clock_offset(offset_ms)

    # ---- events -------------------------------------------------------------------
    def apply(self, event: dict) -> None:
        kind = event.get("type", "")
        handler = getattr(self, "_on_" + kind.split(".", 1)[-1].replace("-", "_"), None)
        if handler:
            handler(event)
        self._refresh_buttons()

    def _on_state(self, ev: dict) -> None:
        state = str(ev.get("state") or "")
        detail = str(ev.get("detail") or "")
        phase = str(ev.get("phase") or "")
        flag = str(ev.get("flag") or "")
        if phase in FLAGS:
            flag, phase = phase, self.phase
        if not flag:
            if any(k in state for k in ("接管", "待确认", "需要")):
                flag = "attention"
            elif "暂停" in state:
                flag = "paused"
            elif any(k in state for k in ("失败", "错误")):
                flag = "failed"
        if phase:
            self.phase = phase
            self.steps.set_phase(phase)
        self.steps.set_flag(flag if flag in FLAGS else "")
        self.paused = flag == "paused" or "暂停" in state
        if state:
            self.pill.set_state(state, tone_for_state(state, flag or phase))
        if detail:
            self.detail.setText(detail)
        if flag in ("attention", "blocked"):
            self.attention = True
            self.set_attention(True)
        elif flag != "paused" and self.attention:
            self.set_attention_state(False)
        if phase == "done" or "订单确认" in state or "下单成功" in state:
            self.steps.set_phase("done")
            self.set_tone("success")
        if "准备就绪" in state:
            self.pending = False
            self.running = False

    def _on_step(self, ev: dict) -> None:
        phase = str(ev.get("phase") or "")
        if ev.get("done"):
            self.steps.mark_done(phase)
        else:
            self.steps.set_phase(phase)
        if phase:
            self.phase = phase

    def _on_log(self, ev: dict) -> None:
        return  # logs go to the record drawer / timeline (main window)

    def _on_lanes(self, ev: dict) -> None:
        self.lanes.set_lanes(ev.get("lanes") or {k: ev.get(k) for k in ("total", "loading", "requests", "cooldown_s", "winner")})

    def _on_timing(self, ev: dict) -> None:
        # §12.5: new events carry phase + phase_ms; 1.5-style events only action + ms. Accept both (or both at once).
        phase, phase_ms = ev.get("phase"), ev.get("phase_ms")
        if phase and phase_ms is not None:
            self.speed.set_phase_ms(str(phase), phase_ms)
        action, ms = ev.get("action"), ev.get("ms")
        if action or ms is not None:
            text = f"{action or '操作'} → 确认 {int(ms)} ms" if ms is not None else str(action)
            self.timing.setText(text)
            self.timing.setObjectName("value")
            theme.repolish(self.timing)

    def _on_checks(self, ev: dict) -> None:
        self.checks.set_checks(ev.get("checks") or {})

    def _on_store(self, ev: dict) -> None:
        store = str(ev.get("store") or "")
        if store:
            self.place.setText(f"已选 {store}")

    def _on_review(self, ev: dict) -> None:
        valid = bool(ev.get("valid"))
        problems = [str(p) for p in (ev.get("problems") or [])]
        total = ev.get("total")
        self.review_ok = valid and self.running and not self.submitted
        if valid:
            amount = f" · 总价 ¥{total:,.0f}" if isinstance(total, (int, float)) else ""
            self.review_label.setText("通过" + amount + (" · 可提交" if not self.submitted else ""))
            self.review_label.setStyleSheet(f"color:{theme.SUCCESS}; font-weight:600;")
        else:
            first = problems[0] if problems else "订单摘要未通过核对"
            extra = f"（另 {len(problems) - 1} 项）" if len(problems) > 1 else ""
            self.review_label.setText("未通过：" + first + extra)
            self.review_label.setStyleSheet(f"color:{theme.DANGER};")
            self.review_label.setToolTip("\n".join(problems) or first)
        destination = ev.get("destination") or ev.get("store")
        if destination and self.operator == "delivery":
            self.place.setText(str(destination))

    def _on_attention(self, ev: dict) -> None:
        message = str(ev.get("message") or "需要人工处理")
        suggestion = str(ev.get("suggestion") or "")
        self.attention = True
        self.set_attention(True)
        self.steps.set_flag("attention")
        self.detail.setText(message + (f"  建议：{suggestion}" if suggestion else ""))
        # review=True only says the user may be asked to submit; the button itself follows operator.review.
        self.pill.set_state("待确认" if ev.get("review") else "请接管", "warning")
        self._show_suggestion(suggestion)
        self.needs_attention.emit(self.operator, "attention")

    def _show_suggestion(self, suggestion: str) -> None:
        """Turn a textual suggestion into a one-click button (login / show browser window)."""
        command, text = None, ""
        if "登录" in suggestion:
            command, text = "login", "登录账户"
        elif "显示浏览器窗口" in suggestion or "显示窗口" in suggestion:
            command, text = "show-window", "显示浏览器窗口"
        elif "重新弹出输入框" in suggestion or "重新输入" in suggestion:
            command, text = "code", "重新输入验证码"
        self._suggestion_command = command
        self.suggest_button.setText(text)
        self.suggest_button.setVisible(bool(command))

    def _on_otp(self, ev: dict) -> None:
        """The code itself is entered in the bottom-right notification card or the Windows notification
        (MainWindow owns both); the card only reflects the waiting state."""
        message = str(ev.get("message") or "")
        kind = str(ev.get("kind") or "sms")
        self.waiting_code = True
        if self._suggestion_command == "code":
            self.suggest_button.setVisible(False)
            self._suggestion_command = None
        self.pill.set_state("等待双重认证" if kind == "apple-2fa" else "等待验证码", "warning")
        self.detail.setText(message or ("请输入 Apple ID 双重认证验证码。" if kind == "apple-2fa"
                                        else "官网已发送短信验证码，请在右下角通知或 Windows 通知里输入。"))
        self.needs_attention.emit(self.operator, "otp")

    def _on_consent(self, ev: dict) -> None:
        if self.consent_dialog is not None:
            self.consent_dialog.close()
        dialog = ConsentDialog(self.window(), self.title, str(ev.get("request") or ""),
                               str(ev.get("url") or ""), str(ev.get("text") or ""))
        dialog.decided.connect(lambda request, accept: self._send("consent", {"request": request, "accept": accept}))
        dialog.finished.connect(lambda _c: setattr(self, "consent_dialog", None))
        self.consent_dialog = dialog
        present(dialog)
        self.pill.set_state("等待同意书确认", "warning")
        self.needs_attention.emit(self.operator, "consent")

    def _on_page(self, ev: dict) -> None:
        image = str(ev.get("image") or "")
        when = datetime.now(CHINA).strftime("%H:%M:%S")
        self.last_page = {"url": str(ev.get("url") or ""), "text": str(ev.get("text") or ""), "image": image, "when": when}
        if image:
            self.thumb.set_image(image)
        if self.preview_dialog is not None and self.preview_dialog.isVisible():
            self.preview_dialog.set_page(**self.last_page)

    def _on_network(self, ev: dict) -> None:
        status = ev.get("status")
        count = int(ev.get("count") or 1)
        if status is None:
            self.network_chip.setVisible(False)
            return
        tone = "danger" if str(status) in ("541", "403", "429") else "warning"
        self.network_chip.set_chip(f"HTTP {status} ×{count}", tone)
        self.network_chip.setToolTip(f"{ev.get('path') or ''}".strip() or f"官网返回 {status}")
        self.network_chip.setVisible(True)

    def _on_run(self, ev: dict) -> None:
        self.running, self.pending, self.paused = True, False, False
        self.review_ok = self.submitted = False
        self.trial = bool(ev.get("trial"))
        self.set_attention_state(False)
        self.set_tone("")
        self.steps.reset()
        self.steps.set_phase("waiting")
        self.checks.set_trial(self.trial)
        self.checks.set_checks({})
        self.lanes.set_lanes({"total": getattr(self.cfg, "entry_tabs", 3) or 3, "loading": 0, "requests": 0, "cooldown_s": 0})
        self.review_label.setText("待核对")
        self.review_label.setStyleSheet("")
        self.review_label.setToolTip("")
        self.timing.setText("—")
        self.network_chip.setVisible(False)
        self.trial_chip.setVisible(self.trial)
        self.suggest_button.setVisible(False)
        self._update_summary()
        target = str(ev.get("target") or "")
        self.speed.reset()
        self.speed.start(parse_time(target) or datetime.now(CHINA))
        self.ticker.start()
        self.pill.set_state("试跑中" if self.trial else "任务进行中", "info")
        if target:
            self.detail.setText(("官网试跑（iPhone Air）" if self.trial else "正式任务") + f" · 目标时间 {target[11:19] if len(target) >= 19 else target}")

    def _on_finished(self, ev: dict) -> None:
        success = bool(ev.get("success"))
        self.running = self.pending = self.paused = False
        self.review_ok = False
        self.speed.stop()
        self.ticker.stop()
        self.waiting_code = False
        if self.consent_dialog is not None:
            self.consent_dialog.reject()
        if success:
            self.submitted = self.submitted or not self.trial
            self.steps.set_phase("done")
            self.steps.set_flag("")
            self.set_tone("success")
            self.set_attention_state(False)
            if self.pill.tone != "success":
                self.pill.set_state("已完成" if not self.trial else "试跑完成", "success")
        reason = str(ev.get("reason") or "")
        if reason and not re.fullmatch(r"[a-z-]+", reason):
            # Machine codes ("complete", "stopped", …) must not replace the operator's own message.
            self.detail.setText(reason)

    def _on_error(self, ev: dict) -> None:
        self.running = self.pending = False
        self.review_ok = False
        self.speed.stop()
        self.ticker.stop()
        self.pill.set_state("出错", "danger")
        self.steps.set_flag("failed")
        self.detail.setText(str(ev.get("message") or "发生不可恢复错误"))
        self.set_attention(True)

    # ---- status() sync ------------------------------------------------------------------
    def sync(self, status: dict | None) -> None:
        """Mirror Dispatcher.status()['operators'][op]; events give the detail, this gives the truth."""
        if not status:
            return
        self.running = bool(status.get("running", self.running))
        self.pending = bool(status.get("pending", self.pending))
        self.paused = bool(status.get("paused", self.paused))
        self.trial = bool(status.get("trial", self.trial))
        self.submitted = bool(status.get("submitted", self.submitted))
        self.review_ok = bool(status.get("review_ok", self.review_ok)) and self.running and not self.submitted
        state, detail, phase = status.get("state"), status.get("detail"), status.get("phase")
        if state and not self.running and not self.pending and self.pill.tone in ("neutral", "info") and state != self.pill.text():
            self.pill.set_state(str(state), tone_for_state(str(state), str(phase or "")))
        if detail and not self.attention and not self.running:
            self.detail.setText(str(detail))
        if phase and phase in ("idle", "waiting") and not self.running:
            self.steps.set_phase(str(phase))
        if status.get("lanes"):
            self.lanes.set_lanes(status["lanes"])
        if status.get("checks"):
            self.checks.set_checks(status["checks"])
        if status.get("store") and self.operator == "pickup":
            self.place.setText(f"已选 {status['store']}")
        if status.get("last_image") and not self.thumb.pixmap:
            self.thumb.set_image(str(status["last_image"]))
        if isinstance(status.get("timeline_ms"), dict) and status["timeline_ms"]:
            self.speed.set_timeline(status["timeline_ms"])
        if "healthy" in status:
            self.set_health(bool(status.get("healthy", True)))
        if status.get("browser_mode"):
            self.set_browser_mode(str(status["browser_mode"]))
        if not self.running and self.speed.running:
            self.speed.stop()
            self.ticker.stop()
        self.trial_chip.setVisible(self.trial and (self.running or self.pending))
        self._refresh_buttons()

    # ---- helpers --------------------------------------------------------------------------
    def set_attention_state(self, on: bool) -> None:
        self.attention = on
        self.set_attention(on)
        if not on:
            self.suggest_button.setVisible(False)
            if self.steps.flag in ("attention", "blocked"):
                self.steps.set_flag("")

    def set_pending(self, on: bool, text: str = "") -> None:
        self.pending = on
        if on:
            self.pill.set_state("准备中", "info")
            if text:
                self.detail.setText(text)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        active = self.running or self.pending
        self.pause_button.setEnabled(self.running and not self.paused)
        self.resume_button.setEnabled(self.running and (self.paused or self.attention))
        self.show_button.setEnabled(True)
        self.submit_button.setEnabled(bool(self.running and self.review_ok and not self.submitted))
        self.submit_button.setText("已提交，请在官网核对" if self.submitted else "提交已核对订单")
        for key in ("prepare", "arm", "now", "trial", "recover", "reset-record"):
            self.actions[key].setEnabled(not active)
        for key in ("restart", "login", "show-window", "hide-window"):
            self.actions[key].setEnabled(True)
        self.actions["stop"].setEnabled(active)

    def close_dialogs(self) -> None:
        for dialog in (self.consent_dialog, self.preview_dialog, self.login_dialog):
            if dialog is not None:
                dialog.close()

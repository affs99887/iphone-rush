"""Dashboard window: top bar, dispatcher column, two operator cards, collapsible record drawer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from duo.model import FULFILLMENTS, Config

from . import APP_NAME, APP_VERSION, OFFSCREEN_POS, is_quiet, theme
from .app_icon import app_icon
from .dialogs import confirm, inform, present
from .dispatcher_panel import BROWSER_MODES, DispatcherPanel
from .notifications import CodeNotification, NotificationCenter
from .operator_card import OperatorCard
from .settings_dialog import SettingsDialog
from .widgets import PHASE_STEP, STEP_TITLES, Banner, CountdownPill, ElidedLabel, button, fmt_ms, label, tone_for_state

CHINA = timezone(timedelta(hours=8))
DEFAULT_TARGET = datetime(2026, 10, 16, 20, 0, tzinfo=CHINA)
OPERATOR_TAGS = {"pickup": "自取", "delivery": "送货", None: "指派"}
ACCEPTED_CLOSE_MS = 2500


class RecordDrawer(QFrame):
    """Bottom collapsible 记录 drawer (monospace, already-masked engine text)."""

    expanded_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.soft_shadow(self)
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 6, 14, 6)
        box.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.toggle = QToolButton()
        self.toggle.setText("记录")
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setToolTip("展开 / 收起运行记录")
        head.addWidget(self.toggle)
        self.preview = ElidedLabel("", "muted")
        head.addWidget(self.preview, 1)
        self.count = label("0 条", "muted")
        head.addWidget(self.count)
        self.copy_button = button("复制", "compact", tip="复制全部记录到剪贴板")
        self.clear_button = button("清空", "compact")
        head.addWidget(self.copy_button)
        head.addWidget(self.clear_button)
        box.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setObjectName("log")
        self.view.setReadOnly(True)
        self.view.document().setMaximumBlockCount(2000)
        self.view.setFixedHeight(160)
        self.view.setVisible(False)
        box.addWidget(self.view)
        self.lines = 0
        self.head_widgets = (self.toggle, self.preview, self.count, self.copy_button, self.clear_button)
        self.toggle.clicked.connect(lambda: self.set_expanded(not self.view.isVisible()))
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.view.toPlainText()))
        self.clear_button.clicked.connect(self.clear)

    def collapsed_height(self) -> int:
        margins = self.layout().contentsMargins()
        head = max(w.sizeHint().height() for w in self.head_widgets)
        return margins.top() + margins.bottom() + head

    def set_expanded(self, expanded: bool) -> None:
        self.view.setVisible(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.expanded_changed.emit(expanded)

    def append(self, text: str, tag: str = "", when: datetime | None = None) -> None:
        when = when or datetime.now(CHINA)
        line = f"{when.strftime('%H:%M:%S')}  {tag:<3} {text}"
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        self.view.insertPlainText(line + "\n")
        self.view.ensureCursorVisible()
        self.lines += 1
        self.count.setText(f"{self.lines} 条")
        self.preview.setText(f"最近：{when.strftime('%H:%M:%S')} {tag} {text}")

    def clear(self) -> None:
        self.view.clear()
        self.lines = 0
        self.count.setText("0 条")
        self.preview.setText("")


class MainWindow(QMainWindow):
    """Consumes `dispatcher.event` dicts (brief §4) and drives the dispatcher (brief §3)."""

    def __init__(self, dispatcher, testing: bool = False, banner: str = ""):
        super().__init__()
        self.dispatcher = dispatcher
        self.testing = testing
        self.closing = False
        self.settings_dialog: SettingsDialog | None = None
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.resize(1240, 820)
        self.setMinimumSize(1080, 720)
        if is_quiet():
            # §14.3: self-checks must not steal focus or appear on the user's desktop.
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.move(*OFFSCREEN_POS)
        self._build(banner)
        self._connect()
        self._seed()
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_status)
        self.sync_timer.start(1000)

    # ---- layout ----------------------------------------------------------------------------
    def _build(self, banner: str) -> None:
        base = QWidget()
        base.setObjectName("base")
        self.setCentralWidget(base)
        root = QVBoxLayout(base)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(12)
        # top bar
        self.top_bar = QWidget()
        self.top_bar.setObjectName("transparent")
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(34, 34))
        logo.setFixedSize(34, 34)
        top.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(label(APP_NAME, "appTitle"))
        title_row.addWidget(label(APP_VERSION, "versionTag"))
        title_row.addStretch(1)
        titles.addLayout(title_row)
        titles.addWidget(label("指派员 + 两路操作员 · 独立后台浏览器 · 门店自取与送货到家并行", "muted"))
        top.addLayout(titles)
        top.addStretch(1)
        self.countdown = CountdownPill(self._countdown_target())
        top.addWidget(self.countdown)
        top.addStretch(1)
        self.trial_button = button("官网试跑", tip="用在售 iPhone Air 走真实官网流程，到提交前强制停止")
        self.settings_button = button("设置", tip="联系人与预约 / 门店策略 / 送货地址 / 运行与浏览器")
        self.primary_button = button("一键布防", "primary", tip="预热浏览器并按开售时间自动开始两路任务")
        self.primary_button.setMinimumWidth(118)
        for b in (self.trial_button, self.settings_button, self.primary_button):
            b.setMinimumHeight(34)
            top.addWidget(b)
        root.addWidget(self.top_bar)
        self.banner = Banner(banner)
        root.addWidget(self.banner)
        # body
        body = QHBoxLayout()
        body.setSpacing(16)
        self.panel = DispatcherPanel()
        body.addWidget(self.panel)
        self.cards: dict[str, OperatorCard] = {}
        for op in self._operators():
            card = OperatorCard(op)
            self.cards[op] = card
            body.addWidget(card, 1)
        root.addLayout(body, 1)
        # drawer
        self.drawer = RecordDrawer()
        root.addWidget(self.drawer)
        # Verification codes: bottom-right in-app cards (never auto-close) + an interactive Windows notification.
        self.notices = NotificationCenter(self, anchor=self.drawer, animate=not is_quiet())
        self.toasts = None
        self.code_seq = 0
        self.awaiting_accept: dict[str, str] = {}      # operator → card key whose code was just submitted

    def attach_toasts(self, toasts) -> None:
        """duo.winnotify.WindowsCodeToast (or None to disable Windows notifications)."""
        self.toasts = toasts
        if toasts is not None:
            toasts.code_entered.connect(self._toast_code)
            toasts.open_requested.connect(self._toast_open)
            toasts.helper_event.connect(self._toast_event)

    def _connect(self) -> None:
        self.dispatcher.event.connect(self.on_event)
        self.primary_button.clicked.connect(self.primary_action)
        self.trial_button.clicked.connect(self.start_trial)
        self.settings_button.clicked.connect(self.open_settings)
        self.panel.policy_changed.connect(self.change_policy)
        self.panel.browser_mode_changed.connect(self.change_browser_mode)
        self.panel.action.connect(self.panel_action)
        for card in self.cards.values():
            card.command.connect(self.send_command)
            card.needs_attention.connect(self.on_needs_attention)
        self.drawer.expanded_changed.connect(lambda _on: (self._fit_drawer(), QTimer.singleShot(0, self._fit_drawer)))

    def _fit_drawer(self) -> None:
        """Size the expanded record view to the free vertical space so the cards never get squeezed."""
        if not self.drawer.view.isVisible():
            return
        root = self.centralWidget().layout()
        margins = root.contentsMargins()
        spacing = root.spacing()
        body_min = max(w.minimumSizeHint().height() for w in (self.panel, *self.cards.values()))
        fixed = margins.top() + margins.bottom() + self.top_bar.sizeHint().height() + spacing + body_min + spacing
        if self.banner.isVisible():
            fixed += self.banner.sizeHint().height() + spacing
        fixed += self.drawer.collapsed_height() + self.drawer.layout().spacing()
        slack = self.centralWidget().height() - fixed
        self.drawer.view.setFixedHeight(max(48, min(220, slack)))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_drawer()

    def _seed(self) -> None:
        for op, card in self.cards.items():
            card.set_config(self._config(op))
        self.sync_status(full=True)
        self.panel.add_timeline("指派员已就绪。一键布防后到点自动开始；两路互不干扰。", "指派", "neutral")
        self.drawer.append("软件已启动；记录已脱敏，不含验证码、卡号或证件号。", "系统")
        if self.cards.get("pickup"):
            self.drawer.append("取货需携带身份证原件；软件不保存身份证号。", "自取")

    # ---- dispatcher helpers --------------------------------------------------------------------
    def _operators(self) -> list[str]:
        try:
            ops = list(self.dispatcher.operators())
        except Exception:
            ops = []
        return ops or list(FULFILLMENTS)

    def _config(self, op: str) -> Config | None:
        try:
            return self.dispatcher.config(op)
        except Exception:
            return None

    def _countdown_target(self) -> datetime:
        try:
            plan = self.dispatcher.plan()
            starts = getattr(plan, "starts_at", None)
            if isinstance(starts, str):
                starts = datetime.strptime(starts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
            if isinstance(starts, datetime):
                return starts if starts.tzinfo else starts.replace(tzinfo=CHINA)
        except Exception:
            pass
        cfg = self._config("pickup")
        try:
            return cfg.start_time() if cfg else DEFAULT_TARGET
        except Exception:
            return DEFAULT_TARGET

    def _call(self, name: str, *args) -> bool:
        """Invoke a dispatcher method; engine errors become a dialog, never a traceback."""
        fn = getattr(self.dispatcher, name, None)
        if fn is None:
            inform(self, "指派员", f"当前引擎不支持“{name}”。")
            return False
        try:
            fn(*args)
            return True
        except Exception as exc:  # ValueError from the engine or anything unexpected
            self.drawer.append(f"{name} 失败：{exc}", "系统")
            inform(self, "指派员", str(exc) or "操作失败。")
            return False

    def is_busy(self) -> bool:
        try:
            if self.dispatcher.is_busy():
                return True
        except Exception:
            pass
        return self.panel.state not in ("idle",) or any(c.running or c.pending for c in self.cards.values())

    # ---- actions -----------------------------------------------------------------------------------
    def primary_action(self) -> None:
        if self.is_busy():
            if confirm(self, "停止全部", "停止两路任务？浏览器进度与防重复记录会保留。", yes="停止全部", danger=True):
                self._call("stop_all")
        else:
            if self._call("prepare"):
                self._call("arm")
                for card in self.cards.values():
                    card.set_pending(True, "正在预热浏览器并布防…")
        self.update_primary()

    def panel_action(self, name: str) -> None:
        if name == "prepare":
            if self._call("prepare"):
                for card in self.cards.values():
                    card.set_pending(True, "正在启动浏览器、打开商品页并校准时钟…")
        elif name == "start_now":
            if confirm(self, "立即开始", "不等开售时间，两路立即开始正式任务？", yes="立即开始"):
                self._call("start_now")
        self.update_primary()

    def start_trial(self) -> None:
        auto = all(self._config(op).auto_submit for op in FULFILLMENTS)
        ending = ("每路核对通过后自动提交一张测试订单（会收到邮件，不付款会被官网自动取消）。" if auto else
                  "开启了自动提交的一路核对通过后自动提交测试订单，其余到核对页等你点“提交已核对订单”。")
        if confirm(self, "官网试跑", "门店自取、送货到家两路同时用在售 iPhone Air 云白色 1TB + AppleCare+ 走真实官网流程。\n"
                                     + ending + "上次试跑留下的测试商品会自动清空。", yes="开始试跑"):
            self._call("trial")
            self.update_primary()

    def send_command(self, op: str, name: str, payload) -> None:
        self._call("command", op, name, payload)
        if name in ("prepare", "arm", "now", "trial", "trial-retry") and op in self.cards:
            self.cards[op].set_pending(True, "正在准备本路浏览器…")
        self.update_primary()

    def change_policy(self, policy: str) -> None:
        if not self._call("set_policy", policy):
            self.sync_status(full=True)

    def change_browser_mode(self, mode: str) -> None:
        if self._call("set_browser_mode", mode):
            self.panel.add_timeline(f"浏览器模式：{BROWSER_MODES.get(mode, mode)}", "指派", "info")
        else:
            self.sync_status(full=True)

    def open_settings(self, modal: bool = True) -> SettingsDialog:
        configs = {op: self._config(op) or Config(fulfillment=op) for op in self.cards}
        dialog = SettingsDialog(self, configs, locked=self.is_busy())
        dialog.saved.connect(self.save_configs)
        self.settings_dialog = dialog
        if modal and not is_quiet():
            dialog.exec()
        else:
            present(dialog)
        return dialog

    def save_configs(self, configs: dict) -> None:
        failed = False
        for op, cfg in configs.items():
            try:
                self.dispatcher.save_config(op, cfg)
            except Exception as exc:
                failed = True
                inform(self, "保存设置", f"{FULFILLMENTS.get(op, op)}：{exc}", error=True)
                continue
            if op in self.cards:
                self.cards[op].set_config(cfg)
        self.countdown.set_target(self._countdown_target())
        if not failed:
            self.panel.add_timeline("设置已加密保存到本机。", "指派", "success")
            self.drawer.append("设置已保存。", "系统")

    # ---- verification codes (in-app cards + Windows notification) ---------------------------------
    def _code_flow(self, kind: str, op: str, event: dict) -> None:
        if kind == "operator.otp":
            self.show_code_request(op, event)
            return
        pending = self.awaiting_accept.get(op)
        card = self.notices.get(pending) if pending else None
        if card is None:
            if kind in ("operator.finished", "operator.error"):
                for stale in self.notices.of_operator(op):
                    if stale.state in ("waiting", "submitted"):
                        stale.mark_stale("本路任务已结束，这条验证码不再需要。")
                        self._cancel_toast(stale.key)
            return
        if kind == "operator.state":
            state = str(event.get("state") or "")
            if state in ("正在验证", "等待验证码", "等待双重认证", "检查页面", "等待官网处理"):
                return
            self.awaiting_accept.pop(op, None)
            if "接管" in state and "验证码" in str(event.get("detail") or ""):
                card.mark_error(str(event.get("detail") or "验证没有通过，请看本路卡片提示。"))
            else:
                card.mark_accepted()
                key = card.key
                # Verified: show "验证通过" briefly, then close the card by itself (user request 10-15).
                QTimer.singleShot(ACCEPTED_CLOSE_MS, lambda: self._close_accepted(key))
        elif kind in ("operator.finished", "operator.error"):
            self.awaiting_accept.pop(op, None)
            card.mark_stale("本路任务已结束。")

    def show_code_request(self, op: str, event: dict) -> CodeNotification:
        request = str(event.get("request") or "")
        kind = str(event.get("kind") or "sms")
        title = FULFILLMENTS.get(op, op)
        for old in self.notices.of_operator(op):
            if old.state in ("waiting", "error", "submitted") and old.request != request:
                old.mark_stale("已被新的验证码请求取代。")
                self._cancel_toast(old.key)
        self.awaiting_accept.pop(op, None)
        self.code_seq += 1
        key = f"{op}-{self.code_seq}"
        cfg = self._config(op)
        phone = getattr(cfg, "phone", "") or str(event.get("phone_tail") or "")
        card = CodeNotification(self, key, op, title, kind, request, phone,
                                str(event.get("message") or ""), bool(event.get("error")))
        card.code_submitted.connect(self._submit_code)
        card.close_requested.connect(self._close_notice)
        self.notices.add(card)
        if self.toasts is not None and not is_quiet():
            what = "Apple ID 双重认证" if kind == "apple-2fa" else "短信验证码"
            body = ("输入推送到受信任设备的 6 位验证码，然后点“提交验证码”。" if kind == "apple-2fa" else
                    f"输入发送至 {card.phone_display} 的 6 位验证码，然后点“提交验证码”。" if card.phone_display
                    else "输入收到的 6 位验证码，然后点“提交验证码”。")
            if event.get("error"):
                body = "上次的验证码不正确。" + body
            self.toasts.show_code_request(key, f"{title} · {what}", body)
        return card

    def _submit_code(self, key: str, code: str, via: str) -> None:
        card = self.notices.get(key)
        if card is None:
            return
        self._send_code(card, code, via)

    def _send_code(self, card: CodeNotification, code: str, via: str) -> None:
        """The single place a code leaves the UI: exactly once per request, whichever surface it came from."""
        if card.state != "waiting":
            return
        self.send_command(card.operator, "otp", {"request": card.request, "code": code, "kind": card.kind})
        card.mark_submitted(via)
        self.awaiting_accept[card.operator] = card.key
        if self.cards.get(card.operator):
            self.cards[card.operator].waiting_code = False
        self._cancel_toast(card.key)
        self.drawer.append(f"{card.operator_title}：验证码已提交（来自{ {'windows': 'Windows 通知', 'remote': '远程验收'}.get(via, '软件窗口') }）。",
                           OPERATOR_TAGS.get(card.operator, ""))

    def _close_accepted(self, key: str) -> None:
        card = self.notices.get(key)
        if card is not None and card.state == "accepted":
            self.notices.remove(key)

    def _close_notice(self, key: str) -> None:
        """One click on ×: the card closes. If its code was still awaited, that route pauses its verification
        (no code is sent) and its route card offers to bring the input back."""
        card = self.notices.get(key)
        self._cancel_toast(key)
        for op, pending in list(self.awaiting_accept.items()):
            if pending == key:
                self.awaiting_accept.pop(op, None)
        if card is not None and card.state == "waiting":
            self.send_command(card.operator, "otp", {"request": card.request, "code": "", "kind": card.kind})
            card.state = "paused"
            route = self.cards.get(card.operator)
            if route is not None:
                route.offer_code_reentry()
            self.drawer.append(f"{card.operator_title}：已关闭验证码通知，本路暂停验证；点本路卡片的“重新输入验证码”可继续。",
                               OPERATOR_TAGS.get(card.operator, ""))
        self.notices.remove(key)

    def _cancel_toast(self, key: str) -> None:
        if self.toasts is not None:
            self.toasts.cancel(key)

    def _toast_code(self, key: str, code: str) -> None:
        card = self.notices.get(key)
        if card is None:
            return
        if self.toasts is not None:
            self.toasts.forget(key)
        card.enter_code(code, "windows")

    def _toast_open(self, key: str) -> None:
        if self.toasts is not None:
            self.toasts.forget(key)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        card = self.notices.get(key)
        if card is not None and card.state == "waiting":
            card.code.setFocus()

    def _toast_event(self, key: str, data: dict) -> None:
        event = data.get("event")
        card = self.notices.get(key)
        if event == "invalid-code" and card is not None:
            card.flash_invalid("Windows 通知里输入的不是 6 位数字，已重新弹出通知；也可以在这里输入。")
        elif event in ("error", "failed"):
            self.drawer.append("Windows 通知不可用：" + str(data.get("message") or data.get("error") or "")
                               + "；请在右下角通知里输入验证码。", "系统")
        elif event == "shown" and not data.get("delivered"):
            self.drawer.append("Windows 通知可能被系统设置关闭（" + str(data.get("setting") or "") + "）；"
                               "请在右下角通知里输入验证码。", "系统")

    def on_needs_attention(self, op: str, kind: str) -> None:
        if is_quiet():
            return  # §14.3: no taskbar flash, no sound during self-checks
        if not self.isActiveWindow():
            QApplication.alert(self, 0)
        QApplication.beep()

    # ---- status sync -----------------------------------------------------------------------------
    def sync_status(self, full: bool = False) -> None:
        try:
            status = self.dispatcher.status() or {}
        except Exception as exc:
            self.drawer.append(f"读取状态失败：{exc}", "系统")
            return
        if full:
            self.panel.set_status(status)
        else:
            state = str(status.get("state") or "idle")
            if state != self.panel.state:
                self.panel.set_state(state)
            owner = status.get("owner")
            if owner and FULFILLMENTS.get(owner, owner) not in self.panel.owner.text():
                self.panel.set_policy(status.get("policy"), status.get("single"), status.get("limit"), owner)
        if status.get("clock_offset_ms") is not None:
            self.countdown.set_offset_ms(int(status["clock_offset_ms"]))
            for card in self.cards.values():
                card.set_clock_offset(int(status["clock_offset_ms"]))
        for op, info in (status.get("operators") or {}).items():
            if op in self.cards and isinstance(info, dict):
                self.cards[op].sync(info)
        self.update_primary()

    def update_primary(self) -> None:
        busy = self.is_busy()
        self.primary_button.setText("停止全部" if busy else "一键布防")
        self.primary_button.setObjectName("danger" if busy else "primary")
        self.primary_button.setToolTip("停止两路任务；进度与防重复记录保留" if busy else "预热浏览器并按开售时间自动开始两路任务")
        theme.repolish(self.primary_button)
        self.trial_button.setEnabled(not busy)
        self.settings_button.setToolTip("任务进行中已锁定设置（可查看）" if busy else "联系人与预约 / 门店策略 / 送货地址 / 运行与浏览器")
        self.panel.set_locked(busy)
        if self.settings_dialog is not None and self.settings_dialog.isVisible():
            self.settings_dialog.set_locked(busy)

    # ---- events ----------------------------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        op = event.get("operator")
        tag = OPERATOR_TAGS.get(op, str(op or "指派"))
        when = _event_time(event)
        if kind.startswith("dispatcher."):
            self.panel.apply(event)
            if kind == "dispatcher.browser-mode":
                mode, reason = str(event.get("mode") or ""), str(event.get("reason") or "")
                if op in self.cards:
                    self.cards[op].set_browser_mode(mode, reason)
                else:
                    self.panel.set_browser_mode(mode)
            elif kind == "dispatcher.health" and op in self.cards:
                self.cards[op].set_health(bool(event.get("healthy", True)), str(event.get("reason") or ""),
                                          str(event.get("action") or "none"))
            elif kind == "dispatcher.clock":
                offset = event.get("offset_ms")
                self.countdown.set_offset_ms(int(offset) if offset is not None else None)
                for card in self.cards.values():
                    card.set_clock_offset(int(offset) if offset is not None else None)
        elif kind.startswith("operator.") and op in self.cards:
            self.cards[op].apply(event)
            self._code_flow(kind, op, event)
        self._record(kind, event, tag, when)
        self.update_primary()

    def _record(self, kind: str, ev: dict, tag: str, when: datetime) -> None:
        """Timeline (meaningful events) + record drawer (everything, compact)."""
        text, tone, drawer_only = "", "info", False
        op_title = FULFILLMENTS.get(ev.get("operator"), "")
        if kind == "dispatcher.state":
            titles = {"idle": "空闲", "preparing": "预热中", "armed": "已布防", "running": "运行中", "stopping": "停止中"}
            text = ev.get("message") or f"指派员状态：{titles.get(ev.get('state'), ev.get('state'))}"
            tone = "neutral" if ev.get("state") == "idle" else "info"
        elif kind == "dispatcher.policy":
            owner = ev.get("owner")
            if owner:
                text = f"{FULFILLMENTS.get(owner, owner)} 取得唯一提交权。" + (ev.get("message") or "")
                tone = "success"
            elif ev.get("mode") == "sequential-account":
                text = "官网要求登录：切换为同账号顺序下单。" + (ev.get("message") or "")
                tone = "warning"
            else:
                text = ev.get("message") or ("已识别官网限购" if ev.get("limit") else "")
                tone = "warning" if ev.get("limit") else "info"
            drawer_only = not text
        elif kind == "dispatcher.clock":
            offset = ev.get("offset_ms")
            if offset is None:
                text = "时钟校准失败，使用本机时钟。"
                tone = "warning"
            else:
                direction = "快" if offset > 0 else "慢"
                text = f"时钟校准：官网比本机{direction} {abs(int(offset))} ms" + (f"（{ev.get('source')}）" if ev.get("source") else "")
        elif kind == "dispatcher.health":
            action = {"soft-restart": "软重启", "hard-restart": "硬重启", "none": ""}.get(ev.get("action"), "")
            if ev.get("healthy", True):
                text = f"{op_title}：心跳恢复正常" if ev.get("reason") == "" and action == "" else f"{op_title}：{ev.get('reason') or '健康'}"
                tone, drawer_only = "success", not op_title
            else:
                text = f"{op_title}：{ev.get('reason') or '无心跳'}" + (f" → {action}" if action else "")
                tone = "warning"
        elif kind == "dispatcher.browser-mode":
            mode = {"headless": "无头", "hidden": "隐藏窗口", "auto": "自动"}.get(ev.get("mode"), ev.get("mode"))
            text = (f"{op_title}：" if op_title else "") + f"浏览器模式切换为{mode}" + (f"（{ev.get('reason')}）" if ev.get("reason") else "")
            tone = "warning" if op_title else "info"
        elif kind == "operator.state":
            state, detail = str(ev.get("state") or ""), str(ev.get("detail") or "")
            text = state + (f" · {detail}" if detail else "")
            tone = tone_for_state(state, str(ev.get("phase") or ""))
        elif kind == "operator.step":
            index = PHASE_STEP.get(str(ev.get("phase") or ""), -1)
            title = STEP_TITLES[index] if 0 <= index < len(STEP_TITLES) else str(ev.get("phase") or "")
            text = f"步骤{'完成' if ev.get('done') else '开始'}：{title}"
            drawer_only = True
        elif kind == "operator.log":
            level = str(ev.get("level") or "info")
            text = str(ev.get("message") or "")
            tone = {"warn": "warning", "warning": "warning", "error": "danger"}.get(level, "info")
            drawer_only = level == "info"
        elif kind == "operator.lanes":
            winner = ev.get("winner") or (ev.get("lanes") or {}).get("winner")
            text = f"入口 {winner} 已接管，其余入口已关闭" if winner else f"入口竞速：{ev.get('loading')} 个加载中 · 请求 {ev.get('requests')}"
            tone, drawer_only = "success", not winner
        elif kind == "operator.timing":
            parts = []
            if ev.get("phase") and ev.get("phase_ms") is not None:
                parts.append(f"阶段 {ev.get('phase')} 用时 {fmt_ms(ev.get('phase_ms'))}")
            if ev.get("action") or ev.get("ms") is not None:
                parts.append(f"{ev.get('action') or '操作'} → 确认 {ev.get('ms')} ms")
            text = " · ".join(parts)
            drawer_only = True
        elif kind == "operator.checks":
            checks = ev.get("checks") or {}
            ok = all(checks.get(k) for k in ("color", "capacity", "trade", "care"))
            text = "选配四项全部核对通过" if ok else "选配核对：" + " ".join(f"{k}={'✓' if v else '○'}" for k, v in checks.items())
            tone, drawer_only = "success", not ok
        elif kind == "operator.store":
            text, tone = f"已选门店：{ev.get('store')}", "success"
        elif kind == "operator.review":
            if ev.get("valid"):
                total = ev.get("total")
                text = "订单核对通过" + (f" · 总价 ¥{total:,.0f}" if isinstance(total, (int, float)) else "")
                tone = "success"
            else:
                problems = ev.get("problems") or []
                text = "订单核对未通过：" + ("；".join(str(p) for p in problems) if problems else "摘要无法确认")
                tone = "warning"
        elif kind == "operator.attention":
            text = str(ev.get("message") or "需要人工处理") + (f" 建议：{ev.get('suggestion')}" if ev.get("suggestion") else "")
            tone = "warning"
        elif kind == "operator.otp":
            tail = ev.get("phone_tail") or ""
            what = "Apple ID 双重认证验证码" if ev.get("kind") == "apple-2fa" else "短信验证码"
            text = (f"{what}不正确，请重新输入" if ev.get("error") else f"等待{what}") + (f"（尾号 {tail}）" if tail and ev.get("kind") != "apple-2fa" else "")
            tone = "warning"
        elif kind == "operator.consent":
            text, tone = "等待你确认 Apple 隐私知情同意书", "warning"
        elif kind == "operator.page":
            text, drawer_only = f"页面预览已更新 {ev.get('url') or ''}", True
        elif kind == "operator.network":
            text = f"官网返回 HTTP {ev.get('status')} ×{ev.get('count') or 1} {ev.get('path') or ''}".rstrip()
            tone = "danger" if str(ev.get("status")) in ("541", "403", "429") else "warning"
        elif kind == "operator.run":
            target = str(ev.get("target") or "")
            text = ("官网试跑开始（iPhone Air，提交前停止）" if ev.get("trial") else "正式任务开始") + (f" · 目标 {target[11:19]}" if len(target) >= 19 else "")
        elif kind == "operator.finished":
            text = ("任务完成：" if ev.get("success") else "任务结束：") + str(ev.get("reason") or "")
            tone = "success" if ev.get("success") else "neutral"
        elif kind == "operator.error":
            text, tone = str(ev.get("message") or "不可恢复错误"), "danger"
        else:
            text, drawer_only = f"{kind} {ev}", True
        if not text:
            return
        self.drawer.append(text if len(text) < 300 else text[:300] + "…", tag, when)
        if not drawer_only:
            self.panel.add_timeline(text, tag, tone)

    # ---- window ------------------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802
        if self.closing:
            event.accept()
            return
        if self.is_busy() and not self.testing:
            if not confirm(self, "退出软件", "退出会停止两路任务并关闭专用浏览器，确定退出？", yes="退出并停止", danger=True):
                event.ignore()
                return
        self.closing = True
        self.sync_timer.stop()
        self.countdown.timer.stop()
        for card in self.cards.values():
            card.ticker.stop()
            card.close_dialogs()
        if self.settings_dialog is not None:
            self.settings_dialog.close()
        if self.toasts is not None:
            self.toasts.shutdown()
        try:
            self.dispatcher.shutdown()
        except Exception as exc:
            self.drawer.append(f"关闭引擎时出错：{exc}", "系统")
        event.accept()


def _event_time(event: dict) -> datetime:
    raw = event.get("time")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).astimezone(CHINA)
        except ValueError:
            pass
    return datetime.now(CHINA)

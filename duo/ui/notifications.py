"""In-app notification cards, bottom-right of the main window.

Rules (user request 2026-10-15): a waiting card never closes on its own (once the code is verified it
closes by itself after a short "验证通过"), and any card closes with one click on ×; the newest
card is at the bottom and older cards slide up; verification-code cards say which route and which phone the
code is for and accept the code directly (six digits auto-submit, Enter submits). Closing a card that is
still waiting pauses that route's verification; the route card then offers "重新输入验证码".
Animations: slide-in with a soft overshoot, smooth restacking, fade-out, a breathing status dot while
waiting and a shake on a wrong entry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject, QParallelAnimationGroup, QPoint,
                            QPropertyAnimation, QRectF, QSequentialAnimationGroup, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import theme
from .dialogs import CodeInput

CHINA = timezone(timedelta(hours=8))
SHADOW = 14                 # painted shadow margin around the card surface
CARD_WIDTH = 372            # visible surface width
RADIUS = 14
MARGIN_RIGHT, MARGIN_BOTTOM, GAP = 18, 8, 10
KIND_TITLES = {"sms": "短信验证码", "apple-2fa": "Apple ID 双重认证"}
DOT_COLORS = {"waiting": theme.WARNING, "submitted": theme.PRIMARY, "accepted": theme.SUCCESS,
              "error": theme.DANGER, "stale": theme.FAINT, "paused": theme.FAINT}
SUBMITTED_VIA = {"window": "软件窗口", "windows": "Windows 通知", "remote": "远程验收"}


def now_text() -> str:
    return datetime.now(CHINA).strftime("%H:%M")


def mask_phone(phone: str) -> str:
    digits = "".join(c for c in phone or "" if c.isdigit())
    return f"{digits[:3]}****{digits[-4:]}" if len(digits) == 11 else (f"尾号 {digits[-4:]}" if digits else "")


class CloseButton(QAbstractButton):
    """A painted × with a round hover background: crisp at any DPI, never swallowed by QSS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("关闭")
        self.setAccessibleName("关闭通知")

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(26, 26)

    def enterEvent(self, event):  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.BORDER if self.isDown() else theme.NEUTRAL_SOFT))
            painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        pen = QPen(QColor(theme.TEXT if self.underMouse() else theme.MUTED), 1.6, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        c, r = QRectF(self.rect()).center(), 4.2
        painter.drawLine(c.x() - r, c.y() - r, c.x() + r, c.y() + r)
        painter.drawLine(c.x() - r, c.y() + r, c.x() + r, c.y() - r)
        painter.end()


class StatusDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.color = QColor(theme.WARNING)
        self.alpha = 1.0

    def set_color(self, color: str) -> None:
        self.color = QColor(color)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.color)
        color.setAlphaF(max(0.25, min(1.0, self.alpha)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(1, 1, 8, 8))
        painter.end()


def _text(text: str, size: int, color: str, weight: int = 400, wrap: bool = True) -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setStyleSheet(f"QLabel {{ color:{color}; font-size:{size}px; font-weight:{weight}; background:transparent; }}")
    return widget


class CodeNotification(QWidget):
    """A verification-code card. Signals carry the card key; the window decides what to send."""

    code_submitted = Signal(str, str, str)   # key, code, via
    close_requested = Signal(str)            # key (one click on ×)
    resized = Signal()

    def __init__(self, parent: QWidget, key: str, operator: str, operator_title: str, kind: str, request: str,
                 phone: str = "", message: str = "", error: bool = False, valid_seconds: int = 600):
        super().__init__(parent)
        self.key, self.operator, self.operator_title = key, operator, operator_title
        self.kind = kind if kind in KIND_TITLES else "sms"
        self.request = request
        self.phone_display = mask_phone(phone)
        self.state = "waiting"
        self.valid_total = max(1, int(valid_seconds))
        self.created = datetime.now(CHINA)
        self._submitted = False
        self.enter_slide: QPropertyAnimation | None = None
        self.setObjectName("notification")
        self.setFixedWidth(CARD_WIDTH + 2 * SHADOW)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._build(error)
        self.opacity = QGraphicsOpacityEffect(self)
        self.opacity.setOpacity(1.0)
        self.opacity.setEnabled(False)
        self.setGraphicsEffect(self.opacity)
        self.breath = QVariantAnimation(self)
        self.breath.setStartValue(1.0)
        self.breath.setKeyValueAt(0.5, 0.3)
        self.breath.setEndValue(1.0)
        self.breath.setDuration(1500)
        self.breath.setLoopCount(-1)
        self.breath.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.breath.valueChanged.connect(self._breathe)
        self.breath.start()
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self._tick()
        self.adjustSize()

    # ---- layout ---------------------------------------------------------------------------
    def _build(self, error: bool) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(SHADOW + 18, SHADOW + 14, SHADOW + 12, SHADOW + 16)
        box.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.dot = StatusDot()
        head.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = _text(f"{self.operator_title} · {KIND_TITLES[self.kind]}", 14, theme.TEXT, 600, wrap=False)
        head.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)
        self.time_label = _text(now_text(), 12, theme.FAINT, wrap=False)
        head.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close_button = CloseButton()
        self.close_button.clicked.connect(lambda: self.close_requested.emit(self.key))
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(head)
        box.addSpacing(4)
        if self.kind == "apple-2fa":
            purpose = "登录 Apple ID 的 6 位验证码，已推送到你的受信任设备"
        else:
            purpose = f"发送至 {self.phone_display} 的 6 位验证码" if self.phone_display else "官网刚发送的 6 位短信验证码"
        self.purpose = _text(purpose, 12, theme.MUTED)
        self.purpose.setContentsMargins(18, 0, 0, 0)
        box.addWidget(self.purpose)
        self.warning = _text("上一次的验证码不正确，请输入新收到的验证码。", 12, theme.DANGER)
        self.warning.setContentsMargins(18, 4, 0, 0)
        self.warning.setVisible(bool(error))
        box.addWidget(self.warning)
        box.addSpacing(12)
        self.code_wrap = QWidget()
        self.code_layout = QHBoxLayout(self.code_wrap)
        self.code_layout.setContentsMargins(0, 0, 6, 0)
        self.code_layout.addStretch(1)
        self.code = CodeInput(self.code_wrap)
        self.code_layout.addWidget(self.code)
        self.code_layout.addStretch(1)
        box.addWidget(self.code_wrap)
        self.code.textChanged.connect(self._code_changed)
        self.code.returnPressed.connect(lambda: self._submit("window"))
        box.addSpacing(10)
        self.status = _text("", 12, theme.FAINT)
        self.status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(self.status)
        self._set_status_text()

    # ---- painting -------------------------------------------------------------------------
    def surface_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(SHADOW, SHADOW - 3, -SHADOW, -SHADOW - 3)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        surface = self.surface_rect()
        for i in range(SHADOW, 0, -1):                    # layers accumulate: keep each one faint
            alpha = round(6 * (1 - i / (SHADOW + 1)) ** 1.4)
            if alpha:
                path = QPainterPath()
                path.addRoundedRect(surface.adjusted(-i, -i + 4, i, i + 4), RADIUS + i, RADIUS + i)
                painter.fillPath(path, QColor(0, 0, 0, alpha))
        path = QPainterPath()
        path.addRoundedRect(surface, RADIUS, RADIUS)
        painter.fillPath(path, QColor(theme.CARD))
        painter.setPen(QPen(QColor(0, 0, 0, 22), 1.0))
        painter.drawPath(path)
        painter.end()

    def _breathe(self, value) -> None:
        self.dot.alpha = float(value)
        self.dot.update()

    # ---- behaviour ------------------------------------------------------------------------
    def remaining_seconds(self) -> int:
        return max(0, self.valid_total - int((datetime.now(CHINA) - self.created).total_seconds()))

    def _tick(self) -> None:
        if self.state == "waiting":
            self._set_status_text()

    def _set_status_text(self) -> None:
        if self.state != "waiting":
            return
        left = self.remaining_seconds()
        if left:
            minutes, seconds = divmod(left, 60)
            self.status.setText(f"输满 6 位自动提交 · 剩余 {minutes:02}:{seconds:02}")
        else:
            self.status.setText("验证码可能已过期，可在本路卡片点“继续”重新发送")

    def _code_changed(self, text: str) -> None:
        if len(text) == 6 and self.state == "waiting":
            QTimer.singleShot(0, lambda: self._submit("window"))

    def _submit(self, via: str) -> None:
        if self._submitted or self.state != "waiting":
            return
        code = self.code.text()
        if len(code) != 6 or not code.isdigit():
            self.flash_invalid("需要 6 位数字")
            return
        self._submitted = True
        self.code_submitted.emit(self.key, code, via)
        code = ""

    def enter_code(self, code: str, via: str) -> None:
        """Code typed elsewhere (Windows notification / remote run): fill the cells and submit once."""
        if self.state != "waiting":
            return
        self.code.blockSignals(True)
        self.code.setText(code)
        self.code.blockSignals(False)
        self._submit(via)

    def mark_submitted(self, via: str = "window") -> None:
        self._set_state("submitted", f"已提交（{SUBMITTED_VIA.get(via, via)}），等待官网确认…", theme.PRIMARY)
        self.code.clear()

    def mark_accepted(self) -> None:
        self._set_state("accepted", "验证通过，本路已继续", theme.SUCCESS)

    def mark_error(self, text: str) -> None:
        self._set_state("error", text, theme.DANGER)
        self.shake()

    def mark_stale(self, text: str) -> None:
        self._set_state("stale", text, theme.FAINT)
        self.code.clear()

    def mark_paused(self) -> None:
        self._set_state("paused", "已暂停验证", theme.FAINT)
        self.code.clear()

    def flash_invalid(self, text: str = "需要 6 位数字") -> None:
        self.warning.setText(text)
        self.warning.setVisible(True)
        self._submitted = False
        self.shake()
        self._relayout()

    def _set_state(self, state: str, text: str, color: str) -> None:
        self.state = state
        self.breath.stop()
        self.clock.stop()
        self.dot.alpha = 1.0
        self.dot.set_color(DOT_COLORS.get(state, theme.FAINT))
        self.warning.setVisible(False)
        self.code_wrap.setVisible(False)
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.status.setContentsMargins(18, 0, 0, 0)
        self.status.setText(text)
        self.status.setStyleSheet(f"QLabel {{ color:{color}; font-size:12px; background:transparent; }}")
        self._relayout()

    def _relayout(self) -> None:
        self.adjustSize()
        self.resized.emit()

    def shake(self) -> None:
        group = QSequentialAnimationGroup(self)
        last = 0
        for offset in (9, -9, 6, -6, 3, 0):
            step = QVariantAnimation(group)
            step.setDuration(45)
            step.setStartValue(last)
            step.setEndValue(offset)
            step.valueChanged.connect(self._apply_shake)
            group.addAnimation(step)
            last = offset
        group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _apply_shake(self, value) -> None:
        offset = int(value)
        self.code_layout.setContentsMargins(max(0, offset), 0, 6 + max(0, -offset), 0)


class NotificationCenter(QObject):
    """Stacks cards in the window's bottom-right corner: newest at the bottom, older ones pushed up."""

    def __init__(self, window: QWidget, anchor: QWidget | None = None, animate: bool = True):
        super().__init__(window)
        self.window, self.anchor, self.animate = window, anchor, animate
        self.cards: list[CodeNotification] = []
        self.moves: dict[str, QPropertyAnimation] = {}
        self.window.installEventFilter(self)

    # ---- queries ---------------------------------------------------------------------------
    def get(self, key: str) -> CodeNotification | None:
        return next((c for c in self.cards if c.key == key), None)

    def for_request(self, request: str) -> CodeNotification | None:
        return next((c for c in self.cards if c.request == request), None)

    def active(self, operator: str) -> CodeNotification | None:
        return next((c for c in reversed(self.cards) if c.operator == operator and c.state == "waiting"
                     and not c.property("leaving")), None)

    def of_operator(self, operator: str) -> list[CodeNotification]:
        return [c for c in self.cards if c.operator == operator]

    # ---- geometry ------------------------------------------------------------------------
    def bottom_y(self) -> int:
        """y of the lowest card's visible bottom edge (just above the record drawer)."""
        if self.anchor is not None and self.anchor.isVisible():
            return self.anchor.mapTo(self.window, QPoint(0, 0)).y() - MARGIN_BOTTOM
        return self.window.height() - MARGIN_BOTTOM - 8

    def targets(self) -> dict[str, QPoint]:
        """Widget positions: the visible surfaces stack upward from bottom_y with GAP between them."""
        result, surface_bottom = {}, self.bottom_y()
        x_right = self.window.width() - MARGIN_RIGHT + SHADOW
        for card in reversed([c for c in self.cards if not c.property("leaving")]):
            card.adjustSize()
            top = surface_bottom + SHADOW + 3 - card.height()     # surface ends SHADOW+3 px above the widget bottom
            result[card.key] = QPoint(x_right - card.width(), top)
            surface_bottom = top + SHADOW - 3 - GAP               # surface starts SHADOW-3 px below the widget top
        return result

    def relayout(self, animated: bool = True) -> None:
        for key, target in self.targets().items():
            card = self.get(key)
            if card is None:
                continue
            card.raise_()
            entering = getattr(card, "enter_slide", None)
            if entering is not None and entering.state() == QAbstractAnimation.State.Running:
                # A second pos animation would make Qt stop the entrance (slide + fade) half-way and leave
                # the card semi-transparent (live 10-15: two code cards 120 ms apart). Retarget it instead.
                entering.setEndValue(target)
                continue
            if not (animated and self.animate) or not card.isVisible():
                self._stop(key)
                card.move(target)
                continue
            running = self.moves.get(key)
            if running is not None and running.endValue() == target:
                continue
            if card.pos() == target:
                continue
            self._stop(key)
            anim = QPropertyAnimation(card, b"pos", self)
            anim.setDuration(300)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(card.pos())
            anim.setEndValue(target)
            self.moves[key] = anim
            anim.start()

    def _stop(self, key: str) -> None:
        anim = self.moves.pop(key, None)
        if anim is not None:
            anim.stop()

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.window and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            QTimer.singleShot(0, lambda: self.relayout(animated=False))
        return False

    # ---- add / remove ---------------------------------------------------------------------
    def add(self, card: CodeNotification) -> CodeNotification:
        card.setParent(self.window)
        card.resized.connect(lambda: self.relayout(True))
        self.cards.append(card)
        card.adjustSize()
        target = self.targets()[card.key]
        self.relayout(True)
        if not self.animate:
            card.move(target)
            card.show()
            card.raise_()
            return card
        card.move(target + QPoint(40, 0))
        card.show()
        card.raise_()
        card.opacity.setEnabled(True)
        card.opacity.setOpacity(0.0)
        enter = QParallelAnimationGroup(self)
        slide = QPropertyAnimation(card, b"pos", enter)
        slide.setDuration(420)
        slide.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutBack))
        slide.setStartValue(card.pos())
        slide.setEndValue(target)
        fade = QPropertyAnimation(card.opacity, b"opacity", enter)
        fade.setDuration(260)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        enter.addAnimation(slide)
        enter.addAnimation(fade)
        card.enter_slide = slide
        card.setProperty("entering", True)

        def settled(new_state, _old=None):
            # Finished or stopped, the card must end fully opaque and in its slot.
            if new_state != QAbstractAnimation.State.Stopped:
                return
            card.enter_slide = None
            card.setProperty("entering", False)
            if not card.property("leaving"):
                card.opacity.setOpacity(1.0)
                card.opacity.setEnabled(False)
                QTimer.singleShot(0, lambda: self.relayout(True))

        enter.stateChanged.connect(settled)
        enter.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        return card

    def remove(self, key: str) -> None:
        """Close a card: it leaves the stack at once (others close the gap) and fades out."""
        card = self.get(key)
        if card is None or card.property("leaving"):
            return
        card.setProperty("leaving", True)
        card.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._stop(key)
        self.relayout(True)

        def done():
            if card in self.cards:
                self.cards.remove(card)
            card.hide()
            card.deleteLater()

        if not self.animate or not card.isVisible():
            done()
            self.relayout(False)
            return
        card.opacity.setEnabled(True)
        leave = QParallelAnimationGroup(self)
        slide = QPropertyAnimation(card, b"pos", leave)
        slide.setDuration(200)
        slide.setEasingCurve(QEasingCurve.Type.InCubic)
        slide.setStartValue(card.pos())
        slide.setEndValue(card.pos() + QPoint(48, 0))
        fade = QPropertyAnimation(card.opacity, b"opacity", leave)
        fade.setDuration(180)
        fade.setStartValue(card.opacity.opacity())
        fade.setEndValue(0.0)
        leave.addAnimation(slide)
        leave.addAnimation(fade)
        leave.finished.connect(done)
        leave.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

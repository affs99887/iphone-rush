"""Modernized dialogs: SMS verification code, privacy consent, page preview, confirmations."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QRegularExpression, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QScrollArea, QSizePolicy,
                               QTextEdit, QVBoxLayout, QWidget)

from . import OFFSCREEN_POS, UI_FLAGS, is_quiet, theme
from .widgets import StatusPill, button, label

OTP_KINDS = {"sms": "Apple 短信验证码", "apple-2fa": "Apple ID 双重认证"}


def present(widget: QWidget) -> None:
    """Show a window/dialog. Normal mode: show + raise + activate. Quiet mode (§14.3): no activation, off-screen."""
    if is_quiet():
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        widget.move(*OFFSCREEN_POS)
        widget.show()
        return
    if UI_FLAGS.get("no_focus"):
        # Remote acceptance run: visible on screen, but never pull focus from what the user is doing.
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        widget.show()
        return
    widget.show()
    widget.raise_()
    widget.activateWindow()


# ---- six-cell code input -----------------------------------------------------------
class CodeInput(QLineEdit):
    """A QLineEdit that paints itself as six large cells. Digits only, never masked in logs."""

    CELLS = 6
    CELL_W, CELL_H, GAP = 44, 54, 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaxLength(self.CELLS)
        self.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9]{0,6}"), self))
        self.setFrame(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        width = self.CELLS * self.CELL_W + (self.CELLS - 1) * self.GAP + 2
        height = self.CELL_H + 2
        # The app QSS sets padding/min-height on QLineEdit; pin the geometry here so the cells are never clipped.
        self.setStyleSheet("QLineEdit { background: transparent; border: none; color: transparent; padding: 0;"
                           f" min-width: {width}px; max-width: {width}px; min-height: {height}px; max-height: {height}px; }}")
        self.setFixedSize(width, height)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.textChanged.connect(lambda _t: self.update())
        self.cursorPositionChanged.connect(lambda _o, _n: self.update())
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)

    def mousePressEvent(self, event):  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.setCursorPosition(len(self.text()))
        event.accept()

    def focusInEvent(self, event):  # noqa: N802
        super().focusInEvent(event)
        self.setCursorPosition(len(self.text()))
        self.update()

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = self.text()
        font = QFont(theme.MONO_FAMILY, 20)
        font.setBold(True)
        painter.setFont(font)
        active = len(text) if len(text) < self.CELLS else self.CELLS - 1
        for i in range(self.CELLS):
            x = 1 + i * (self.CELL_W + self.GAP)
            rect = QRectF(x, 1, self.CELL_W, self.CELL_H)
            path = QPainterPath()
            path.addRoundedRect(rect, 10, 10)
            focused = self.hasFocus() and i == active
            painter.setPen(QPen(QColor(theme.PRIMARY if focused else theme.BORDER_STRONG), 2 if focused else 1))
            painter.setBrush(QColor("#ffffff" if i < len(text) or focused else "#fafafc"))
            painter.drawPath(path)
            if i < len(text):
                painter.setPen(QColor(theme.TEXT))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text[i])
            elif focused and (self.property("blink") is not False):
                painter.setPen(QPen(QColor(theme.PRIMARY), 2))
                cx = rect.center().x()
                painter.drawLine(int(cx), int(rect.top() + 14), int(cx), int(rect.bottom() - 14))
        painter.end()


class VerificationDialog(QDialog):
    """Apple verification code prompt (SMS or Apple ID two-factor).

    API-compatible with 1.5: VerificationDialog(parent, phone, message).take_code(). Non-modal, stays on top,
    auto-focused, labelled with the operator; auto-submits as soon as six digits are entered (§12.3).
    The code is returned once by take_code() and cleared immediately; the UI never writes it anywhere.
    """

    def __init__(self, parent=None, phone: str = "", message: str = "", operator_label: str = "",
                 valid_seconds: int = 120, kind: str = "sms"):
        super().__init__(parent)
        self.operator_label = operator_label
        self.kind = kind if kind in OTP_KINDS else "sms"
        self.request = ""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        self.setMinimumWidth(440)
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 24, 28, 22)
        box.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.pill = StatusPill(operator_label or "官网验证", "warning")
        head.addWidget(self.pill)
        self.kind_chip = label("", "muted")
        head.addWidget(self.kind_chip)
        head.addStretch(1)
        box.addLayout(head)
        self.heading = label("", "cardTitle")
        box.addWidget(self.heading)
        self.description = label("", "muted", wrap=True)
        box.addWidget(self.description)
        self.phone_tail = phone[-4:] if phone else ""
        self.set_kind(self.kind, message)
        self.code = CodeInput(self)
        code_row = QHBoxLayout()
        code_row.addStretch(1)
        code_row.addWidget(self.code)
        code_row.addStretch(1)
        box.addSpacing(4)
        box.addLayout(code_row)
        self.error_label = label("", "error", wrap=True)
        self.error_label.setVisible(False)
        box.addWidget(self.error_label)
        self.hint = label("", "muted")
        box.addWidget(self.hint)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.cancel = button("暂停验证", tip="保持本路暂停，稍后可在卡片上点击“继续”")
        self.ok = button("提交验证码", "primary")
        self.ok.setEnabled(False)
        self.ok.setDefault(True)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.ok)
        box.addLayout(buttons)
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self.accept)
        self.code.textChanged.connect(self._code_changed)
        self.code.returnPressed.connect(lambda: self.accept() if len(self.code.text()) == 6 else None)
        self.auto_submit = True
        self.remaining = max(0, int(valid_seconds))
        self.valid_minutes = max(1, round(self.remaining / 60)) if self.remaining else 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        if self.remaining:
            self.timer.start(1000)
        self._tick()
        self.code.setFocus()

    def set_kind(self, kind: str, message: str = "") -> None:
        self.kind = kind if kind in OTP_KINDS else "sms"
        title = OTP_KINDS[self.kind]
        self.setWindowTitle((self.operator_label + " · " if self.operator_label else "") + title)
        self.kind_chip.setText(title)
        if self.kind == "apple-2fa":
            self.heading.setText("请输入 Apple ID 双重认证验证码")
            default = "验证码已发送到你的受信任设备（iPhone / iPad / Mac）。只用于本次登录，不写入设置、日志或文件。"
        else:
            self.heading.setText(f"请输入尾号 {self.phone_tail} 收到的 Apple 验证码" if self.phone_tail else "请输入 Apple 短信验证码")
            default = "官网已发送短信。验证码只用于本次官网验证，不写入设置、日志或文件。"
        self.description.setText(message or default)

    def _code_changed(self, text: str) -> None:
        complete = len(text) == 6
        self.ok.setEnabled(complete)
        if complete and self.auto_submit:
            # Let the sixth digit paint first, then submit without another click (§12.3).
            QTimer.singleShot(0, self.accept)

    def _tick(self) -> None:
        if self.remaining > 0:
            minutes, secs = divmod(self.remaining, 60)
            self.hint.setText(f"输满 6 位自动提交 · 验证码一般 {self.valid_minutes} 分钟内有效 · 剩余 {minutes:02}:{secs:02}")
            self.remaining -= 1
        else:
            self.timer.stop()
            self.hint.setText("验证码可能已过期；如官网提示失效，请在卡片上点击“继续”让操作员重新发送。")

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.code.clear()
        self.code.setFocus()

    def take_code(self) -> str:
        """Return the code once (only after accept) and clear it; later calls return ""."""
        value = self.code.text() if self.result() == QDialog.DialogCode.Accepted else ""
        self.code.clear()
        return value

    def accept(self) -> None:  # noqa: D401 - guard: only six digits can be accepted
        if len(self.code.text()) != 6 or self.result() == QDialog.DialogCode.Accepted:
            return
        self.timer.stop()
        super().accept()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.code.setFocus(Qt.FocusReason.OtherFocusReason)


# ---- Apple ID login (§13.2 / §14.5) -------------------------------------------------
class LoginDialog(QDialog):
    """Collects Apple ID + password for the operator's `login` command.

    The password lives only in the line edit until 登录 is clicked; it is handed to the `submitted`
    signal and the widget is cleared in the same call. Nothing on the dialog keeps a copy.
    """

    submitted = Signal(str, str)   # apple_id, password

    def __init__(self, parent=None, operator_label: str = "", apple_id: str = ""):
        super().__init__(parent)
        self.operator_label = operator_label
        self.setWindowTitle((operator_label + " · " if operator_label else "") + "登录 Apple 账户")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(440)
        box = QVBoxLayout(self)
        box.setContentsMargins(28, 24, 28, 22)
        box.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(StatusPill(operator_label or "操作员", "info"))
        head.addStretch(1)
        box.addLayout(head)
        box.addWidget(label("登录 Apple 账户", "cardTitle"))
        box.addWidget(label("操作员会在后台页面内输入并登录；如需双重认证，软件会弹出验证码窗口。"
                            "密码只在本次命令中存在于内存，不写入设置、日志或文件。", "muted", wrap=True))
        self.apple_id = QLineEdit()
        self.apple_id.setPlaceholderText("Apple ID（邮箱或手机号）")
        self.apple_id.setText(apple_id)
        box.addWidget(self.apple_id)
        self.password = QLineEdit()
        self.password.setPlaceholderText("密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        box.addWidget(self.password)
        self.error_label = label("", "error", wrap=True)
        self.error_label.setVisible(False)
        box.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = button("取消")
        self.login = button("登录", "primary")
        self.login.setDefault(True)
        self.login.setEnabled(False)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.login)
        box.addLayout(buttons)
        self.cancel.clicked.connect(self.reject)
        self.login.clicked.connect(self.send)
        self.apple_id.textChanged.connect(self._validate)
        self.password.textChanged.connect(self._validate)
        self.password.returnPressed.connect(lambda: self.send() if self.login.isEnabled() else None)
        (self.password if apple_id else self.apple_id).setFocus()

    def _validate(self) -> None:
        self.login.setEnabled(bool(self.apple_id.text().strip()) and bool(self.password.text()))

    def send(self) -> None:
        apple_id = self.apple_id.text().strip()
        password = self.password.text()
        if not apple_id or not password:
            self.error_label.setText("请填写 Apple ID 与密码。")
            self.error_label.setVisible(True)
            return
        # Clear the widget before anything else can run; the only remaining copy is the signal argument.
        self.password.clear()
        self.password.setText("")
        self.submitted.emit(apple_id, password)
        del password
        self.accept()

    def reject(self) -> None:
        self.password.clear()
        super().reject()


# ---- privacy consent ---------------------------------------------------------------
class ConsentDialog(QDialog):
    """Privacy consent prompt. Only the user's explicit click lets the operator tick the box."""

    decided = Signal(str, bool)   # request id, accepted

    def __init__(self, parent=None, operator_label: str = "", request: str = "", url: str = "", text: str = ""):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle((operator_label + " · " if operator_label else "") + "Apple 隐私知情同意书")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(720, 580)
        self.setMinimumSize(560, 420)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(StatusPill(operator_label or "官网", "warning"))
        head.addWidget(label("Apple 隐私知情同意书", "cardTitle"))
        head.addStretch(1)
        box.addLayout(head)
        self.source = label("来源：" + (url or "Apple 官网结账页"), "muted")
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.source)
        self.document = QTextEdit()
        self.document.setObjectName("document")
        self.document.setReadOnly(True)
        self.document.setPlainText(text or "（官网未提供可读文本，请点击“查看页面”核对后再决定。）")
        box.addWidget(self.document, 1)
        box.addWidget(label("同意后，本路只勾选这份知情同意书并继续；购买协议与销售条款不会自动勾选。", "muted", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.decline = button("暂不同意，保持暂停")
        self.agree = button("同意并继续本路", "primary")
        buttons.addWidget(self.decline)
        buttons.addWidget(self.agree)
        box.addLayout(buttons)
        self.decline.clicked.connect(self.reject)
        self.agree.clicked.connect(self.accept)
        self.finished.connect(lambda code: self.decided.emit(self.request, code == QDialog.DialogCode.Accepted))


# ---- page preview ------------------------------------------------------------------
class PagePreviewDialog(QDialog):
    """Screenshot + text of the operator's hidden browser page, with a refresh button."""

    refresh_requested = Signal()

    def __init__(self, parent=None, operator_label: str = ""):
        super().__init__(parent)
        self.operator_label = operator_label
        self.setWindowTitle((operator_label + " · " if operator_label else "") + "后台页面预览")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(980, 760)
        self.setMinimumSize(640, 480)
        box = QVBoxLayout(self)
        box.setContentsMargins(20, 16, 20, 16)
        box.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(StatusPill(operator_label or "页面", "info"))
        self.url_label = label("", "muted")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        head.addWidget(self.url_label, 1)
        self.refresh = button("刷新截图", tip="让操作员重新截取当前页面")
        self.refresh.clicked.connect(self.refresh_requested.emit)
        head.addWidget(self.refresh)
        box.addLayout(head)
        self.text = QTextEdit()
        self.text.setObjectName("reader")
        self.text.setReadOnly(True)
        self.text.setFixedHeight(110)
        box.addWidget(self.text)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("framed")
        self.scroll.setWidgetResizable(True)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.picture.setStyleSheet("background: transparent;")
        self.scroll.setWidget(self.picture)
        box.addWidget(self.scroll, 1)
        self._pixmap = QPixmap()
        self.updated_label = label("", "muted")
        box.addWidget(self.updated_label)

    def set_page(self, url: str = "", text: str = "", image: str = "", when: str = "") -> None:
        self.url_label.setText(url or "（无地址）")
        self.text.setPlainText(text or "（没有可读文本）")
        self._pixmap = QPixmap(image) if image else QPixmap()
        self.updated_label.setText(("截图时间 " + when + " · " if when else "") + (image or "暂无截图"))
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap.isNull():
            self.picture.setPixmap(QPixmap())
            self.picture.setText("暂无截图。点击“刷新截图”让操作员截取当前页面。")
            self.picture.setObjectName("muted")
            return
        width = max(200, self.scroll.viewport().width() - 16)
        self.picture.setPixmap(self._pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


# ---- message box helpers ------------------------------------------------------------
def confirm(parent: QWidget | None, title: str, text: str, yes: str = "确定", no: str = "取消",
            danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    ok = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(no, QMessageBox.ButtonRole.RejectRole)
    ok.setObjectName("danger" if danger else "primary")
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is ok


def inform(parent: QWidget | None, title: str, text: str, error: bool = False) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical if error else QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text)
    box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
    box.exec()

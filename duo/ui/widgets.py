"""Reusable dashboard widgets (PySide6 Widgets + QPainter, no QML)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen, QPixmap,
                           QStandardItem, QStandardItemModel, QTextOption)
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QListView, QPushButton, QSizePolicy,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from . import theme

CHINA = timezone(timedelta(hours=8))

STEP_TITLES = ["入口", "选配", "购物袋", "结账", "核对", "提交"]
# brief §7: fulfillment / contact / verify / payment are all shown under "结账".
PHASE_STEP = {
    "idle": -1, "waiting": -1, "entry": 0, "configure": 1, "add": 1, "bag": 2, "checkout": 3,
    "fulfillment": 3, "contact": 3, "verify": 3, "payment": 3, "review": 4, "submit": 5, "done": 6,
}
CHECK_TITLES = {"color": "星光白色", "capacity": "1TB", "trade": "不折抵", "care": "AppleCare+"}
ROW_HEIGHT = 20   # minimum height of a caption/value row so layouts never squeeze text together


# ---- small helpers -------------------------------------------------------------
def label(text: str = "", role: str = "", wrap: bool = False, align=None) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    if wrap:
        widget.setWordWrap(True)
    if align is not None:
        widget.setAlignment(align)
    return widget


def button(text: str, role: str = "", tip: str = "") -> QPushButton:
    widget = QPushButton(text)
    if role:
        widget.setObjectName(role)
    if tip:
        widget.setToolTip(tip)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return widget


def divider(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("vdivider" if vertical else "divider")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


class ElidedLabel(QLabel):
    """Single-line label that elides with “…” instead of growing the layout; full text in the tooltip."""

    def __init__(self, text: str = "", role: str = "value"):
        super().__init__()
        self.setObjectName(role)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(ROW_HEIGHT)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        super().setText(self._full)
        self.setToolTip(self._full)
        self.update()

    def text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, max(ROW_HEIGHT, super().minimumSizeHint().height()))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(max(40, min(super().sizeHint().width(), 240)), max(ROW_HEIGHT, super().sizeHint().height()))

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 2))
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        painter.drawText(self.rect(), int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), elided)
        painter.end()


class WrapLabel(QLabel):
    """Word-wrapped label with a fixed number of lines; full text in the tooltip."""

    def __init__(self, text: str = "", lines: int = 2, role: str = "muted"):
        super().__init__()
        self.setObjectName(role)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._lines = lines
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        super().setText(text)
        self.setToolTip(text if len(text) > 24 else "")

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.setFixedHeight(QFontMetrics(self.font()).lineSpacing() * self._lines + 2)


# ---- pills & chips --------------------------------------------------------------
class StatusPill(QLabel):
    """Rounded status pill with a leading dot: ● 入口竞速."""

    def __init__(self, text: str = "空闲", tone: str = "neutral", compact: bool = False):
        super().__init__()
        self._compact = compact
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_state(text, tone)

    def set_state(self, text: str, tone: str = "neutral") -> None:
        self._text, self._tone = text, tone
        bg, fg = theme.tone_colors(tone)
        size = 11 if self._compact else 12
        pad = "2px 9px 2px 8px" if self._compact else "3px 11px 3px 9px"
        self.setStyleSheet(f"QLabel {{ background:{bg}; color:{fg}; border-radius: {11 if self._compact else 13}px;"
                           f" padding:{pad}; font-size:{size}px; font-weight:600; }}")
        super().setText(f"●  {text}")
        self.adjustSize()

    def text(self) -> str:  # noqa: D401 - state text without the dot
        return self._text

    @property
    def tone(self) -> str:
        return self._tone


class Chip(QLabel):
    """Tiny tag (used for the four configuration checks, network badges, modes)."""

    def __init__(self, text: str = "", tone: str = "neutral"):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_chip(text, tone)

    def set_chip(self, text: str, tone: str = "neutral") -> None:
        self._tone = tone
        bg, fg = theme.tone_colors(tone)
        self.setStyleSheet(f"QLabel {{ background:{bg}; color:{fg}; border-radius: 7px; padding: 1px 8px;"
                           f" font-size: 11px; font-weight: 600; }}")
        self.setText(text)
        self.adjustSize()

    @property
    def tone(self) -> str:
        return self._tone


# ---- cards ----------------------------------------------------------------------
class Card(QFrame):
    """White rounded card with a soft shadow and an optional header row."""

    def __init__(self, title: str = "", right: QWidget | None = None, padding: int = 18):
        super().__init__()
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.soft_shadow(self)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding - 2, padding, padding - 2)
        self.body.setSpacing(10)
        self.title_label = None
        self.header = None
        if title:
            self.header = QHBoxLayout()
            self.header.setSpacing(8)
            self.title_label = label(title, "cardTitle")
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            if right is not None:
                self.header.addWidget(right)
            self.body.addLayout(self.header)

    def set_attention(self, on: bool) -> None:
        if self.property("attention") == on:
            return
        self.setProperty("attention", on)
        theme.repolish(self)

    def set_tone(self, tone: str) -> None:
        if self.property("tone") == tone:
            return
        self.setProperty("tone", tone)
        theme.repolish(self)


class KeyValueGrid(QGridLayout):
    """Caption / value rows that keep the captions aligned."""

    def __init__(self, caption_width: int = 58):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(10)
        self.setVerticalSpacing(6)
        self.setColumnMinimumWidth(0, caption_width)
        self.setColumnStretch(1, 1)
        self._rows = 0

    def add_row(self, caption: str, value: QWidget | None = None) -> QWidget:
        cap = label(caption, "caption")
        cap.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        cap.setMinimumHeight(ROW_HEIGHT)
        if value is None:
            value = ElidedLabel("—", "value")
        value.setMinimumHeight(max(ROW_HEIGHT, value.minimumHeight()))
        self.addWidget(cap, self._rows, 0)
        self.addWidget(value, self._rows, 1)
        self.setRowMinimumHeight(self._rows, ROW_HEIGHT)
        self._rows += 1
        return value


# ---- step bar ------------------------------------------------------------------
class StepBar(QWidget):
    """Six-step progress bar: 入口 / 选配 / 购物袋 / 结账 / 核对 / 提交."""

    def __init__(self):
        super().__init__()
        self.current = -1          # index of the active step, 6 = everything done
        self.completed: set[int] = set()
        self.flag = ""             # "", "attention", "paused", "blocked", "failed"
        self.setFixedHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def reset(self) -> None:
        self.current, self.completed, self.flag = -1, set(), ""
        self.update()

    def set_phase(self, phase: str) -> None:
        index = PHASE_STEP.get(phase)
        if index is None:
            return
        self.current = index
        if index >= 0:
            self.completed |= set(range(min(index, 6)))
        self.update()

    def mark_done(self, phase: str) -> None:
        index = PHASE_STEP.get(phase, -1)
        if 0 <= index < 6:
            self.completed.add(index)
            self.current = max(self.current, index + 1)
            self.update()

    def set_flag(self, flag: str) -> None:
        if flag != self.flag:
            self.flag = flag
            self.update()

    def step_state(self, index: int) -> str:
        if index in self.completed or index < self.current:
            return "done"
        if index == self.current:
            return "current"
        return "pending"

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(STEP_TITLES)
        w = self.width()
        step_w = w / n
        cy, r = 12.0, 8.5
        accent = {"attention": theme.WARNING, "paused": theme.MUTED, "blocked": theme.WARNING,
                  "failed": theme.DANGER}.get(self.flag, theme.PRIMARY)
        font = QFont(self.font())
        font.setPixelSize(11)
        for i in range(n):
            cx = step_w * (i + 0.5)
            state = self.step_state(i)
            if i < n - 1:
                done_line = self.step_state(i) == "done" and self.step_state(i + 1) != "pending" or \
                    self.step_state(i + 1) == "done"
                pen = QPen(QColor(theme.PRIMARY if done_line else theme.BORDER_STRONG), 2)
                painter.setPen(pen)
                painter.drawLine(QPointF(cx + r + 4, cy), QPointF(cx + step_w - r - 4, cy))
            if state == "done":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.PRIMARY))
                painter.drawEllipse(QPointF(cx, cy), r, r)
                pen = QPen(QColor("#ffffff"), 1.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                path = QPainterPath(QPointF(cx - 3.6, cy + 0.2))
                path.lineTo(cx - 1.0, cy + 2.8)
                path.lineTo(cx + 3.8, cy - 2.6)
                painter.drawPath(path)
            elif state == "current":
                painter.setBrush(QColor("#ffffff"))
                painter.setPen(QPen(QColor(accent), 2))
                painter.drawEllipse(QPointF(cx, cy), r, r)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(accent))
                painter.drawEllipse(QPointF(cx, cy), 3.4, 3.4)
            else:
                painter.setBrush(QColor(theme.NEUTRAL_SOFT))
                painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
                painter.drawEllipse(QPointF(cx, cy), r, r)
            font.setBold(state == "current")
            painter.setFont(font)
            color = theme.TEXT if state != "pending" else theme.MUTED
            if state == "current" and self.flag:
                color = accent
            painter.setPen(QColor(color))
            rect = QRectF(step_w * i, cy + r + 6, step_w, 18)
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, STEP_TITLES[i])
        painter.end()


# ---- countdown pill -------------------------------------------------------------
class CountdownPill(QFrame):
    """Time left until the sale, using the dispatcher's clock offset when known."""

    def __init__(self, target: datetime | None = None):
        super().__init__()
        self.setObjectName("countdown")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QFrame#countdown {{ background: {theme.CARD}; border: 1px solid {theme.BORDER};"
                           f" border-radius: 19px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 5, 16, 5)
        row.setSpacing(10)
        self.caption = label("距开售", "caption")
        self.value = label("--:--:--", "mono")
        font = theme.mono_font(13, bold=True)
        self.value.setFont(font)
        self.value.setStyleSheet(f"color:{theme.TEXT};")
        self.source = Chip("本机时钟", "neutral")
        row.addWidget(self.caption)
        row.addWidget(self.value)
        row.addWidget(self.source)
        self.target = target or datetime(2026, 10, 16, 20, 0, tzinfo=CHINA)
        self.offset_ms: int | None = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)
        self.tick()

    def set_target(self, target: datetime) -> None:
        if target.tzinfo is None:
            target = target.replace(tzinfo=CHINA)
        self.target = target
        self.tick()

    def set_offset_ms(self, offset_ms: int | None) -> None:
        self.offset_ms = offset_ms
        if offset_ms is None:
            self.source.set_chip("本机时钟", "neutral")
        else:
            self.source.set_chip("官网时钟", "success")
        self.tick()

    def now(self) -> datetime:
        now = datetime.now(CHINA)
        if self.offset_ms is not None:
            now += timedelta(milliseconds=self.offset_ms)
        return now

    def tick(self) -> None:
        delta = self.target - self.now()
        seconds = int(delta.total_seconds())
        day_text = self.target.strftime("%m月%d日 %H:%M").replace("月0", "月").lstrip("0")
        if seconds <= 0:
            self.caption.setText(f"{day_text} 已开售")
            self.value.setText("00:00:00")
            self.value.setStyleSheet(f"color:{theme.SUCCESS};")
            return
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        self.caption.setText(f"距 {day_text} 开售")
        self.value.setText(f"{days}天 {hours:02}:{minutes:02}:{secs:02}" if days else f"{hours:02}:{minutes:02}:{secs:02}")
        self.value.setStyleSheet(f"color:{theme.PRIMARY if seconds < 3600 else theme.TEXT};")


# ---- timeline -------------------------------------------------------------------
ROLE_TIME = Qt.ItemDataRole.UserRole + 1
ROLE_TAG = Qt.ItemDataRole.UserRole + 2
ROLE_TONE = Qt.ItemDataRole.UserRole + 3


class TimelineDelegate(QStyledItemDelegate):
    PAD_Y = 6
    DOT_X = 8

    def __init__(self, view: QListView):
        super().__init__(view)
        self.view = view

    def _fonts(self):
        meta = QFont(self.view.font())
        meta.setPixelSize(11)
        body = QFont(self.view.font())
        body.setPixelSize(12)
        return meta, body

    def _text_width(self) -> int:
        return max(60, self.view.viewport().width() - 30)

    def _body_rect(self, body_font: QFont, text: str) -> QRect:
        metrics = QFontMetrics(body_font)
        rect = metrics.boundingRect(QRect(0, 0, self._text_width(), 1000),
                                    Qt.TextFlag.TextWordWrap, text)
        max_h = metrics.lineSpacing() * 2
        return QRect(0, 0, rect.width(), min(rect.height(), max_h))

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        meta, body = self._fonts()
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        height = self.PAD_Y * 2 + QFontMetrics(meta).lineSpacing() + 2 + self._body_rect(body, text).height()
        return QSize(self._text_width() + 30, height)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        meta, body = self._fonts()
        rect = option.rect
        tone = index.data(ROLE_TONE) or "info"
        _, fg = theme.tone_colors(tone)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        time_text = index.data(ROLE_TIME) or ""
        tag = index.data(ROLE_TAG) or ""
        meta_fm = QFontMetrics(meta)
        y = rect.top() + self.PAD_Y
        # dot
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fg))
        painter.drawEllipse(QPointF(rect.left() + self.DOT_X, y + meta_fm.lineSpacing() / 2), 3.5, 3.5)
        # meta line: tag · time
        painter.setFont(meta)
        x = rect.left() + 22
        if tag:
            painter.setPen(QColor(fg))
            painter.drawText(QRect(x, y, 200, meta_fm.lineSpacing()), Qt.AlignmentFlag.AlignVCenter, tag)
            x += meta_fm.horizontalAdvance(tag) + 8
        painter.setPen(QColor(theme.FAINT))
        painter.drawText(QRect(x, y, 200, meta_fm.lineSpacing()), Qt.AlignmentFlag.AlignVCenter, time_text)
        # body
        y += meta_fm.lineSpacing() + 2
        painter.setFont(body)
        painter.setPen(QColor(theme.TEXT))
        body_rect = QRect(rect.left() + 22, y, self._text_width(), self._body_rect(body, text).height())
        option_text = QTextOption(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        option_text.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        painter.setClipRect(body_rect)
        painter.drawText(QRectF(body_rect), text, option_text)
        painter.restore()


class Timeline(QListView):
    """Most recent events (max 50): dot + tag + time + wrapped text."""

    def __init__(self, max_items: int = 50):
        super().__init__()
        self.setObjectName("timeline")
        self.max_items = max_items
        self.model_ = QStandardItemModel(self)
        self.setModel(self.model_)
        self.delegate = TimelineDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMouseTracking(True)
        self.setViewportMargins(0, 4, 0, 0)

    def add(self, text: str, tag: str = "", tone: str = "info", when: datetime | None = None) -> None:
        when = when or datetime.now(CHINA)
        item = QStandardItem(text)
        item.setData(when.strftime("%H:%M:%S"), ROLE_TIME)
        item.setData(tag, ROLE_TAG)
        item.setData(tone, ROLE_TONE)
        item.setToolTip(text)
        item.setEditable(False)
        at_bottom = self.verticalScrollBar().value() >= self.verticalScrollBar().maximum() - 4
        self.model_.appendRow(item)
        while self.model_.rowCount() > self.max_items:
            self.model_.removeRow(0)
        if at_bottom:
            self.scrollToBottom()

    def entries(self) -> list[str]:
        return [self.model_.item(i).text() for i in range(self.model_.rowCount())]

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()


# ---- lane meter ------------------------------------------------------------------
class LaneDots(QWidget):
    def __init__(self):
        super().__init__()
        self.total, self.loading, self.winner = 3, 0, None
        self.setFixedSize(4 * 16, 10)

    def set_lanes(self, total: int, loading: int, winner: int | None) -> None:
        self.total, self.loading, self.winner = max(0, min(total, 6)), loading, winner
        self.setFixedWidth(max(1, self.total) * 16)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(self.total):
            if self.winner == i + 1:
                color = theme.SUCCESS
            elif self.winner:
                color = theme.BORDER
            elif i < self.loading:
                color = theme.PRIMARY
            else:
                color = theme.BORDER_STRONG
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(i * 16, 1, 12, 8), 3, 3)
        painter.end()


class LaneMeter(QWidget):
    """入口竞速小指标：N 入口 · 加载中 · 请求数 · 冷却."""

    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.dots = LaneDots()
        self.text = ElidedLabel("尚未开始入口竞速", "value")
        row.addWidget(self.dots)
        row.addWidget(self.text, 1)
        self.lanes = {}

    def set_lanes(self, lanes: dict) -> None:
        self.lanes = dict(lanes or {})
        total = int(lanes.get("total") or 0)
        loading = int(lanes.get("loading") or 0)
        requests = int(lanes.get("requests") or 0)
        cooldown = float(lanes.get("cooldown_s") or 0)
        winner = lanes.get("winner")
        self.dots.set_lanes(total, loading, winner)
        if winner:
            text = f"入口 {winner} 已接管 · 其余入口已关闭 · 请求 {requests}"
        else:
            text = f"{total} 入口 · 加载中 {loading} · 请求 {requests}"
            text += f" · 冷却 {cooldown:.0f}s" if cooldown > 0 else " · 无冷却"
        self.text.setText(text)


# ---- speed table (§12.5) -----------------------------------------------------------------
SPEED_ROWS = [("wall", "维护墙"), ("configure", "选配→加购"), ("bag", "加购→结账"), ("checkout", "结账→核对"), ("total", "总计")]
# operator phase → speed row (fulfillment/contact/verify/payment are part of "结账→核对")
PHASE_SPEED_ROW = {
    "entry": "wall", "maintenance": "wall", "wall": "wall",
    "configure": "configure", "add": "configure",
    "bag": "bag",
    "checkout": "checkout", "fulfillment": "checkout", "contact": "checkout", "verify": "checkout", "payment": "checkout",
    "total": "total",
}


def fmt_ms(ms: float | int | None) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    if ms < 0:
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    if seconds < 100:
        return f"{seconds:.2f} s" if seconds < 10 else f"{seconds:.1f} s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


class SpeedTable(QWidget):
    """Compact five-column speed strip: 维护墙 / 选配→加购 / 加购→结账 / 结账→核对 / 总计.

    Fed by `operator.timing` events carrying `phase` + `phase_ms`, by `status()["operators"][op]["timeline_ms"]`,
    and by a stopwatch that runs from the run's target time (T) once a run starts.
    """

    HEIGHT = 38

    def __init__(self):
        super().__init__()
        self.phase_ms: dict[str, float] = {}
        self.total_override: float | None = None
        self.target: datetime | None = None
        self.stopped_at: datetime | None = None
        self.offset_ms = 0
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("每阶段用时：维护墙穿越 / 选配到加购确认 / 加购到结账 / 结账到核对页 / 从开售时间 T 起的总计")

    # -- data
    def reset(self) -> None:
        self.phase_ms.clear()
        self.total_override = None
        self.target = self.stopped_at = None
        self.update()

    def set_phase_ms(self, phase: str, ms) -> None:
        if phase not in PHASE_SPEED_ROW or ms is None:
            return
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if phase == "total":
            self.total_override = value
        else:
            self.phase_ms[phase] = value
        self.update()

    def set_timeline(self, timeline: dict | None) -> None:
        """Replace all values from the dispatcher's aggregated `timeline_ms` (phase → ms, or row → ms)."""
        if not isinstance(timeline, dict):
            return
        self.phase_ms.clear()
        self.total_override = None
        for key, value in timeline.items():
            if isinstance(value, dict):
                value = value.get("ms", value.get("phase_ms"))
            self.set_phase_ms(str(key), value)
        self.update()

    def start(self, target: datetime | None) -> None:
        self.target = target
        self.stopped_at = None
        self.update()

    def stop(self) -> None:
        if self.target is not None and self.stopped_at is None:
            self.stopped_at = self.now()
        self.update()

    def set_clock_offset(self, offset_ms: int | None) -> None:
        self.offset_ms = int(offset_ms or 0)

    def now(self) -> datetime:
        return datetime.now(CHINA) + timedelta(milliseconds=self.offset_ms)

    def row_ms(self, row: str) -> float | None:
        if row == "total":
            if self.total_override is not None:
                return self.total_override
            if self.target is not None:
                end = self.stopped_at or self.now()
                elapsed = (end - self.target).total_seconds() * 1000
                return elapsed if elapsed >= 0 else None
            known = [v for k, v in self.phase_ms.items() if PHASE_SPEED_ROW.get(k) != "total"]
            return sum(known) if known else None
        values = [v for k, v in self.phase_ms.items() if PHASE_SPEED_ROW.get(k) == row]
        return sum(values) if values else None

    def values(self) -> dict[str, float | None]:
        return {row: self.row_ms(row) for row, _title in SPEED_ROWS}

    @property
    def running(self) -> bool:
        return self.target is not None and self.stopped_at is None

    def tick(self) -> None:
        if self.running:
            self.update()

    # -- paint
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(SPEED_ROWS)
        col_w = self.width() / n
        caption_font = QFont(self.font())
        caption_font.setPixelSize(11)
        value_font = theme.mono_font(9, bold=True)
        value_font.setPixelSize(12)
        painter.setPen(QColor(theme.BORDER))
        for i in range(1, n):
            x = int(col_w * i)
            painter.drawLine(x, 6, x, self.HEIGHT - 6)
        for i, (row, title) in enumerate(SPEED_ROWS):
            rect = QRectF(col_w * i, 0, col_w, self.HEIGHT)
            painter.setFont(caption_font)
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(rect.adjusted(0, 3, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, title)
            value = self.row_ms(row)
            text = fmt_ms(value)
            painter.setFont(value_font)
            if row == "total" and self.running:
                color = theme.PRIMARY
            elif value is None:
                color = theme.FAINT
            else:
                color = theme.TEXT
            painter.setPen(QColor(color))
            painter.drawText(rect.adjusted(0, 0, 0, -3), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, text)
        painter.end()


# ---- configuration check list -----------------------------------------------------
class CheckList(QWidget):
    """Four configuration checks as painted chips (星光白色 / 1TB / 不折抵 / AppleCare+).

    Lays the chips out in one row when the card is wide enough and wraps to two rows otherwise
    (height-for-width), so nothing is ever clipped.
    """

    CHIP_H, GAP, PAD = 18, 6, 8

    def __init__(self):
        super().__init__()
        self.titles = dict(CHECK_TITLES)
        self.checks = {key: False for key in CHECK_TITLES}
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(ROW_HEIGHT)

    # -- data
    def set_trial(self, trial: bool) -> None:
        self.titles["color"] = "云白色" if trial else CHECK_TITLES["color"]
        self.updateGeometry()
        self.update()

    def set_checks(self, checks: dict | None) -> None:
        checks = checks or {}
        for key in self.checks:
            self.checks[key] = bool(checks.get(key, False))
        self.update()

    def chip_texts(self) -> list[tuple[str, str, bool]]:
        return [(key, ("✓ " if self.checks[key] else "○ ") + self.titles[key], self.checks[key]) for key in CHECK_TITLES]

    # -- geometry
    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(True)
        return font

    def _rows(self, width: int) -> list[list[tuple[int, int, str, bool]]]:
        metrics = QFontMetrics(self._font())
        rows: list[list[tuple[int, int, str, bool]]] = [[]]
        x = 0
        for _key, text, ok in self.chip_texts():
            w = metrics.horizontalAdvance(text) + 2 * self.PAD
            if rows[-1] and x + w > width:
                rows.append([])
                x = 0
            rows[-1].append((x, w, text, ok))
            x += w + self.GAP
        return rows

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        rows = len(self._rows(max(width, 1)))
        return max(ROW_HEIGHT, rows * self.CHIP_H + (rows - 1) * 4 + (ROW_HEIGHT - self.CHIP_H))

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        # When chips wrap, the wrapped height becomes a hard minimum so an over-constrained layout cannot clip a row.
        needed = self.heightForWidth(self.width())
        if needed != self.minimumHeight():
            self.setMinimumHeight(needed)
            self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self._font())
        width = sum(metrics.horizontalAdvance(t) + 2 * self.PAD for _k, t, _ok in self.chip_texts()) + self.GAP * 3
        return QSize(width, ROW_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(80, ROW_HEIGHT)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font())
        rows = self._rows(self.width())
        y = (ROW_HEIGHT - self.CHIP_H) / 2
        for row in rows:
            for x, w, text, ok in row:
                bg, fg = theme.tone_colors("success" if ok else "neutral")
                rect = QRectF(x, y, w, self.CHIP_H)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(bg))
                painter.drawRoundedRect(rect, 7, 7)
                painter.setPen(QColor(fg))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            y += self.CHIP_H + 4
        painter.end()


# ---- screenshot thumbnail ---------------------------------------------------------
class ScreenshotThumb(QFrame):
    """Page screenshot preview; click = ask the operator to refresh/show the page."""

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("thumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.pixmap: QPixmap | None = None
        self.path = ""
        self.placeholder = "任务开始后此处显示后台页面截图 · 点击查看"
        self.summary_title = ""
        self.summary_lines: list[str] = []
        self.setToolTip("点击放大查看当前后台页面")

    def set_image(self, path: str) -> None:
        self.path = path or ""
        pix = QPixmap(path) if path else QPixmap()
        self.pixmap = pix if not pix.isNull() else None
        self.update()

    def set_placeholder(self, text: str) -> None:
        self.placeholder = text
        self.update()

    def set_summary(self, title: str, lines: list[str]) -> None:
        """Shown while there is no screenshot yet (the mission brief for this operator)."""
        self.summary_title, self.summary_lines = title, list(lines)
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QColor("#fafafc"))
        painter.drawPath(path)
        if self.pixmap:
            painter.setClipPath(path)
            scaled = self.pixmap.scaled(self.size() - QSize(2, 2), Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) / 2
            painter.drawPixmap(QPointF(x, 1), scaled)
        else:
            font = QFont(self.font())
            font.setPixelSize(12)
            hint_rect = QRectF(self.rect()).adjusted(12, 0, -12, -10)
            if self.summary_lines and self.height() >= 120:
                title_font = QFont(self.font())
                title_font.setPixelSize(12)
                title_font.setBold(True)
                line_h = 20
                total = (line_h if self.summary_title else 0) + line_h * len(self.summary_lines)
                y = max(14.0, (self.height() - 24 - total) / 2)
                if self.summary_title:
                    painter.setFont(title_font)
                    painter.setPen(QColor(theme.MUTED))
                    painter.drawText(QRectF(12, y, self.width() - 24, line_h), Qt.AlignmentFlag.AlignCenter, self.summary_title)
                    y += line_h
                painter.setFont(font)
                painter.setPen(QColor(theme.TEXT))
                for line in self.summary_lines:
                    painter.drawText(QRectF(12, y, self.width() - 24, line_h), Qt.AlignmentFlag.AlignCenter, line)
                    y += line_h
                painter.setPen(QColor(theme.FAINT))
                painter.drawText(hint_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, self.placeholder)
            else:
                painter.setFont(font)
                painter.setPen(QColor(theme.FAINT))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
        painter.end()


# ---- misc ------------------------------------------------------------------------
class Banner(QLabel):
    """Prominent one-line notice (e.g. demo mode)."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setObjectName("banner")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setVisible(bool(text))


def tone_for_state(state: str, phase: str = "") -> str:
    """Pick a pill tone from an operator state phrase + phase."""
    if phase in ("failed",) or any(k in state for k in ("失败", "错误", "无法")):
        return "danger"
    if phase == "done" or any(k in state for k in ("订单确认", "下单成功", "已完成", "已提交")):
        return "success"
    if phase in ("attention", "blocked", "paused") or any(k in state for k in ("接管", "待确认", "暂停", "需要", "等待验证", "请")):
        return "warning"
    if phase in ("", "idle", "waiting") and any(k in state for k in ("等待", "空闲", "尚未", "就绪", "已停止")):
        return "neutral"
    return "info"

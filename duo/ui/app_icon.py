"""QPainter-drawn application icon: two phones (Duo) on a blue rounded square."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def paint_icon(painter: QPainter, size: float) -> None:
    s = size / 128.0
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    # background: calm blue gradient on a squircle-ish rounded square
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor("#4aa3f7"))
    grad.setColorAt(1.0, QColor("#0a5fd0"))
    painter.setBrush(grad)
    painter.drawRoundedRect(QRectF(0, 0, size, size), 29 * s, 29 * s)
    # soft highlight at the top
    shine = QLinearGradient(0, 0, 0, size * 0.55)
    shine.setColorAt(0.0, QColor(255, 255, 255, 46))
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(shine)
    painter.drawRoundedRect(QRectF(0, 0, size, size * 0.55), 29 * s, 29 * s)
    # back phone (translucent)
    painter.setBrush(QColor(255, 255, 255, 120))
    painter.drawRoundedRect(QRectF(52 * s, 20 * s, 44 * s, 86 * s), 11 * s, 11 * s)
    # front phone body
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(QRectF(30 * s, 30 * s, 46 * s, 88 * s), 11 * s, 11 * s)
    # front phone screen
    screen = QLinearGradient(0, 38 * s, 0, 110 * s)
    screen.setColorAt(0.0, QColor("#1d7ff0"))
    screen.setColorAt(1.0, QColor("#0b56bd"))
    painter.setBrush(screen)
    painter.drawRoundedRect(QRectF(35 * s, 37 * s, 36 * s, 72 * s), 7 * s, 7 * s)
    # dynamic island + home indicator
    painter.setBrush(QColor(255, 255, 255, 230))
    painter.drawRoundedRect(QRectF(46 * s, 41 * s, 14 * s, 4 * s), 2 * s, 2 * s)
    painter.drawRoundedRect(QRectF(45 * s, 103 * s, 16 * s, 2.4 * s), 1.2 * s, 1.2 * s)
    # check mark badge (precise / verified)
    if size >= 32:
        painter.setBrush(QColor("#1d9a6c"))
        painter.drawEllipse(QPointF(92 * s, 92 * s), 17 * s, 17 * s)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor("#ffffff"), 4.2 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath(QPointF(84 * s, 92.5 * s))
        path.lineTo(90 * s, 98.5 * s)
        path.lineTo(101 * s, 86 * s)
        painter.drawPath(path)


def icon_pixmap(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    paint_icon(painter, size)
    painter.end()
    return pix


def app_icon() -> QIcon:
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(icon_pixmap(size))
    return icon


def write_png(path: Path | str, size: int = 256) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    icon_pixmap(size).save(str(path), "PNG")
    return path

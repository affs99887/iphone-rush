"""Apple-like calm light theme: palette constants, QSS and small styling helpers."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

# ---- palette (brief §9) -------------------------------------------------------
BG = "#f5f5f7"
CARD = "#ffffff"
BORDER = "#e6e8ec"
BORDER_STRONG = "#d2d2d7"
PRIMARY = "#0071e3"
PRIMARY_HOVER = "#0077ed"
PRIMARY_PRESSED = "#006edb"
PRIMARY_SOFT = "#eaf3fd"
SUCCESS = "#1d9a6c"
SUCCESS_SOFT = "#e5f4ed"
WARNING = "#b7791f"
WARNING_SOFT = "#fbf1e0"
DANGER = "#d14343"
DANGER_SOFT = "#fbe9e9"
TEXT = "#1d1d1f"
MUTED = "#6e6e73"
FAINT = "#a1a1a6"
NEUTRAL_SOFT = "#f0f0f3"

FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACKS = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
MONO_FAMILY = "Consolas"
MONO_FALLBACKS = '"Consolas", "Cascadia Mono", "Microsoft YaHei UI", monospace'

RADIUS_CARD = 16
RADIUS_CONTROL = 10

TONES = {
    # tone: (background, foreground)
    "neutral": (NEUTRAL_SOFT, MUTED),
    "info": (PRIMARY_SOFT, PRIMARY),
    "success": (SUCCESS_SOFT, SUCCESS),
    "warning": (WARNING_SOFT, WARNING),
    "danger": (DANGER_SOFT, DANGER),
}


def _asset_dir() -> Path:
    folder = Path(tempfile.gettempdir()) / "duo-ui-2.0"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _render(name: str, size: int, draw) -> str:
    """Render a QSS image (1x and @2x) at runtime so the frozen EXE needs no asset files."""
    folder = _asset_dir()
    for scale, suffix in ((1, ""), (2, "@2x")):
        target = folder / f"{name}{suffix}.png"
        if target.exists():
            continue
        pix = QPixmap(size * scale, size * scale)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(scale, scale)
        draw(painter, size)
        painter.end()
        pix.save(str(target))
    return str(folder / f"{name}.png").replace("\\", "/")


def _draw_check(painter: QPainter, size: int):
    pen = QPen(QColor("#ffffff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    s = size / 18
    path = QPainterPath(QPointF(4.5 * s, 9.5 * s))
    path.lineTo(7.8 * s, 12.6 * s)
    path.lineTo(13.6 * s, 5.8 * s)
    painter.drawPath(path)


def _draw_chevron(color: str, up: bool = False):
    def draw(painter: QPainter, size: int):
        pen = QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        s = size / 10
        top, bottom = (6.5, 3.5) if up else (3.5, 6.5)
        path = QPainterPath(QPointF(2 * s, top * s))
        path.lineTo(5 * s, bottom * s)
        path.lineTo(8 * s, top * s)
        painter.drawPath(path)
    return draw


def build_qss() -> str:
    check = _render("check", 18, _draw_check)
    chevron = _render("chevron", 10, _draw_chevron(MUTED))
    chevron_light = _render("chevron-light", 10, _draw_chevron(FAINT))
    chevron_up = _render("chevron-up", 10, _draw_chevron(MUTED, up=True))
    return f"""
QWidget {{ font-family: {FONT_FALLBACKS}; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#base, QDialog {{ background: {BG}; }}
QWidget#transparent {{ background: transparent; }}
QWidget#settingsPane {{ background: {CARD}; }}

/* ---- cards ---- */
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: {RADIUS_CARD}px; }}
QFrame#card[attention="true"] {{ border: 2px solid {WARNING}; }}
QFrame#card[tone="success"] {{ border: 2px solid {SUCCESS}; }}
QFrame#card[tone="danger"] {{ border: 2px solid {DANGER}; }}
QFrame#subcard {{ background: #fafafc; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#divider {{ background: {BORDER}; border: none; min-height: 1px; max-height: 1px; }}
QFrame#vdivider {{ background: {BORDER}; border: none; min-width: 1px; max-width: 1px; }}

/* ---- text roles ---- */
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 22px; font-weight: 600; }}
QLabel#appTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#cardTitle {{ font-size: 15px; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: 12px; font-weight: 600; color: {MUTED}; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#caption {{ color: {MUTED}; font-size: 12px; }}
QLabel#value {{ font-size: 13px; }}
QLabel#valueStrong {{ font-size: 13px; font-weight: 600; }}
QLabel#mono {{ font-family: {MONO_FALLBACKS}; }}
QLabel#versionTag {{ background: {NEUTRAL_SOFT}; color: {MUTED}; border-radius: 6px; padding: 1px 7px; font-size: 11px; font-weight: 600; }}
QLabel#banner {{ background: {WARNING_SOFT}; color: {WARNING}; border: 1px solid #efd9ad; border-radius: 10px; padding: 6px 12px; font-weight: 600; }}
QLabel#error {{ color: {DANGER}; font-size: 12px; }}
QLabel#link {{ color: {PRIMARY}; }}

/* ---- buttons ---- */
QPushButton {{ background: {CARD}; border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS_CONTROL}px;
               padding: 6px 14px; font-weight: 600; color: {TEXT}; min-height: 20px; }}
QPushButton:hover {{ background: {BG}; border-color: #b8b8bd; }}
QPushButton:pressed {{ background: #e8e8ed; }}
QPushButton:disabled {{ color: {FAINT}; background: {BG}; border-color: {BORDER}; }}
QPushButton#primary {{ background: {PRIMARY}; color: #ffffff; border: 1px solid {PRIMARY}; }}
QPushButton#primary:hover {{ background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; }}
QPushButton#primary:pressed {{ background: {PRIMARY_PRESSED}; }}
QPushButton#primary:disabled {{ background: #b9d7f6; border-color: #b9d7f6; color: #ffffff; }}
QPushButton#danger {{ background: {DANGER}; color: #ffffff; border: 1px solid {DANGER}; }}
QPushButton#danger:hover {{ background: #c53b3b; }}
QPushButton#danger:disabled {{ background: #edb9b9; border-color: #edb9b9; color: #ffffff; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {PRIMARY}; padding: 4px 10px; }}
QPushButton#ghost:hover {{ background: {PRIMARY_SOFT}; }}
QPushButton#ghost:disabled {{ color: {FAINT}; background: transparent; }}
QPushButton#compact {{ padding: 4px 10px; min-height: 18px; font-size: 12px; }}
QPushButton#compact:disabled {{ color: {FAINT}; }}
QPushButton::menu-indicator {{ image: url("{chevron}"); subcontrol-position: right center; subcontrol-origin: padding;
                               width: 10px; height: 10px; left: -8px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 4px 8px; color: {TEXT}; font-weight: 600; }}
QToolButton:hover {{ background: #e8e8ed; }}

/* ---- inputs ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox {{
    background: {CARD}; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 5px 10px; min-height: 20px;
    selection-background-color: {PRIMARY}; selection-color: #ffffff; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QDateTimeEdit:disabled, QComboBox:disabled {{
    color: {FAINT}; background: {BG}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {DANGER}; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 10px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: url("{chevron_light}"); }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; outline: 0; }}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 2px 8px; border-radius: 6px; }}
QDateTimeEdit::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: right center; }}
QDateTimeEdit::down-arrow {{ image: url("{chevron}"); width: 10px; height: 10px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; background: transparent; margin: 2px 4px 0 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; background: transparent; margin: 0 4px 2px 0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{chevron_up}"); width: 9px; height: 9px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{chevron}"); width: 9px; height: 9px; }}
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QDoubleSpinBox::down-arrow:disabled {{ image: url("{chevron_light}"); }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid #c7c7cc; border-radius: 5px; background: {CARD}; }}
QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; image: url("{check}"); }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: {BG}; }}
QCheckBox::indicator:checked:disabled {{ background: #b9d7f6; border-color: #b9d7f6; }}
QCheckBox:disabled {{ color: {FAINT}; }}

/* ---- lists / text ---- */
QListWidget#sideNav {{ background: transparent; border: none; outline: 0; font-size: 13px; }}
QListWidget#sideNav::item {{ padding: 9px 12px; border-radius: 8px; color: {TEXT}; margin: 2px 0; }}
QListWidget#sideNav::item:hover {{ background: #e8e8ed; }}
QListWidget#sideNav::item:selected {{ background: {CARD}; color: {PRIMARY}; font-weight: 600; border: 1px solid {BORDER}; }}
QListView#timeline {{ background: transparent; border: none; outline: 0; }}
QPlainTextEdit#log, QTextEdit#reader {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; padding: 6px 8px;
    color: #3a3a3c; font-family: {MONO_FALLBACKS}; font-size: 12px; selection-background-color: {PRIMARY}; }}
QTextEdit#document {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px 12px; font-size: 13px; line-height: 1.5; }}
QScrollArea#plain {{ background: transparent; border: none; }}
QScrollArea#framed {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c7c7cc; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #aeaeb2; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #c7c7cc; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* ---- menus / tooltips / message boxes ---- */
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {PRIMARY_SOFT}; color: {PRIMARY}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{ background: {TEXT}; color: #ffffff; border: none; padding: 6px 9px; border-radius: 6px; font-size: 12px; }}
QMessageBox {{ background: {CARD}; }}
QMessageBox QLabel {{ font-size: 13px; color: {TEXT}; }}
QMessageBox QPushButton {{ min-width: 76px; }}
QStackedWidget {{ background: transparent; }}
QCalendarWidget QWidget {{ alternate-background-color: {BG}; }}
"""


def apply_theme(app: QApplication) -> None:
    """Fusion + palette + QSS. Call once before building windows."""
    app.setStyle("Fusion")
    font = QFont(FONT_FAMILY, 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor(BG))
    palette.setColor(palette.ColorRole.Base, QColor(CARD))
    palette.setColor(palette.ColorRole.AlternateBase, QColor(BG))
    palette.setColor(palette.ColorRole.Text, QColor(TEXT))
    palette.setColor(palette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(palette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(palette.ColorRole.Highlight, QColor(PRIMARY))
    palette.setColor(palette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(palette.ColorRole.PlaceholderText, QColor(FAINT))
    app.setPalette(palette)
    app.setStyleSheet(build_qss())


def soft_shadow(widget: QWidget, blur: int = 24, alpha: float = 0.08, dy: int = 4) -> QGraphicsDropShadowEffect:
    """Light card shadow (brief: blur 24, alpha 0.08)."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, int(round(255 * alpha))))
    widget.setGraphicsEffect(effect)
    return effect


def tone_colors(tone: str) -> tuple[str, str]:
    return TONES.get(tone, TONES["neutral"])


def repolish(widget: QWidget) -> None:
    """Re-apply QSS after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def mono_font(point_size: int = 10, bold: bool = False) -> QFont:
    font = QFont(MONO_FAMILY, point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setBold(bold)
    return font

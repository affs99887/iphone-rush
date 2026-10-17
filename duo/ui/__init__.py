"""Desktop UI for the Duo reservation assistant 2.0 (PySide6 Widgets + QSS).

Modules:
    theme            palette constants, QSS, apply_theme(), soft shadow helper
    widgets          StatusPill, StepBar, CountdownPill, Card, Timeline, LaneMeter, CheckList, ScreenshotThumb
    operator_card    one card per operator (pickup / delivery)
    dispatcher_panel dispatcher card (policy, submit owner, browser mode, clock, health, timeline)
    settings_dialog  grouped settings dialog (validates Config, saves through the dispatcher)
    dialogs          OTP / consent / page preview dialogs
    main_window      the dashboard window
    stub             ScriptedDispatcher for development, screenshots and UI tests only
    app_icon         QPainter-drawn application icon
"""

APP_NAME = "iPhone Duo 预约助手"
APP_VERSION = "2.0"

# §14.3 no-disturb: when "quiet" is on (--ui-smoke / --live-trial) windows are shown without activation,
# moved off-screen, and nothing ever calls raise_()/activateWindow()/alert()/beep().
UI_FLAGS = {"quiet": False}
OFFSCREEN_POS = (-20000, -20000)


def set_quiet(enabled: bool) -> None:
    UI_FLAGS["quiet"] = bool(enabled)


def is_quiet() -> bool:
    return bool(UI_FLAGS.get("quiet"))

"""iPhone Duo 预约助手 2.0 — thin desktop entry point.

Selects the real `duo.dispatcher.Dispatcher` when it is importable (and `--stub` is absent), otherwise
falls back to `duo.ui.stub.ScriptedDispatcher`, which never touches Apple and is shown with a banner.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).parent / ".vendor"))

# The executable uses the GUI subsystem; browser helper processes also have no console.
if os.name == "nt":
    _popen = subprocess.Popen

    class NoConsolePopen(_popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = NoConsolePopen
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Duo.Reservation.Desktop.2")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPainter  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea  # noqa: E402

from duo.ui import APP_NAME, APP_VERSION, OFFSCREEN_POS, set_quiet  # noqa: E402
from duo.ui.app_icon import app_icon  # noqa: E402
from duo.ui.main_window import MainWindow  # noqa: E402
from duo.ui.theme import apply_theme  # noqa: E402

CHINA = timezone(timedelta(hours=8))
ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
DATA = ROOT / "data"
ARTIFACTS = ROOT / "artifacts"
LOCK_NAME = "duo-reservation-helper-v1"   # shared with 1.5 so two versions never run at once


def console_window_present() -> bool:
    return bool(ctypes.windll.kernel32.GetConsoleWindow()) if os.name == "nt" else False


def create_dispatcher(data_root: Path, testing: bool, force_stub: bool):
    """Real Dispatcher if importable (Worker A), else the UI stub. Returns (dispatcher, class_name, note)."""
    note = ""
    if not force_stub:
        try:
            from duo.dispatcher import Dispatcher  # provided by the engine package
        except Exception as exc:  # ImportError while Worker A's module is missing, or a broken engine
            note = f"引擎不可用：{exc.__class__.__name__}: {exc}"
        else:
            return Dispatcher(data_root, testing), "Dispatcher", ""
    from duo.ui.stub import ScriptedDispatcher
    return ScriptedDispatcher(data_root, testing), "ScriptedDispatcher", note


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def install_crash_handler(app: QApplication, window_ref: dict) -> None:
    """Unhandled exceptions in Qt slots become a message box instead of a console traceback."""
    shown = {"count": 0}

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            (ROOT / "data").mkdir(exist_ok=True)
            with (ROOT / "data" / "ui-errors.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{datetime.now(CHINA).isoformat()}]\n{text}")
        except OSError:
            pass
        window = window_ref.get("window")
        if window is not None and hasattr(window, "drawer"):
            try:
                window.drawer.append(f"界面错误：{exc_type.__name__}: {exc}", "系统")
            except Exception:
                pass
        shown["count"] += 1
        if shown["count"] <= 3:
            QMessageBox.warning(window, "界面出现错误",
                                f"{exc_type.__name__}: {exc}\n\n详情已写入 data/ui-errors.log；两路任务不受影响。")

    sys.excepthook = hook


class Options:
    """Command-line switches. `quiet` (§14.3) is on for every self-check mode."""

    def __init__(self, argv):
        argv = list(argv)
        self.ui_smoke = "--ui-smoke" in argv
        self.live_trial = "--live-trial" in argv
        self.retry_empty_trial = "--retry-empty-trial" in argv
        # Unattended live trial: never send the SMS unless asked; optionally auto-accept the privacy consent.
        self.live_trial_sms = "--live-trial-sms" in argv
        self.live_trial_consent = "--live-trial-consent" in argv
        self.force_stub = "--stub" in argv
        # Attended acceptance: the assistant drives the app's own widgets through files (duo/ui/remote.py).
        self.remote = argv[argv.index("--remote") + 1] if "--remote" in argv and argv.index("--remote") + 1 < len(argv) else ""
        self.testing = self.ui_smoke
        self.quiet = self.ui_smoke or self.live_trial


def build_window(dispatcher, options: Options, dispatcher_class: str = "Dispatcher", note: str = "") -> MainWindow:
    """Create (and show) the main window. In quiet mode it is shown off-screen without activation."""
    set_quiet(options.quiet)
    banner = ""
    if dispatcher_class == "ScriptedDispatcher":
        banner = "演示模式：当前使用脚本化指派员，不会访问 Apple 官网，也不会下单。" + (f"（{note}）" if note else "")
    window = MainWindow(dispatcher, testing=options.testing, banner=banner)
    if dispatcher_class == "ScriptedDispatcher" and not options.force_stub:
        window.setWindowTitle(f"{APP_NAME} {APP_VERSION} · 演示模式")
    if options.quiet:
        # §14.3 no-disturb: never steal focus or appear on the user's desktop during self-checks.
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        window.move(*OFFSCREEN_POS)
    elif options.remote:
        # Remote acceptance: on screen so the user can watch, but never steal focus from their work.
        from duo.ui import UI_FLAGS
        UI_FLAGS["no_focus"] = True
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.show()
    return window


def main() -> int:
    options = Options(sys.argv[1:])
    testing = options.testing
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(app_icon())
    apply_theme(app)

    lock = QLocalServer()
    lock_name = ("duo-helper-" + str(abs(hash(str(ROOT))))) if testing else LOCK_NAME
    socket = QLocalSocket()
    socket.connectToServer(lock_name)
    if socket.waitForConnected(400):
        QMessageBox.information(None, "软件已经运行", "请使用已打开的预约助手窗口（旧版 1.x 也请先关闭）。")
        return 0
    QLocalServer.removeServer(lock_name)
    lock.listen(lock_name)

    data_root = DATA / "test" if testing else DATA
    dispatcher, dispatcher_class, note = create_dispatcher(data_root, testing, options.force_stub)
    window_ref: dict = {}
    install_crash_handler(app, window_ref)
    window = build_window(dispatcher, options, dispatcher_class, note)
    window_ref["window"] = window
    if not options.quiet and not testing and os.name == "nt":
        # Interactive Windows notification for verification codes (typed straight into the notification).
        from duo.winnotify import WindowsCodeToast, register_app_identity
        icon = DATA / "app-icon.png"
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            app_icon().pixmap(128, 128).save(str(icon))
        except OSError:
            icon = None
        register_app_identity(str(icon) if icon else "")
        window.attach_toasts(WindowsCodeToast())
    if dispatcher_class == "ScriptedDispatcher" and testing:
        # Populate the dashboard for screenshots; the stub never touches the network.
        QTimer.singleShot(200, lambda: dispatcher.demo(ARTIFACTS))
    if options.remote:
        from duo.ui.remote import RemoteControl
        window.remote = RemoteControl(window, dispatcher, Path(options.remote))

    if options.live_trial:
        # Exercise the shipped GUI and the real engine against Apple. No substitute shop; trial never submits.
        if hasattr(dispatcher, "set_trial_options"):
            dispatcher.set_trial_options(sms=options.live_trial_sms, auto_consent=options.live_trial_consent)

        def start_trial():
            if options.retry_empty_trial:
                for op in dispatcher.operators():
                    dispatcher.command(op, "trial-retry")
            else:
                dispatcher.trial()
            window.update_primary()
        QTimer.singleShot(700, start_trial)
        report_timer = QTimer(window)

        def live_report():
            try:
                status = dispatcher.status()
            except Exception as exc:
                status = {"error": f"status() failed: {exc}"}
            write_json(ARTIFACTS / "live-trial.json", {
                "time": datetime.now(CHINA).isoformat(), "version": APP_VERSION, "dispatcher_class": dispatcher_class,
                "console_window": console_window_present(), "browser": "bundled-chromium", "quiet": options.quiet,
                "window_position": [window.x(), window.y()], "status": status,
            })
        report_timer.timeout.connect(live_report)
        report_timer.start(1000)
        window.live_report_timer = report_timer

    if testing:
        def capture():
            ARTIFACTS.mkdir(exist_ok=True)
            app.processEvents()
            window.grab().save(str(ARTIFACTS / "panel.png"))
            dialog = window.open_settings(modal=False)
            if dispatcher_class == "ScriptedDispatcher":
                dialog.set_locked(False)   # screenshot the editable form, not the demo's locked state
            app.processEvents()
            composite = window.grab()
            dialog_pix = dialog.grab()
            painter = QPainter(composite)
            painter.fillRect(composite.rect(), QColor(0, 0, 0, 70))
            painter.drawPixmap((composite.width() - dialog_pix.width()) // 2,
                               (composite.height() - dialog_pix.height()) // 2, dialog_pix)
            painter.end()
            composite.save(str(ARTIFACTS / "panel-settings.png"))
            dialog.close()
            dashboard_scrollers = window.centralWidget().findChildren(QScrollArea)
            write_json(ARTIFACTS / "ui-smoke.json", {
                "visible": window.isVisible(), "title": window.windowTitle(), "width": window.width(),
                "height": window.height(), "min_width": window.minimumWidth(), "min_height": window.minimumHeight(),
                "console_window": console_window_present(), "operators": list(window.cards),
                "dispatcher_class": dispatcher_class, "stub": dispatcher_class == "ScriptedDispatcher",
                "version": APP_VERSION, "dashboard_scroll_areas": len(dashboard_scrollers),
                "timeline_entries": window.panel.timeline.model_.rowCount(),
                "quiet": options.quiet, "show_without_activating": window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating),
                "window_position": [window.x(), window.y()], "settings_dialog_position": [dialog.x(), dialog.y()],
                "browser_engine": "bundled Chromium", "live_checkout_test": False,
            })
            window.close()
        QTimer.singleShot(1400, capture)
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # No terminal traceback, including when launched by double-click.
        try:
            (ROOT / "data").mkdir(exist_ok=True)
            (ROOT / "data" / "ui-errors.log").open("a", encoding="utf-8").write(
                f"\n[{datetime.now(CHINA).isoformat()}] startup\n{traceback.format_exc()}")
        except OSError:
            pass
        if QApplication.instance():
            QMessageBox.critical(None, "无法启动", "预约助手启动失败，请检查软件所在目录是否可写，以及软件文件是否完整。\n"
                                                   "详情见 data/ui-errors.log。")
        else:
            ctypes.windll.user32.MessageBoxW(None, "预约助手启动失败，请检查软件文件是否完整。", APP_NAME, 0x10)
        raise SystemExit(1)

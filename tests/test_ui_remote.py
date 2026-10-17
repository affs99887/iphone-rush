"""Remote control channel (--remote) drives the app's own widgets; offscreen, no browser, no network."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from duo import events
from duo.dispatcher import Dispatcher
from duo.ui.main_window import MainWindow
from duo.ui.remote import RemoteControl
from duo.ui.theme import apply_theme

CHINA = timezone(timedelta(hours=8))


def pump(times=8):
    for _ in range(times):
        QCoreApplication.processEvents()


class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.dispatcher = Dispatcher(root / "data", testing=True, start_threads=False, health_interval_ms=0)
        self.window = MainWindow(self.dispatcher, testing=True)
        self.window.show()
        self.folder = root / "remote"
        self.remote = RemoteControl(self.window, self.dispatcher, self.folder)
        self.remote.timer.stop()
        self.remote.box_timer.stop()
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.window.closing = True
        self.window.close()
        self.dispatcher.shutdown()
        pump()

    def send(self, **command):
        n = len(list(self.folder.glob("commands/*.json")))
        (self.folder / "commands" / f"{n:03d}.json").write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
        self.remote.tick()
        pump()

    def queued(self, op):
        return list(self.dispatcher.ops[op].commands.queue)

    def log_text(self):
        return (self.folder / "log.jsonl").read_text(encoding="utf-8")

    def test_otp_command_types_into_the_real_dialog_and_never_logs_the_code(self):
        self.window.on_event(events.operator_otp("delivery", "req-9", "18100000000", "请输入验证码"))
        pump()
        self.send(cmd="otp", op="delivery", code="482913")
        pump(12)
        self.assertIn(("otp", {"request": "req-9", "code": "482913"}), self.queued("delivery"))
        self.assertEqual(self.queued("pickup"), [])
        self.assertNotIn("482913", self.log_text())
        self.remote.write_status()
        self.assertNotIn("482913", (self.folder / "status.json").read_text(encoding="utf-8"))

    def test_consent_and_login_commands_use_the_cards_dialogs(self):
        self.window.on_event(events.operator_consent("pickup", "c-2", "https://secure8.www.apple.com.cn/shop/checkout", "需要你签署"))
        pump()
        self.send(cmd="consent", op="pickup", accept=True)
        self.assertIn(("consent", {"request": "c-2", "accept": True}), self.queued("pickup"))
        self.send(cmd="login", op="pickup", apple_id="a@example.com", password="s3cret-pw")
        login = [c for c in self.queued("pickup") if c[0] == "login"]
        self.assertEqual(login[0][1]["apple_id"], "a@example.com")
        self.assertNotIn("s3cret-pw", self.log_text())

    def test_submit_is_refused_until_the_review_passed_and_then_goes_through_the_confirm_box(self):
        self.send(cmd="submit", op="delivery")
        self.assertIn("not enabled", self.log_text())
        self.assertEqual(self.queued("delivery"), [])
        self.window.on_event(events.operator_run("delivery", True, datetime.now(CHINA).isoformat()))
        self.window.on_event(events.operator_review("delivery", True, [], 16098.0, "", "上海市嘉定区"))
        pump()
        self.remote.box_timer.start(20)                     # the app's confirmation box is accepted by the remote
        self.send(cmd="submit", op="delivery")
        for _ in range(60):
            pump()
            if any(c[0] == "submit" for c in self.queued("delivery")):
                break
        self.remote.box_timer.stop()
        self.assertIn("submit", [c[0] for c in self.queued("delivery")])
        self.assertIn("auto-confirm", self.log_text())

    def test_status_file_reflects_cards(self):
        self.window.on_event(events.operator_review("pickup", False, ["无法确认所选上海门店取货"], None, "", ""))
        pump()
        self.remote.write_status()
        status = json.loads((self.folder / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(set(status["cards"]), {"pickup", "delivery"})
        self.assertIn("未通过", status["cards"]["pickup"]["review"])
        self.assertFalse(status["cards"]["pickup"]["submit_enabled"])


if __name__ == "__main__":
    unittest.main()

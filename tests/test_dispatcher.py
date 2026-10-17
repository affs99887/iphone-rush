"""Dispatcher (指派员) behaviour with recording fake operators; nothing here launches a browser."""
import json
import sys
import tempfile
import time
import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication

from duo import events
from duo.dispatcher import Dispatcher, measure_clock_offset
from duo.model import Config


class FakeOperator(QObject):
    """Records every command; emits nothing unless a test asks it to."""

    event = Signal(dict)

    def __init__(self, folder, op, coordinator, mode):
        super().__init__()
        self.folder, self.name, self.coordinator, self.browser_mode = Path(folder), op, coordinator, mode
        self.sent = []
        self.started = False
        self.heartbeat = time.monotonic()
        self.phase = "idle"
        self.submitted = False
        self.shield_attempts = 0
        self.consecutive_blocks = 0
        self.interrupted = 0

    def start(self):
        self.started = True

    def isRunning(self):  # noqa: N802
        return self.started

    def wait(self, _ms):
        return True

    def interrupt(self):
        self.interrupted += 1
        return True

    def send(self, name, payload=None):
        self.sent.append((name, payload))

    def status(self):
        return {"state": "", "detail": "", "phase": self.phase, "running": False, "paused": False, "trial": False,
                "submitted": self.submitted, "review_ok": False, "browser_mode": self.browser_mode,
                "lanes": {}, "refreshes": 0, "store": "", "checks": {}, "last_image": "", "timeline_ms": {}}

    def commands(self):
        return [name for name, _ in self.sent]


def pump():
    for _ in range(5):
        QCoreApplication.processEvents()


class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dispatcher = Dispatcher(self.root, testing=True, operator_factory=FakeOperator, health_interval_ms=0)
        self.addCleanup(self.dispatcher.shutdown)
        self.events = []
        self.dispatcher.event.connect(self.events.append)
        self.pickup, self.delivery = self.dispatcher.ops["pickup"], self.dispatcher.ops["delivery"]

    def types(self):
        return [e["type"] for e in self.events]

    def test_status_shape_and_isolated_operator_folders(self):
        status = self.dispatcher.status()
        for key in ("state", "policy", "single", "limit", "attempts", "owner", "mode", "error", "browser_mode",
                    "clock_offset_ms", "operators"):
            self.assertIn(key, status)
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["mode"], "parallel")
        self.assertEqual(set(status["operators"]), {"pickup", "delivery"})
        self.assertTrue(all(info["healthy"] for info in status["operators"].values()))
        # 1.x layout is kept so old journals stay valid: pickup = root, delivery = root/delivery.
        self.assertEqual(self.pickup.folder, self.root)
        self.assertEqual(self.delivery.folder, self.root / "delivery")
        self.assertTrue((self.root / "dispatcher.json").exists())

    def test_policy_cannot_change_while_busy_and_persists_across_restart(self):
        self.dispatcher.running["pickup"] = True
        with self.assertRaises(ValueError):
            self.dispatcher.set_policy("single")
        self.dispatcher.running["pickup"] = False
        self.dispatcher.set_policy("single")
        self.assertTrue(self.dispatcher.status()["single"])
        self.assertIn(events.DISPATCHER_POLICY, self.types())
        again = Dispatcher(self.root, testing=True, operator_factory=FakeOperator, start_threads=False, health_interval_ms=0)
        self.addCleanup(again.shutdown)
        self.assertEqual(again.status()["policy"], "single")
        with self.assertRaises(ValueError):
            self.dispatcher.set_policy("random")

    def test_one_click_flow_prepares_then_arms_both_operators(self):
        self.dispatcher.sync_clock = lambda: None       # no network in unit tests
        self.dispatcher.prepare()
        self.dispatcher.arm()
        for op in (self.pickup, self.delivery):
            self.assertEqual(op.commands(), ["prepare", "arm"])
            self.assertIsInstance(op.sent[0][1], Config)
            self.assertEqual(op.sent[0][1].fulfillment, op.name)
        self.assertEqual(self.dispatcher.status()["state"], "preparing")
        for op in (self.pickup, self.delivery):
            op.event.emit(events.operator_finished(op.name, True, "prepared"))
        pump()
        self.assertEqual(self.dispatcher.status()["state"], "armed")
        self.assertTrue(self.dispatcher.is_busy())      # arm is still pending inside the operators
        # Settings stay editable per operator, but a start command while pending is refused.
        with self.assertRaises(ValueError):
            self.dispatcher.command("pickup", "now")

    def test_commands_route_to_one_operator_only(self):
        self.dispatcher.command("pickup", "pause")
        self.assertEqual(self.pickup.commands(), ["pause"])
        self.assertEqual(self.delivery.commands(), [])
        self.dispatcher.command("delivery", "consent", {"request": "r1", "accept": False})
        self.assertEqual(self.delivery.sent[-1], ("consent", {"request": "r1", "accept": False}))
        self.dispatcher.command("delivery", "login", {"apple_id": "a@b.c", "password": "secret"})
        self.assertEqual(self.delivery.sent[-1][0], "login")
        with self.assertRaises(ValueError):
            self.dispatcher.command("pickup", "format-disk")
        with self.assertRaises(ValueError):
            self.dispatcher.command("courier", "pause")
        # The audit timeline never receives the password.
        self.dispatcher._audit({"type": "test", "operator": "delivery", "text": "login secret"})
        timeline = (self.root / "timeline.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("password", json.dumps([c for c in self.delivery.sent if c[0] != "login"]))
        self.assertIn("test", timeline)

    def test_health_monitor_soft_then_hard_then_stops_at_cap(self):
        self.dispatcher.running["pickup"] = True
        self.dispatcher.configs["pickup"].max_restarts = 2
        self.pickup.heartbeat = time.monotonic() - 60
        self.dispatcher.health_check()
        self.assertEqual(self.pickup.sent[-1][0], "restart")
        self.assertFalse(self.pickup.sent[-1][1]["hard"])
        self.assertEqual(self.pickup.interrupted, 1)
        # Within the grace period nothing else is sent.
        self.dispatcher.health_check()
        self.assertEqual(len(self.pickup.sent), 1)
        self.dispatcher.last_restart_at["pickup"] = time.monotonic() - 60
        self.dispatcher.health_check()
        self.assertTrue(self.pickup.sent[-1][1]["hard"])
        self.dispatcher.last_restart_at["pickup"] = time.monotonic() - 60
        self.dispatcher.health_check()
        self.assertEqual(len(self.pickup.sent), 2, "restart cap reached: no third restart")
        attention = [e for e in self.events if e["type"] == events.OPERATOR_ATTENTION]
        self.assertTrue(attention and "上限" in attention[-1]["message"])
        self.assertFalse(self.dispatcher.status()["operators"]["pickup"]["healthy"])
        # A fresh heartbeat marks it healthy again; the idle delivery operator was never touched.
        self.pickup.heartbeat = time.monotonic()
        self.dispatcher.health_check()
        self.assertTrue(self.dispatcher.status()["operators"]["pickup"]["healthy"])
        self.assertEqual(self.delivery.sent, [])

    def test_541_downgrades_to_hidden_only_after_shield_path_is_exhausted(self):
        self.dispatcher.running["pickup"] = True
        blocked = events.operator_network("pickup", 541, "/shop/fulfillment-messages", 1)
        self.pickup.event.emit(blocked)
        pump()
        self.assertNotIn("restart", self.pickup.commands(), "the operator's own re-challenge comes first")
        self.pickup.shield_attempts, self.pickup.consecutive_blocks = 6, 4
        self.pickup.event.emit(blocked)
        pump()
        self.assertEqual(self.pickup.sent[-1][0], "restart")
        self.assertEqual(self.pickup.sent[-1][1]["mode"], "hidden")
        modes = [e for e in self.events if e["type"] == events.DISPATCHER_BROWSER_MODE]
        self.assertEqual((modes[-1]["operator"], modes[-1]["mode"]), ("pickup", "hidden"))
        self.assertTrue(self.dispatcher.downgraded["pickup"])
        self.assertFalse(self.dispatcher.downgraded["delivery"])
        # The user forced headless: no downgrade even when exhausted.
        self.dispatcher.set_browser_mode("headless")
        self.delivery.shield_attempts, self.delivery.consecutive_blocks = 6, 4
        self.dispatcher.running["delivery"] = True
        self.delivery.event.emit(events.operator_network("delivery", 541, "/shop/fulfillment-messages", 5))
        pump()
        self.assertNotIn("restart", self.delivery.commands())

    def test_one_route_needing_login_keeps_both_routes_parallel(self):
        # Separate browser profiles: pickup signs in, delivery stays a guest — two bags, no conflict.
        self.dispatcher.running["pickup"] = self.dispatcher.running["delivery"] = True
        self.pickup.event.emit(events.event(events.INTERNAL_LOGIN_REQUIRED, "pickup", checkout_mode="guest"))
        pump()
        self.assertEqual(self.dispatcher.status()["mode"], "parallel")
        policy = [e for e in self.events if e["type"] == events.DISPATCHER_POLICY]
        self.assertIn("并行", policy[-1]["message"])
        self.assertIn("登录账户", policy[-1]["message"])
        self.assertEqual(self.dispatcher.sequential_queue, [])

    def test_forced_login_switches_to_sequential_account_mode_once(self):
        # Both routes need the Apple ID (one shared bag): only then do they run one after the other.
        self.dispatcher.running["delivery"] = True
        self.dispatcher.login_required.add("pickup")
        self.delivery.event.emit(events.event(events.INTERNAL_LOGIN_REQUIRED, "delivery", checkout_mode="guest"))
        pump()
        self.assertEqual(self.dispatcher.status()["mode"], "sequential-account")
        policy = [e for e in self.events if e["type"] == events.DISPATCHER_POLICY]
        self.assertEqual(policy[-1]["mode"], "sequential-account")
        self.assertIn("顺序", policy[-1]["message"])
        self.assertEqual(self.dispatcher.sequential_queue, ["delivery"])
        count = len(policy)
        self.delivery.event.emit(events.event(events.INTERNAL_LOGIN_REQUIRED, "delivery", checkout_mode="guest"))
        pump()
        self.assertEqual(len([e for e in self.events if e["type"] == events.DISPATCHER_POLICY]), count)

    def test_coordination_event_pings_the_other_running_operator(self):
        self.dispatcher.running["pickup"] = self.dispatcher.running["delivery"] = True
        self.pickup.event.emit(events.event(events.INTERNAL_COORDINATION, "pickup",
                                            status=self.dispatcher.coordinator.status(), peer_blocked=False))
        pump()
        self.assertEqual(self.delivery.commands(), ["check-dispatcher"])
        self.assertNotIn("check-dispatcher", self.pickup.commands())

    def test_operator_events_are_forwarded_and_audited_with_masking(self):
        self.pickup.event.emit(events.operator_log("pickup", "联系 13800000000 已填写", "info"))
        pump()
        forwarded = [e for e in self.events if e["type"] == events.OPERATOR_LOG]
        self.assertEqual(forwarded[-1]["operator"], "pickup")
        timeline = (self.root / "timeline.jsonl").read_text(encoding="utf-8")
        self.assertIn("[手机号]", timeline)
        self.assertNotIn("13800000000", timeline)

    def test_run_and_finish_events_drive_dispatcher_state(self):
        self.pickup.event.emit(events.operator_run("pickup", False, datetime.now(timezone(timedelta(hours=8))).isoformat()))
        pump()
        self.assertTrue(self.dispatcher.running["pickup"])
        self.assertEqual(self.dispatcher.status()["state"], "running")
        self.pickup.event.emit(events.operator_finished("pickup", False, "stopped"))
        pump()
        self.assertFalse(self.dispatcher.running["pickup"])
        self.assertEqual(self.dispatcher.status()["state"], "idle")

    def test_save_config_validates_and_pins_fulfillment(self):
        cfg = Config(fulfillment="pickup", phone="123")
        with self.assertRaises(ValueError):
            self.dispatcher.save_config("delivery", cfg)
        good = Config(fulfillment="pickup")
        self.dispatcher.save_config("delivery", good)
        self.assertEqual(self.dispatcher.config("delivery").fulfillment, "delivery")
        self.assertIsNot(self.dispatcher.config("delivery"), good)


class ClockTests(unittest.TestCase):
    def test_median_offset_from_date_headers(self):
        class Response:
            def __init__(self, when):
                self.headers = {"Date": format_datetime(when, usegmt=True)}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            return Response(datetime.now(timezone.utc) + timedelta(seconds=3))

        offset, source = measure_clock_offset(opener=opener)
        self.assertIsNotNone(offset)
        self.assertTrue(2500 <= offset <= 4000, offset)
        self.assertIn("Date", source)

    def test_failure_falls_back_to_local_clock(self):
        def opener(request, timeout):
            raise OSError("offline")

        offset, source = measure_clock_offset(opener=opener)
        self.assertIsNone(offset)
        self.assertIn("本机", source)


if __name__ == "__main__":
    unittest.main()

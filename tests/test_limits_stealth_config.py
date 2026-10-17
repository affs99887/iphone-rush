"""Limit semantics (§13.1), stealth launch profiles (§8.1/§14), events, Config v4 and operator helpers."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
from PySide6.QtWidgets import QApplication

from duo import events, stealth
from duo.model import Config, retry_delay
from duo.operator import Operator, jittered_backoff
from duo.orders import OrderCoordinator, one_per_customer, per_order_quantity_cap

URL = "https://www.apple.com.cn/shop/checkout"


class LimitSemanticsTests(unittest.TestCase):
    PER_ORDER = ["每单限购1台", "单次只能下单一台", "每笔订单限购 1 件", "每次限购一部", "每个订单限购1台",
                 "一次性最多购买 1 台", "Limit 1 per order", "One item per order"]
    PER_PERSON = ["每人限购1台", "每位顾客限购一部", "每个账户仅可下一单", "同一手机号限购：1 件", "Limit 1 per customer"]

    def test_per_order_caps_never_trigger_single_order_racing(self):
        for text in self.PER_ORDER:
            with self.subTest(text=text):
                self.assertEqual(one_per_customer({"url": URL, "text": text}), "")
                self.assertTrue(per_order_quantity_cap({"url": URL, "text": text}))

    def test_per_person_caps_still_trigger(self):
        for text in self.PER_PERSON:
            with self.subTest(text=text):
                self.assertTrue(one_per_customer({"url": URL, "text": text}))
                self.assertEqual(per_order_quantity_cap({"url": URL, "text": text}), "")

    def test_mixed_sentence_is_per_order_unless_a_separate_per_person_clause_exists(self):
        self.assertEqual(one_per_customer({"url": URL, "text": "每位顾客每单限购 1 台"}), "")
        self.assertEqual(one_per_customer({"url": URL, "text": "同一账户单次只能购买一台"}), "")
        self.assertTrue(one_per_customer({"url": URL, "text": "每单限购1台。每人限购1台。"}))
        self.assertTrue(one_per_customer({"url": URL, "text": "每笔订单限购 1 件；每位顾客限购 1 件"}))

    def test_coordinator_ignores_per_order_cap_but_logs_nothing_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = OrderCoordinator(Path(tmp))
            self.assertFalse(gate.observe({"url": URL, "text": "每单限购1台"}))
            self.assertIsNone(gate.status()["limit"])
            self.assertTrue(gate.claim("pickup")[0])
            self.assertTrue(gate.claim("delivery")[0])
            self.assertTrue(gate.set_mode("sequential-account"))
            self.assertFalse(gate.set_mode("sequential-account"))
            self.assertEqual(OrderCoordinator(Path(tmp)).status()["mode"], "sequential-account")


class StealthTests(unittest.TestCase):
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36"

    def test_user_agent_rewrite_and_version_mapping(self):
        self.assertNotIn("Headless", stealth.rewrite_user_agent(self.UA))
        self.assertEqual(stealth.user_agent_for("153.0.8010.12"), self.UA.replace("HeadlessChrome/", "Chrome/"))
        with self.assertRaises(ValueError):
            stealth.user_agent_for("")

    def test_headless_profile_drops_automation_markers(self):
        kwargs = stealth.launch_kwargs("headless", user_agent=self.UA)
        self.assertTrue(kwargs["headless"])
        self.assertIn("--enable-automation", kwargs["ignore_default_args"])
        self.assertIn("--disable-blink-features=AutomationControlled", kwargs["args"])
        agent = next(a for a in kwargs["args"] if a.startswith("--user-agent="))
        self.assertNotIn("HeadlessChrome", agent)
        self.assertEqual(kwargs["locale"], "zh-CN")
        self.assertEqual(kwargs["timezone_id"], "Asia/Shanghai")
        self.assertFalse(kwargs["accept_downloads"])

    def test_hidden_profile_is_headed_offscreen_and_unthrottled(self):
        kwargs = stealth.launch_kwargs("hidden")
        self.assertFalse(kwargs["headless"])
        self.assertIn(f"--window-position={stealth.OFFSCREEN[0]},{stealth.OFFSCREEN[1]}", kwargs["args"])
        for flag in ("--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                     "--disable-background-timer-throttling", "--no-first-run"):
            self.assertIn(flag, kwargs["args"])
        with self.assertRaises(ValueError):
            stealth.launch_kwargs("visible")

    def test_mode_resolution_and_init_script(self):
        self.assertEqual(stealth.resolve_mode("auto"), "headless")
        self.assertEqual(stealth.resolve_mode("auto", downgraded=True), "hidden")
        self.assertEqual(stealth.resolve_mode("hidden"), "hidden")
        with self.assertRaises(ValueError):
            stealth.resolve_mode("edge")
        for marker in ("webdriver", "languages", "plugins", "window.chrome"):
            self.assertIn(marker, stealth.INIT_SCRIPT)
        self.assertTrue(stealth.looks_blocked("Access Denied You don't have permission"))
        self.assertFalse(stealth.looks_blocked("购买 iPhone Air"))

    def test_resource_blocking_never_touches_apple_scripts_or_shield(self):
        self.assertFalse(Config().block_resources, "bandwidth saver is opt-in (§8.0)")
        self.assertFalse(Operator.should_block("https://www.apple.com.cn/shop/shld/v3/verify.js", "script"))
        self.assertFalse(Operator.should_block("https://www.apple.com.cn/shop/fulfillment-messages", "fetch"))
        self.assertFalse(Operator.should_block("https://www.apple.com.cn/shop/buy-iphone/iphone-duo", "document"))
        self.assertTrue(Operator.should_block("https://store.storeimages.cdn-apple.com/x.jpg", "image"))
        self.assertTrue(Operator.should_block("https://www.google-analytics.com/collect", "xhr"))
        self.assertFalse(Operator.should_block("https://example.org/a.js", "script"))


class WindowHandleTests(unittest.TestCase):
    def test_cloaking_accepts_an_existing_hwnd(self):
        # Live 10-15: "显示浏览器窗口" failed with "cannot be converted to pointer" (HWND(HWND(...))).
        from ctypes import wintypes
        handle = wintypes.HWND(0)
        self.assertIs(stealth.as_hwnd(handle), handle)
        self.assertEqual(stealth.as_hwnd(0).value, None)
        self.assertIsInstance(stealth.cloak_window(handle, False), bool)
        self.assertIsInstance(stealth.is_cloaked(handle), bool)


class EventTests(unittest.TestCase):
    def test_builders_validate_enumerations(self):
        with self.assertRaises(ValueError):
            events.dispatcher_state("sleeping")
        with self.assertRaises(ValueError):
            events.dispatcher_health("pickup", False, "x", "reboot-pc")
        with self.assertRaises(ValueError):
            events.operator_otp("pickup", "r", "18100000000", "m", kind="email")
        with self.assertRaises(ValueError):
            events.dispatcher_policy("adaptive", False, None, None, "", mode="random")
        otp = events.operator_otp("pickup", "r", "18100000000", "m", kind="apple-2fa")
        self.assertEqual(otp["phone_tail"], "0000")
        self.assertNotIn("18100000000", str(otp))
        timing = events.operator_timing("pickup", "阶段完成", 1200, phase="configure", phase_ms=1200)
        self.assertEqual((timing["phase"], timing["phase_ms"]), ("configure", 1200))

    def test_six_step_bar_mapping(self):
        self.assertEqual(events.step_of("fulfillment"), "checkout")
        self.assertEqual(events.step_of("waiting"), "")
        done = events.steps_done("review")
        self.assertTrue(done["entry"] and done["configure"] and done["bag"] and done["checkout"])
        self.assertFalse(done["review"] or done["submit"])
        self.assertTrue(all(events.steps_done("done").values()))


class PerRoutePhoneTests(unittest.TestCase):
    def test_each_route_has_its_own_default_phone(self):
        self.assertEqual(Config().phone, "13800000000")
        self.assertEqual(Config(fulfillment="pickup").phone, "13800000000")
        self.assertEqual(Config(fulfillment="delivery").phone, "13900000000")
        self.assertEqual(Config(fulfillment="delivery", phone="18100000000").phone, "18100000000")
        Config(fulfillment="delivery").validate()

    def test_old_delivery_settings_migrate_to_the_second_phone_but_custom_phones_stay(self):
        old = Config(fulfillment="delivery", phone="13800000000").to_dict()
        old["schema_version"] = 4
        self.assertEqual(Config.from_dict(old).phone, "13900000000")
        custom = dict(old, phone="13900000000")
        self.assertEqual(Config.from_dict(custom).phone, "13900000000")
        pickup = dict(Config(fulfillment="pickup").to_dict(), schema_version=4)
        self.assertEqual(Config.from_dict(pickup).phone, "13800000000")
        # Once saved at schema 5, a deliberately chosen 13800000000 for delivery is respected.
        self.assertEqual(Config.from_dict(dict(old, schema_version=5)).phone, "13800000000")

    def test_dispatcher_gives_each_route_its_phone(self):
        from duo.dispatcher import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = Dispatcher(Path(tmp), testing=True, start_threads=False, health_interval_ms=0)
            try:
                self.assertEqual(dispatcher.config("pickup").phone, "13800000000")
                self.assertEqual(dispatcher.config("delivery").phone, "13900000000")
            finally:
                dispatcher.shutdown()


class ConfigV4Tests(unittest.TestCase):
    def test_v3_settings_migrate_and_checkout_mode_mirrors_guest_flag(self):
        old = Config().to_dict()
        old["schema_version"] = 3
        old["guest_checkout"] = False
        for key in ("browser_mode", "prewarm_minutes", "clock_sync", "max_restarts", "headless_fallback", "block_resources", "checkout_mode"):
            old.pop(key, None)
        cfg = Config.from_dict(old)
        self.assertEqual(cfg.schema_version, 7)
        self.assertEqual(Config(fulfillment="delivery").shipping_postcode, "200000")
        self.assertEqual(Config.from_dict(dict(old, shipping_postcode="")).shipping_postcode, "200000")
        self.assertEqual(Config.from_dict(dict(old, shipping_postcode="201800")).shipping_postcode, "201800",
                         "a postcode the user entered is kept")
        self.assertTrue(cfg.auto_submit and cfg.auto_consent, "older settings adopt automatic terms + submission")
        self.assertEqual(cfg.checkout_mode, "account")
        self.assertEqual(cfg.browser_mode, "auto")
        cfg.checkout_mode = "guest"
        self.assertTrue(cfg.guest_checkout)
        with self.assertRaises(ValueError):
            cfg.checkout_mode = "anonymous"
        self.assertEqual(Config.from_dict({"schema_version": 4, "checkout_mode": "account"}).guest_checkout, False)

    def test_copy_is_deep_and_validation_covers_new_fields(self):
        cfg = Config()
        clone = cfg.copy()
        clone.stores.clear()
        self.assertTrue(cfg.stores)
        bad = Config(browser_mode="edge")
        with self.assertRaises(ValueError):
            bad.validate()
        bad = Config(max_restarts=99)
        with self.assertRaises(ValueError):
            bad.validate()
        self.assertEqual(Config(prewarm_minutes=10).effective_prewarm_minutes, 30, "shield must be warm ≥ 30 min before T")
        self.assertEqual(Config(prewarm_minutes=0).effective_prewarm_minutes, 0)
        self.assertEqual(Config(prewarm_minutes=45).effective_prewarm_minutes, 45)

    def test_541_shares_the_block_backoff(self):
        self.assertGreaterEqual(retry_delay(1.8, 0, 541), 15)
        self.assertLessEqual(retry_delay(1.8, 9, 541), 60)
        self.assertEqual(retry_delay(1.8, 0, 200), 1.8)


class OperatorHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_phase_mapping_follows_apple_checkout_stages(self):
        f = Operator.phase_for
        self.assertEqual(f("https://www.apple.com.cn/shop/buy-iphone/iphone-duo", "select", False, "/shop/buy-iphone/iphone-duo", "entry"), "configure")
        self.assertEqual(f("https://www.apple.com.cn/shop/buy-iphone/iphone-duo", "wait", True, "/shop/buy-iphone/iphone-duo", "add"), "add")
        self.assertEqual(f("https://www.apple.com.cn/shop/bag", "click", True, "/shop/buy-iphone/iphone-duo", "add"), "bag")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init", "select", True, "x", "bag"), "fulfillment")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout?_s=PickupContact-init", "fill", True, "x", "fulfillment"), "contact")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout?_s=Billing-init", "select", True, "x", "contact"), "payment")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout?_s=Review", "review", True, "x", "payment"), "review")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout/thankyou", "complete", True, "x", "submit"), "done")
        self.assertEqual(f("https://secure8.www.apple.com.cn/shop/checkout?_s=PickupContact", "otp", True, "x", "contact"), "verify")

    def test_backoff_is_bounded_with_jitter(self):
        values = [jittered_backoff(n) for n in range(1, 12)]
        self.assertTrue(all(2.5 <= v <= 21.5 for v in values), values)
        self.assertLess(jittered_backoff(1), jittered_backoff(5) + 1.5)

    def test_phase_timeline_and_status_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = Operator(Path(tmp), "pickup")
            seen = []
            op.event.connect(seen.append)
            op.run_started = time.monotonic()
            op.set_phase("entry")
            time.sleep(0.02)
            op.set_phase("configure")
            self.assertIn("entry", op.timeline_ms)
            self.assertGreaterEqual(op.timeline_ms["entry"], 15)
            timing = [e for e in seen if e["type"] == events.OPERATOR_TIMING]
            self.assertEqual(timing[-1]["phase"], "entry")
            status = op.status()
            for key in ("state", "phase", "running", "paused", "review_ok", "browser_mode", "lanes", "timeline_ms",
                        "shield", "checkout_mode", "heartbeat_age", "restarts", "add_attempts"):
                self.assertIn(key, status)
            self.assertEqual(status["phase"], "configure")
            self.assertEqual(status["checkout_mode"], "guest")

    def test_journal_records_mode_and_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = Operator(Path(tmp), "delivery", browser_mode="headless")
            op.checkpoint("add-attempt", attempts=2)
            journal = op.read_journal()
            self.assertEqual((journal["phase"], journal["attempts"], journal["browser_mode"], journal["browser"]),
                             ("add-attempt", 2, "headless", "chromium"))


if __name__ == "__main__":
    unittest.main()

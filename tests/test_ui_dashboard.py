"""Dashboard UI on the real Dispatcher with unstarted operators (offscreen; no browser, no network)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from duo import events
from duo.dispatcher import Dispatcher
from duo.ui import OFFSCREEN_POS, set_quiet
from duo.ui.main_window import MainWindow
from duo.ui.theme import apply_theme

CHINA = timezone(timedelta(hours=8))


def pump(times=6):
    for _ in range(times):
        QCoreApplication.processEvents()


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dispatcher = Dispatcher(Path(self.temp.name), testing=True, start_threads=False, health_interval_ms=0)
        self.window = MainWindow(self.dispatcher, testing=True)
        self.window.show()
        pump()
        self.addCleanup(self._teardown)

    def _teardown(self):
        set_quiet(False)
        self.window.closing = True
        self.window.close()
        self.dispatcher.shutdown()
        pump()

    def queued(self, op):
        return list(self.dispatcher.ops[op].commands.queue)

    def emit(self, event):
        self.window.on_event(event)
        pump()

    def test_dashboard_has_two_cards_a_dispatcher_panel_and_no_scrolling(self):
        self.assertEqual(set(self.window.cards), {"pickup", "delivery"})
        self.assertEqual(self.window.centralWidget().findChildren(QScrollArea), [])
        self.assertIn("2.0", self.window.windowTitle())
        self.assertEqual(self.window.primary_button.text(), "一键布防")
        self.assertIn("无头", self.window.cards["pickup"].mode_chip.text() or "无头")

    def test_submit_enabled_only_after_valid_review_on_that_card(self):
        pickup, delivery = self.window.cards["pickup"], self.window.cards["delivery"]
        self.assertFalse(pickup.submit_button.isEnabled())
        self.emit(events.operator_run("pickup", False, datetime.now(CHINA).isoformat()))
        self.assertTrue(pickup.running)
        self.emit(events.operator_review("pickup", True, [], 24098.0, "静安", "上海 静安"))
        self.assertTrue(pickup.submit_button.isEnabled())
        self.assertFalse(delivery.submit_button.isEnabled())
        self.assertIn("24,098", pickup.review_label.text())
        self.emit(events.operator_review("pickup", False, ["订单总价未确认或超出金额上限"], None, "静安", ""))
        self.assertFalse(pickup.submit_button.isEnabled())
        self.assertIn("未通过", pickup.review_label.text())
        self.emit(events.operator_review("pickup", True, [], 24098.0, "静安", "上海 静安"))
        self.assertTrue(pickup.submit_button.isEnabled())
        self.emit(events.operator_finished("pickup", False, "stopped"))
        self.assertFalse(pickup.submit_button.isEnabled())

    def otps(self, op):
        return [c for c in self.queued(op) if c[0] == "otp"]

    def test_code_notification_is_labelled_and_auto_submits_six_digits_once(self):
        self.emit(events.operator_otp("pickup", "req-1", "18100000000", "请输入验证码"))
        notice = self.window.notices.active("pickup")
        self.assertIsNotNone(notice)
        self.assertIn("门店自取", notice.title.text())
        self.assertIn("短信验证码", notice.title.text())
        self.assertIn("138****0000", notice.purpose.text(), "says which phone this route's code goes to")
        self.assertIn("等待验证码", self.window.cards["pickup"].pill.text())
        notice.code.setText("12345")
        pump()
        self.assertEqual(self.otps("pickup"), [])
        notice.code.setText("123456")
        pump(10)
        self.assertEqual(self.otps("pickup"), [("otp", {"request": "req-1", "code": "123456"})])
        self.assertEqual(self.queued("delivery"), [])
        self.assertEqual(notice.state, "submitted")
        self.assertEqual(notice.code.text(), "", "the code is cleared from the widget")
        notice.enter_code("999999", "windows")           # a late second source never resubmits
        pump()
        self.assertEqual(len(self.otps("pickup")), 1)
        # "等待官网处理" is not a verdict yet; Apple accepted it once the operator moves on.
        self.emit(events.operator_state("pickup", "等待官网处理", "等待官网处理完上一步请求再操作。", "contact"))
        self.assertEqual(notice.state, "submitted")
        self.emit(events.operator_state("pickup", "自动操作", "继续结账流程。", "contact"))
        self.assertEqual(notice.state, "accepted")
        self.assertIn(notice, self.window.notices.cards, "shows 验证通过 briefly")
        # …then closes by itself (user request 10-15).
        for _ in range(400):
            if notice not in self.window.notices.cards:
                break
            QTest.qWait(10)
        self.assertNotIn(notice, self.window.notices.cards)

    def test_notifications_stack_newest_at_the_bottom_and_never_auto_close(self):
        self.window.resize(1240, 820)
        pump()
        self.emit(events.operator_otp("pickup", "p-1", "18100000000", "请输入验证码"))
        self.emit(events.operator_otp("delivery", "d-1", "18100000000", "两步验证", kind="apple-2fa"))
        pump(20)
        first, second = self.window.notices.cards
        self.assertEqual((first.operator, second.operator), ("pickup", "delivery"))
        self.assertIn("双重认证", second.title.text())
        self.assertIn("送货到家", second.title.text())
        targets = self.window.notices.targets()
        self.assertLess(targets[first.key].y(), targets[second.key].y(), "the older card moved up")
        self.assertGreater(targets[second.key].x(), self.window.width() // 2, "bottom-right corner")
        # A newer request for the same route supersedes the old card but does not delete it.
        self.emit(events.operator_otp("pickup", "p-2", "18100000000", "验证码不正确", error=True))
        pump(20)
        self.assertEqual(first.state, "stale")
        self.assertEqual(len(self.window.notices.cards), 3)
        self.assertEqual(self.window.notices.cards[-1].request, "p-2")
        self.assertTrue(self.window.notices.cards[-1].warning.isVisible())

    def test_two_cards_arriving_together_both_end_fully_opaque(self):
        # Live 10-15: the two routes asked for codes 120 ms apart; the older card stayed semi-transparent
        # because restacking it stopped its entrance animation half-way.
        self.window.resize(1240, 820)
        self.window.notices.animate = True
        pump()
        self.emit(events.operator_otp("pickup", "p-1", "18100000000", "请输入验证码"))
        QTest.qWait(120)
        self.emit(events.operator_otp("delivery", "d-1", "18100000000", "请输入验证码"))
        QTest.qWait(900)
        first, second = self.window.notices.cards
        for card in (first, second):
            self.assertFalse(card.opacity.isEnabled(), card.operator)
            self.assertEqual(card.opacity.opacity(), 1.0, card.operator)
            self.assertFalse(card.property("entering"), card.operator)
        targets = self.window.notices.targets()
        self.assertEqual(first.pos(), targets[first.key])
        self.assertEqual(second.pos(), targets[second.key])

    def test_one_click_on_close_closes_and_the_route_offers_to_re_enter(self):
        self.emit(events.operator_otp("delivery", "req-2", "18100000000", "请输入验证码"))
        notice = self.window.notices.active("delivery")
        self.assertIn("139****0000", notice.purpose.text(), "the delivery route has its own phone")
        notice.close_button.click()
        pump(10)
        self.assertEqual(self.otps("delivery"), [("otp", {"request": "req-2", "code": ""})], "verification paused")
        self.assertIsNone(self.window.notices.active("delivery"), "closed with a single click")
        for _ in range(100):                                  # the card fades out before it is removed
            if notice not in self.window.notices.cards:
                break
            QTest.qWait(10)
        self.assertNotIn(notice, self.window.notices.cards)
        route = self.window.cards["delivery"]
        self.assertTrue(route.suggest_button.isVisible())
        self.assertEqual(route.suggest_button.text(), "重新输入验证码")
        # The operator's pause notice keeps the offer; one click asks the operator for the code again.
        self.emit(events.operator_attention("delivery", "已暂停验证码输入，点继续可再次输入，或直接在官网验证。",
                                            "收到短信后点“继续”，我会重新弹出输入框。"))
        self.assertEqual(route.suggest_button.text(), "重新输入验证码")
        route.suggest_button.click()
        pump()
        self.assertIn("resume", [c[0] for c in self.queued("delivery")])
        # Closing an already-handled card sends nothing.
        self.emit(events.operator_otp("pickup", "req-5", "18100000000", "请输入验证码"))
        done = self.window.notices.active("pickup")
        done.code.setText("112233")
        pump(10)
        sent = len(self.otps("pickup"))
        done.close_button.click()
        pump(10)
        self.assertEqual(len(self.otps("pickup")), sent)

    def test_code_from_the_windows_notification_is_submitted_through_the_same_path(self):
        class FakeToasts(QObject):
            code_entered = Signal(str, str)
            open_requested = Signal(str)
            helper_event = Signal(str, dict)

            def __init__(self):
                super().__init__()
                self.shown, self.cancelled, self.forgotten = [], [], []

            def show_code_request(self, key, title, body):
                self.shown.append((key, title, body))
                return True

            def cancel(self, key):
                self.cancelled.append(key)

            def forget(self, key):
                self.forgotten.append(key)

            def shutdown(self):
                pass

        toasts = FakeToasts()
        self.window.attach_toasts(toasts)
        self.emit(events.operator_otp("delivery", "req-3", "18100000000", "请输入验证码"))
        key, title, body = toasts.shown[-1]
        self.assertIn("送货到家", title)
        self.assertIn("短信验证码", title)
        self.assertIn("139****0000", body)
        toasts.code_entered.emit(key, "246810")
        pump(10)
        self.assertEqual(self.otps("delivery"), [("otp", {"request": "req-3", "code": "246810"})])
        self.assertEqual(self.window.notices.get(key).state, "submitted")
        self.assertIn(key, toasts.forgotten)
        # Typing in the window first instead cancels the Windows notification.
        self.emit(events.operator_otp("pickup", "req-4", "18100000000", "请输入验证码"))
        key2 = toasts.shown[-1][0]
        self.window.notices.get(key2).code.setText("135791")
        pump(10)
        self.assertIn(key2, toasts.cancelled)
        toasts.code_entered.emit(key2, "000000")
        pump(10)
        self.assertEqual(len(self.otps("pickup")), 1, "never submitted twice")

    def test_settings_round_trip_and_validation_message(self):
        dialog = self.window.open_settings(modal=False)
        self.assertEqual(dialog.phones["delivery"].text(), "13900000000", "each route has its own phone")
        dialog.phone.setText("18100000001")
        dialog.checkout_mode["pickup"].setCurrentIndex(dialog.checkout_mode["pickup"].findData("account"))
        configs = dialog.collect()
        self.assertEqual(configs["pickup"].phone, "18100000001")
        self.assertEqual(configs["delivery"].phone, "13900000000", "editing one route's phone leaves the other alone")
        self.assertEqual(configs["pickup"].checkout_mode, "account")
        self.assertEqual(configs["delivery"].checkout_mode, "guest")
        self.assertEqual((configs["pickup"].fulfillment, configs["delivery"].fulfillment), ("pickup", "delivery"))
        dialog.save()
        pump()
        self.assertEqual(self.dispatcher.config("pickup").phone, "18100000001")
        self.assertFalse(self.dispatcher.config("pickup").guest_checkout)
        dialog = self.window.open_settings(modal=False)
        dialog.phone.setText("123")
        with self.assertRaises(ValueError) as caught:
            dialog.collect()
        self.assertIn("门店自取", str(caught.exception))
        dialog.close()

    def test_login_dialog_hands_over_password_once_and_keeps_nothing(self):
        card = self.window.cards["pickup"]
        dialog = card.open_login()
        received = []
        dialog.submitted.connect(lambda a, p: received.append((a, p)))
        dialog.apple_id.setText("someone@example.com")
        self.assertFalse(dialog.login.isEnabled())
        dialog.password.setText("hunter2-secret")
        self.assertTrue(dialog.login.isEnabled())
        dialog.send()
        pump(10)
        self.assertEqual(received, [("someone@example.com", "hunter2-secret")])
        self.assertEqual(dialog.password.text(), "")
        login = [c for c in self.queued("pickup") if c[0] == "login"]
        self.assertEqual(login[0][1]["apple_id"], "someone@example.com")
        self.assertEqual(card.apple_id_hint, "someone@example.com")
        self.assertNotIn("hunter2-secret", repr({k: v for k, v in vars(card).items() if isinstance(v, (str, dict, list))}))
        self.assertIsNone(card.login_dialog)
        self.assertIn("登录中", card.pill.text())

    def test_speed_table_and_action_timing_accept_both_event_shapes(self):
        card = self.window.cards["pickup"]
        self.emit(events.operator_run("pickup", False, datetime.now(CHINA).isoformat()))
        self.emit(events.operator_timing("pickup", "阶段完成：configure", 1500, phase="configure", phase_ms=1500))
        self.assertTrue(card.speed.phase_ms, "phase timing populates the speed table")
        self.emit({"type": "operator.timing", "operator": "pickup", "time": datetime.now(CHINA).isoformat(),
                   "action": "选择 1TB。", "ms": 230})
        self.assertIn("230 ms", card.timing.text())
        self.window.sync_status(full=True)
        pump()

    def test_attention_offers_one_click_login_and_sequential_mode_is_shown(self):
        card = self.window.cards["delivery"]
        self.emit(events.operator_attention("delivery", "Apple 账户尚未登录。", "点击“登录账户”，在软件内输入 Apple ID 与密码。"))
        self.assertTrue(card.attention)
        self.assertTrue(card.suggest_button.isVisible())
        self.assertEqual(card.suggest_button.text(), "登录账户")
        self.assertEqual(card.pill.text(), "请接管")
        self.emit(events.dispatcher_policy("adaptive", False, None, None, "官网要求登录", mode="sequential-account"))
        self.assertEqual(self.window.panel.mode.text(), "同账号顺序下单")
        self.emit(events.dispatcher_policy("adaptive", False, None, "pickup", "", mode="parallel"))
        self.assertIn("门店自取", self.window.panel.owner.text())

    def test_consent_dialog_routes_the_users_decision(self):
        card = self.window.cards["pickup"]
        self.emit(events.operator_consent("pickup", "c-1", "https://secure8.www.apple.com.cn/shop/checkout", "需要你签署本知情同意书…"))
        self.assertIsNotNone(card.consent_dialog)
        card.consent_dialog.decided.emit("c-1", True)
        pump()
        self.assertIn(("consent", {"request": "c-1", "accept": True}), self.queued("pickup"))
        self.assertEqual(self.queued("delivery"), [])

    def test_quiet_mode_window_never_activates_or_appears_on_screen(self):
        set_quiet(True)
        try:
            quiet = MainWindow(self.dispatcher, testing=True)
            quiet.show()
            pump()
            self.assertTrue(quiet.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
            self.assertEqual((quiet.x(), quiet.y()), OFFSCREEN_POS)
            quiet.closing = True
            quiet.close()
        finally:
            set_quiet(False)

    def test_network_and_browser_mode_events_update_chips(self):
        card = self.window.cards["pickup"]
        self.emit(events.operator_network("pickup", 541, "/shop/fulfillment-messages", 2))
        self.assertTrue(card.network_chip.isVisible())
        self.assertIn("541", card.network_chip.text())
        self.emit(events.dispatcher_browser_mode("pickup", "hidden", "安全盾重新握手仍返回 HTTP 541"))
        self.assertIn("隐藏", card.mode_chip.text())


if __name__ == "__main__":
    unittest.main()

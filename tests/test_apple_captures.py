"""Regression expectations grounded in DOMs captured on Apple's actual site.

These are DOM replay tests, not claims of future preorder/server availability.
No fixture is generated from our demo server or from expected button strings.
"""
import json
import unittest
from pathlib import Path

from duo.browser import Adapter
from duo.model import Config, ProductTarget

FIXTURES = Path(__file__).parent / "fixtures" / "apple-live-2026-10-05"
AIR = ProductTarget("iPhone Air", "云白色", "/shop/buy-iphone/iphone-air")


def capture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


class AppleCaptureTests(unittest.TestCase):
    def setUp(self):
        self.air = Adapter(Config(), AIR)

    def test_duo_presale_does_not_add(self):
        adapter = Adapter(Config())
        snap = capture("duo-presale")
        self.assertEqual(adapter.plan(snap).kind, "select")
        self.assertTrue(adapter.selected_checks(snap)["capacity"])

    def test_real_duo_available_options_are_selected_during_presale(self):
        adapter = Adapter(Config())
        for file, field in [("duo-live-start", "tradeupinline"), ("duo-live-no-trade", "applecare-options")]:
            decision = adapter.plan(capture(file))
            self.assertEqual(decision.kind, "select")
            self.assertEqual(decision.node["name"], field)
            self.assertFalse(decision.node["disabled"])
        self.assertEqual(adapter.plan(capture("duo-live-configured")).kind, "refresh")

    def test_in_stock_apple_page_unlocks_controls_progressively(self):
        for name, field in [("air-start", "dimensionColor"),
                            ("air-color", "dimensionCapacity"),
                            ("air-capacity", "tradeupinline"),
                            ("air-no-trade", "applecare-options")]:
            with self.subTest(name=name):
                snap = capture(name)
                decision = self.air.plan(snap)
                self.assertEqual(decision.kind, "select")
                self.assertEqual(decision.node["name"], field)
                self.assertFalse(decision.node["disabled"])

    def test_real_price_and_footnote_in_selected_capacity(self):
        snap = capture("air-capacity")
        label = next(n["label"] for n in snap["nodes"]
                     if n["name"] == "dimensionCapacity" and n["selected"])
        self.assertIn("脚注", label)
        self.assertIn("14,299", label)
        self.assertTrue(self.air.selected_checks(snap)["capacity"])

    def test_real_add_button_only_after_config_confirmed(self):
        snap = capture("air-configured")
        self.assertTrue(all(self.air.selected_checks(snap).values()))
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "add")
        self.assertEqual(decision.node["name"], "add-to-cart")
        self.air.added = True
        self.assertEqual(self.air.plan(snap).kind, "manual")

    def test_enabled_ssr_button_does_not_add_before_purchase_scripts_load(self):
        snap = capture("air-configured")
        snap["ready_state"] = "interactive"
        self.assertEqual(self.air.plan(snap).kind, "wait")
        snap["ready_state"] = "complete"
        self.assertEqual(self.air.plan(snap).kind, "add")

    def test_accessory_attach_page_goes_to_bag(self):
        self.air.product_verified = self.air.added = True
        decision = self.air.plan(capture("air-after-add"))
        self.assertEqual(decision.kind, "click")
        self.assertEqual(decision.node["label"], "查看购物袋")

    def test_bag_recommendations_are_not_purchased_items(self):
        snap = capture("air-bag")
        self.assertIn("保护壳", snap["text"])
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "click")
        self.assertEqual(decision.node["label"], "结账")
        # The production Duo target still refuses the Air test basket.
        self.assertEqual(Adapter(Config()).plan(snap).kind, "manual")

    def test_bag_missing_applecare_is_healed_in_the_bag_not_re_added(self):
        # Live 10-14 capture: the add raced Apple's async AC+ attach; the bag shows the phone and an
        # "添加 AppleCare+" upsell whose text also says "AppleCare+". Text presence must not pass the check.
        snap = json.loads((FIXTURES.parent / "apple-live-2026-10-14" / "air-bag-missing-care.json").read_text(encoding="utf-8"))
        self.assertIn("AppleCare+", snap["summary"])
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "click")
        self.assertTrue(decision.node["id"].endswith(".add"))
        self.assertIn("AppleCare+", decision.node["label"])
        self.assertFalse(self.air.product_verified)
        # One click per control: if the bag still lacks AC+ afterwards the user is asked, never a re-add.
        self.air.last_clicked = decision.node["selector"]
        again = self.air.plan(snap)
        self.assertEqual(again.kind, "manual")
        self.assertIn("缺少 AppleCare+", again.message)
        self.assertNotEqual(self.air.plan(capture("air-bag")).kind, "manual", "a bag with the real AC+ line still checks out")

    def test_real_signin_and_consent_pause_immediately(self):
        # Account checkout on the real sign-in page: the operator must stop and ask for a login (§13.2),
        # never type into Apple's form on its own.
        self.air.cfg.guest_checkout = False
        self.assertEqual(self.air.plan(capture("air-checkout-start")).kind, "login-required")
        decision = self.air.plan(capture("air-fulfillment"))
        self.assertEqual(decision.kind, "manual")
        self.assertIn("知情同意书", decision.message)

    def test_guest_checkout_uses_actual_apple_control(self):
        dec = self.air.plan(capture("air-checkout-start"))
        self.assertEqual((dec.kind, dec.node["id"]), ("click", "signIn.guestLogin.guestLogin"))

    def test_delivery_uses_real_apple_options_without_picking_a_store(self):
        adapter = Adapter(Config(fulfillment="delivery"), AIR)
        snap = capture("air-delivery-options")
        dec = adapter.plan(snap)
        self.assertTrue(adapter.delivery_verified)
        self.assertFalse(adapter.chosen_store)
        self.assertIn(dec.kind, ("click", "select"))
        if dec.kind == "select":
            self.assertIn("shippingOptions", dec.node["name"])
            self.assertIn("免费", dec.node["label"])
            for n in snap["nodes"]:
                if "shippingOptions" in n["name"]:
                    n["selected"] = n["selector"] == dec.node["selector"]
            dec = adapter.plan(snap)
        self.assertEqual(dec.node["label"], "继续填写送货地址")

    def test_real_pickup_tab_is_aria_radio_button(self):
        decision = self.air.plan(capture("air-delivery-options"))
        self.assertEqual(decision.kind, "select")
        self.assertEqual(decision.node["label"], "我要取货")
        self.assertEqual(decision.node["kind"], "radio")

    def test_store_choice_is_based_on_live_availability(self):
        snap = capture("air-stores-ready")
        unavailable = [n for n in snap["nodes"] if "静安" in n["label"]]
        self.assertTrue(unavailable)
        self.assertTrue(all(n["disabled"] for n in unavailable))
        decision = self.air.plan(snap)
        self.assertEqual((decision.kind, decision.value), ("store", "环球港"))

    def test_wait_for_actual_processing_state(self):
        self.assertEqual(self.air.plan(capture("air-stores")).kind, "wait")

    def test_apples_order_pages_after_submit(self):
        live = FIXTURES.parent / "apple-live-2026-10-15"
        load = lambda name: json.loads((live / (name + ".json")).read_text(encoding="utf-8"))
        status = self.air.plan(load("order-status"))
        self.assertEqual(status.kind, "wait")
        done = self.air.plan(load("order-thankyou"))
        self.assertEqual(done.kind, "complete")
        self.assertEqual(self.air.order_number, "W0000000001")
        self.assertIn("掌上生活", done.message)
        # The review page right after the click still offers 立即下单: only review() may handle it.
        self.assertEqual(self.air.plan(load("review-after-submit")).kind, "review")

    def test_pickup_waits_while_apple_still_shows_the_delivery_panel(self):
        # Live 10-15: 我要取货 already selected, no "正在处理", but the page still showed 送货至… /
        # 继续填写送货地址 for a moment. That is "store list not loaded yet", never "please take over".
        snap = capture("air-stores")
        snap["text"] = snap["text"].replace("正在处理", "")
        snap["summary"] = snap.get("summary", "").replace("正在处理", "")
        snap["busy"] = False
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "wait")
        self.assertIn("门店列表", decision.message)
        busy = dict(snap, checkout_busy=True)
        self.assertEqual(self.air.plan(busy).kind, "wait")
        ready = self.air.plan(capture("air-stores-ready"))
        self.assertEqual((ready.kind, ready.value), ("store", "环球港"))

    def test_expand_other_stores_only_when_visible_choices_unavailable(self):
        # Fault injection into the recorded page; not an observed live shortage.
        snap = capture("air-stores-ready")
        for n in snap["nodes"]:
            if n["name"] == "store-locator-result":
                n["disabled"] = True
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "click")
        self.assertEqual(decision.node["label"], "显示更多取货地点")
        self.air.last_clicked = decision.node["selector"]
        self.assertEqual(self.air.plan(snap).kind, "stock")

    def test_selected_store_uses_actual_continue_label(self):
        decision = self.air.plan(capture("air-store-ready"))
        self.assertEqual(self.air.chosen_store, "环球港")
        self.assertEqual(decision.node["label"], "继续填写取货详情")

    def test_identity_explanation_does_not_block_contact_autofill(self):
        self.air.cfg.email = ""
        self.air.chosen_store = "环球港"
        snap = capture("air-contact-empty")
        self.assertIn("验证你的身份", snap["text"])
        for field in ["lastName", "firstName", "fullDaytimePhone"]:
            decision = self.air.plan(snap)
            self.assertEqual(decision.kind, "fill")
            self.assertEqual(decision.node["name"], field)
            decision.node["value"] = decision.value
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "manual")
        self.assertIn("电子邮箱", decision.message)

    def test_phone_verify_sends_once_after_known_fields(self):
        self.air.chosen_store = "环球港"
        self.air.cfg.email = "fixture@example.invalid"
        snap = capture("air-contact-empty")
        for _ in range(4):
            decision = self.air.plan(snap)
            self.assertEqual(decision.kind, "fill")
            decision.node["value"] = decision.value
        decision = self.air.plan(snap)
        self.assertEqual(decision.kind, "wait")  # Recorded empty page has a disabled verify button.
        verify = next(n for n in snap["nodes"] if n["label"] == "验证")
        verify["disabled"] = False  # Controlled state-transition injection, not live evidence.
        self.assertEqual(self.air.plan(snap).kind, "send-code")
        self.air.sms_requested = True
        self.assertEqual(self.air.plan(snap).kind, "manual")

    def test_cmb_selected_page_passes_irrelevant_other_bank_search(self):
        self.air.chosen_store = "环球港"
        snap = capture("air-cmb-selected")
        dec = self.air.plan(snap)
        self.assertEqual((dec.kind, dec.node["label"]), ("click", "检查订单"))
        self.assertTrue(self.air.payment_verified)

    def test_missing_cmb_or_24_months_never_falls_back(self):
        snap = capture("air-cmb-selected")
        term = next(n for n in snap["nodes"] if n["label"].startswith("24 期"))
        term["disabled"] = True
        self.assertEqual(self.air.plan(snap).kind, "manual")
        term["disabled"] = False
        bank = next(n for n in snap["nodes"] if n["label"] == "招商银行")
        bank["disabled"] = True
        self.assertEqual(self.air.plan(snap).kind, "manual")

    def test_changed_bank_and_term_are_corrected_before_review(self):
        snap = capture("air-cmb-selected")
        bank = next(n for n in snap["nodes"] if n["label"] == "招商银行")
        bank["selected"] = False
        self.assertEqual(self.air.plan(snap).node, bank)
        bank["selected"] = True
        term = next(n for n in snap["nodes"] if n["label"].startswith("24 期"))
        term["selected"] = False
        self.assertEqual(self.air.plan(snap).node, term)

    def test_actual_final_button_stops_trial_for_review(self):
        dec = self.air.plan(capture("air-review-cmb24"))
        self.assertEqual((dec.kind, dec.node["label"]), ("review", "立即下单"))

    def test_real_created_order_stops_at_cmb_payment(self):
        snap = capture("air-order-cmb-pending")
        self.assertIn("掌上生活", snap["text"])
        self.assertIn("30 分钟", snap["text"])
        self.assertIn("W0000000000", snap["text"])
        dec = self.air.plan(snap)
        # The order exists (number shown, waiting for 掌上生活 payment): that is a placed order, not a step to act on.
        self.assertEqual(dec.kind, "complete")
        self.assertIn("掌上生活", dec.message)
        self.assertIsNone(dec.node)

    def test_real_expired_session_returns_to_existing_bag(self):
        self.air.added = True
        dec = self.air.plan(capture("air-session-expired"))
        self.assertEqual((dec.kind, dec.node["label"]), ("recover-bag", "返回购物袋"))

    def test_real_masked_summary_requires_prior_verified_contact_and_store(self):
        from duo.model import AIR_TARGET, review_checks
        cfg = Config(last_name="李", first_name="测试")
        # This captured page has Apple's own phone/email masking and redacted name.
        summary = capture("air-review-cmb24")["summary"]
        self.assertFalse(review_checks(summary, cfg, "环球港", target=AIR_TARGET)[0])
        self.assertTrue(review_checks(summary, cfg, "环球港", target=AIR_TARGET,
                                     contact_verified=True, store_verified=True)[0])
        cfg.phone = "18100001234"
        self.assertFalse(review_checks(summary, cfg, "环球港", target=AIR_TARGET,
                                      contact_verified=True, store_verified=True)[0])


if __name__ == "__main__":
    unittest.main()

"""Isolation and mismatch fault injection; these are not live checkout tests."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
from PySide6.QtWidgets import QApplication
from duo.browser import Adapter, Decision
from duo.operator import Operator as Worker
from duo.model import CHINA, Config, mask, review_checks
from test_logic import node, snapshot


class DeliverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(fulfillment="delivery", last_name="李", first_name="测试",
                          phone="18100000000", email="fixture@example.invalid",
                          shipping_address="测试路1号101室")

    def summary(self):
        return ("iPhone Duo 星光白色 1TB 数量：1 AppleCare+ 服务计划\n"
                "送货地址：上海市 嘉定区 测试路1号101室 李测试 18100000000\n"
                "订单总计：RMB 24,098 电子发票 - 个人 招商银行分期 24 期\nfixture@example.invalid")

    def test_each_flow_rejects_the_other_fulfillment(self):
        self.assertTrue(review_checks(self.summary(), self.cfg, "")[0])
        pickup = Config.from_dict(self.cfg.to_dict())
        pickup.fulfillment = "pickup"
        self.assertFalse(review_checks(self.summary(), pickup, "静安", store_verified=True)[0])
        for bad in [self.summary().replace("嘉定区", "静安区"),
                    self.summary().replace("1号101室", "1号102室"),
                    self.summary().replace("送货地址", "取货地点"),
                    self.summary().replace("24 期", "12 期"),
                    self.summary().replace("数量：1", "数量：2")]:
            self.assertFalse(review_checks(bad, self.cfg, "静安", contact_verified=True, store_verified=True)[0])

    def test_real_review_page_layout_passes_and_still_rejects_a_wrong_address(self):
        # Layout of Apple's Review page captured live on 2026-10-14 (personal data replaced by the test config):
        # the province is written "上海" (no 市), contacts are masked by Apple.
        review = ("准备下单了吗？ 请确保以下信息均准确无误。 预计送达日期： 明天 现在订购，预计可于明天送达。 更改送货方式 "
                  "iPhone Duo 1TB 星光白色 数量\xa0 1 RMB 21,499 适用于 iPhone Duo 的 AppleCare+ 服务计划 RMB 2,599 "
                  "送货详情 更改 送货地址： 李\xa0测试\xa0 上海\xa0上海,\xa0嘉定区\xa0 " + self.cfg.shipping_address +
                  "\xa0 中国\xa0 ••••••••0000\xa0 联系信息： f•••••e@example.invalid\xa0 ••••••••0000\xa0 "
                  "发票： 电子发票 - 个人 付款方式： 招商银行 分期付款 24 期 订单总计 RMB 24,098")
        valid, problems, total = review_checks(review, self.cfg, "", contact_verified=True)
        self.assertTrue(valid, problems)
        self.assertEqual(total, 24098.0)
        for bad in (review.replace("嘉定区", "宝山区"),
                    review.replace(self.cfg.shipping_address, "测试路9号909室"),
                    review.replace("上海\xa0上海", "北京\xa0北京")):
            self.assertFalse(review_checks(bad, self.cfg, "", contact_verified=True)[0])

    def _review_page(self, final_disabled):
        go = node("立即下单", "button", disabled=final_disabled)
        go["id"] = "rs-checkout-continue-button-bottom"          # same id as every 继续 / 检查订单 button
        nodes = [node("更改零售店 取货日期待付款完成后确定。", "button"), node("更改付款详情", "button"), go]
        text = ("结账 准备下单了吗？ 请确保以下信息均准确无误。 付款详情 更改 分期付款方案： 24 个月，每月约 RMB 671 "
                "总计 RMB 16,098 立即下单")
        return snapshot(nodes, text=text, url="https://secure10.www.apple.com.cn/shop/checkout?_s=Review")

    def test_review_page_waits_for_place_order_and_never_misreads_the_installment_summary(self):
        # Live 10-15: the review page shows "分期付款方案" without bank radios and a disabled 立即下单;
        # it was misread as "招商银行分期不可用" and a stale 继续 click then landed on 立即下单.
        for cfg in (self.cfg, Config(last_name="李", first_name="测试", phone="18100000000")):
            adapter = Adapter(cfg)
            adapter.delivery_verified = adapter.address_verified = True
            adapter.chosen_store = "环球港"
            dec = adapter.plan(self._review_page(final_disabled=True))
            self.assertEqual(dec.kind, "wait", dec.message)
            dec = adapter.plan(self._review_page(final_disabled=False))
            self.assertEqual((dec.kind, dec.node["label"]), ("review", "立即下单"))

    def test_shipping_address_is_redacted_from_logs(self):
        self.assertNotIn(self.cfg.shipping_address, mask(self.cfg.shipping_summary, self.cfg))

    def test_delivery_cannot_continue_without_verified_address(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        dec = adapter.plan(snapshot([node("继续选择付款方式", "button")]))
        self.assertEqual(dec.kind, "manual")
        self.assertFalse(adapter.chosen_store)

    def test_address_form_correction_and_unknown_required_postcode(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        fields = [node("省份", "select-one", value="31"),
                  node("区县", "select-one", value="310114"),
                  node("详细地址", "text", value="错误地址")]
        fields[0]["options"] = [dict(text="上海市", value="31", disabled=False)]
        fields[1]["options"] = [dict(text="嘉定区", value="310114", disabled=False)]
        snap = snapshot(fields, text="送货地址 上海市 嘉定区")
        dec = adapter.plan(snap)
        self.assertEqual((dec.kind, dec.value), ("fill", self.cfg.shipping_address))
        self.assertFalse(adapter.address_verified)
        fields[-1]["value"] = dec.value
        adapter.plan(snap)
        self.assertTrue(adapter.address_verified)
        postcode = node("邮政编码", "text")
        postcode["required"] = True
        fields.append(postcode)
        # The configured example postcode is filled when Apple asks for it…
        self.assertEqual(adapter.plan(snap).value, "200000")
        # …and an unknown postcode is never guessed.
        unknown = Adapter(Config.from_dict(dict(self.cfg.to_dict(), shipping_postcode="")))
        unknown.delivery_verified = True
        self.assertEqual(unknown.plan(snap).kind, "manual")

    def _overlay_node(self, label, kind="text", value="", suffix="address"):
        n = node(label, kind, value=value)
        n["id"] = "checkout.fulfillment.deliveryAddressOverlay." + suffix
        return n

    def test_delivery_address_overlay_uses_its_own_save_button(self):
        # Live 10-14: "继续填写送货地址" opens a modal overlay; its own save button must be clicked,
        # not the page-level continue button (which sits behind the modal and times out).
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        street = self._overlay_node("详细地址", value=self.cfg.shipping_address, suffix="address")
        save = self._overlay_node("保存", kind="button", suffix="saveButton")
        page_continue = node("继续填写送货地址", "button")
        page_continue["id"] = "rs-checkout-continue-button-bottom"
        snap = snapshot([street, save, page_continue], text="送货地址 上海市 嘉定区 " + self.cfg.shipping_address)
        dec = adapter.delivery(snap)
        self.assertEqual((dec.kind, dec.node["id"]), ("click", "checkout.fulfillment.deliveryAddressOverlay.saveButton"))

    def test_delivery_overlay_without_save_button_pauses_not_clicks_page_continue(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        street = self._overlay_node("详细地址", value=self.cfg.shipping_address, suffix="address")
        cancel = self._overlay_node("取消", kind="button", suffix="cancelButton")
        page_continue = node("继续填写送货地址", "button")
        page_continue["id"] = "rs-checkout-continue-button-bottom"
        snap = snapshot([street, cancel, page_continue], text="送货地址 上海市 嘉定区 " + self.cfg.shipping_address)
        dec = adapter.delivery(snap)
        self.assertEqual(dec.kind, "manual")
        self.assertIn("保存", dec.message)
        # The page-level continue button is never returned while the overlay is open.
        full = adapter.plan(snap)
        self.assertNotEqual(getattr(full.node, "get", lambda _k: None)("id") if full.node else None,
                            "rs-checkout-continue-button-bottom")

    def _real_overlay(self, picker_label="上海 嘉定区"):
        # Field layout captured from Apple's delivery address overlay on 2026-10-14 (ids and labels only).
        picker = self._overlay_node(picker_label, kind="button",
                                    suffix="addressStreets.address.stateCitySelectorForCheckout.provinceCityDistrict")
        picker["name"] = "provinceCityDistrict"
        street = self._overlay_node("详细地址", value=self.cfg.shipping_address, suffix="addressStreets.address.street")
        street["name"] = "street"
        street2 = self._overlay_node("", suffix="addressStreets.address.street2")
        street2["name"] = "street2"
        apply = self._overlay_node("使用", kind="button", suffix="addressStreets.apply")
        cancel, close = node("取消", "button"), node("关闭", "button")
        saved = node("保存的地址", "select-one")
        page_continue = node("继续填写送货地址", "button")
        page_continue["id"] = "rs-checkout-continue-button-bottom"
        # The page writes "上海 嘉定区" without 市 — region matching must tolerate that.
        return snapshot([saved, picker, street, street2, apply, cancel, close, page_continue],
                        text="送货至： 上海 嘉定区 选择免费送货方式 " + self.cfg.shipping_address)

    def test_real_overlay_clicks_shi_yong_and_never_the_page_button(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        dec = adapter.plan(self._real_overlay())
        self.assertEqual((dec.kind, dec.node["label"]), ("click", "使用"))
        self.assertTrue(dec.node["id"].endswith("addressStreets.apply"))
        self.assertTrue(adapter.address_verified)
        # Clicked once already and the overlay is still there: ask the user instead of looping.
        adapter.last_clicked = dec.node["selector"]
        self.assertEqual(adapter.plan(self._real_overlay()).kind, "manual")

    def _fulfillment(self, selected_label, text_extra=""):
        tab = node("为我送货", selected=True)
        slots = []
        later = (datetime.now(CHINA) + timedelta(days=3)).strftime("%Y/%m/%d")
        for label in ("预计送达日期： 明天 白天 免费", "预计送达日期： 今天送达 免费", f"预计送达日期： 周五 {later} 09:00 - 11:00 免费"):
            s = node(label, selected=(label == selected_label))
            s["name"] = "deliveryOptions-checkout.fulfillment.deliveryTab.delivery.shipmentGroups.shipmentGroup-1.shippingOptions"
            slots.append(s)
        go = node("继续填写送货地址", "button")
        go["id"] = "rs-checkout-continue-button-bottom"
        return snapshot([tab] + slots + [go], text="你希望如何收到订单商品？ 送货至： 上海 嘉定区 " + text_extra)

    def test_option_refused_on_continue_moves_to_next_earliest(self):
        adapter = Adapter(self.cfg)
        today = "预计送达日期： 今天送达 免费"
        standing = "请选择其他送货方式以继续结账。"
        # The standing prompt alone never rejects: keep the earliest option and continue.
        dec = adapter.plan(self._fulfillment(today, standing))
        self.assertEqual(dec.node["label"], "继续填写送货地址")
        self.assertFalse(adapter.rejected_slots)
        # A planned-but-not-performed 继续 (held back while Apple was busy) proves nothing.
        dec = adapter.plan(self._fulfillment(today, standing))
        self.assertEqual(dec.node["label"], "继续填写送货地址")
        self.assertFalse(adapter.rejected_slots)
        adapter.performed(dec)                      # the operator actually clicked it
        # While Apple is still processing that 继续, the page is not judged at all.
        busy = self._fulfillment(today, standing)
        busy["checkout_busy"] = True
        self.assertEqual(adapter.plan(busy).kind, "wait")
        self.assertFalse(adapter.rejected_slots)
        # Apple finished and kept us here with today still selected: today is refused, take the next earliest.
        dec = adapter.plan(self._fulfillment(today, standing))
        self.assertEqual((dec.kind, dec.node["label"]), ("select", "预计送达日期： 明天 白天 免费"))
        self.assertIn("今天送达", dec.message)
        # The same option written differently ("最快今天送达") counts as refused too.
        self.assertIn(Adapter.slot_identity(node("最快今天送达 免费")), adapter.rejected_slots)
        self.assertNotIn(Adapter.slot_identity(node("预计送达日期： 明天 白天 免费")), adapter.rejected_slots)
        self.assertEqual(adapter.plan(self._fulfillment("预计送达日期： 明天 白天 免费")).node["label"], "继续填写送货地址")

    def test_after_address_confirmed_the_selected_option_is_kept_not_chased(self):
        adapter = Adapter(self.cfg)
        tomorrow = "预计送达日期： 明天 白天 免费"
        # Before the address: always the earliest.
        dec = adapter.plan(self._fulfillment(tomorrow))
        self.assertEqual((dec.kind, dec.node["label"]), ("select", "预计送达日期： 今天送达 免费"))
        # Address confirmed (overlay "使用" performed and closed); Apple now has 明天 selected: keep it, continue.
        adapter.apply_selector = "#apply"
        dec = adapter.plan(self._fulfillment(tomorrow))
        self.assertEqual(dec.node["label"], "继续填写送货地址")
        self.assertEqual(adapter.current_slot, Adapter.slot_identity(node(tomorrow)))

    def test_continue_that_opens_the_address_overlay_is_not_a_refusal(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        today = "预计送达日期： 今天送达 免费"
        dec = adapter.plan(self._fulfillment(today))
        self.assertEqual(dec.node["label"], "继续填写送货地址")
        adapter.performed(dec)
        self.assertIsNotNone(adapter.continued_with)
        apply = adapter.plan(self._real_overlay())  # 继续 opened the overlay
        self.assertIsNone(adapter.continued_with)
        adapter.performed(apply)
        self.assertTrue(adapter.apply_selector)
        dec = adapter.plan(self._fulfillment(today))  # back after 使用: still today, continue again
        self.assertTrue(adapter.address_applied)
        self.assertEqual(dec.node["label"], "继续填写送货地址")
        self.assertFalse(adapter.rejected_slots)

    def _shipping_page(self, picker_value="上海 嘉定区", street=""):
        # Field layout captured from Apple's Shipping-init page on 2026-10-14 (ids and labels only).
        def field(label, kind, suffix, value=""):
            n = node(label, kind, value=value)
            n["id"] = "checkout.shipping." + suffix
            n["name"] = suffix.rsplit(".", 1)[-1]
            n["required"] = True
            return n
        picker = field("收货地址：", "text", "addressSelector.newAddress.address.stateCitySelectorForCheckout.provinceCityDistrict", picker_value)
        picker["required"] = False
        nodes = [field("姓氏", "text", "addressSelector.newAddress.address.lastName"),
                 field("名字", "text", "addressSelector.newAddress.address.firstName"),
                 picker,
                 field("详细地址", "text", "addressSelector.newAddress.address.street", street),
                 field("收件人手机号码", "tel", "addressSelector.newAddress.address.copyContact.fullDaytimePhone"),
                 field("电子邮件地址", "email", "addressContactEmail.address.emailAddress"),
                 field("联系人手机号码", "tel", "addressContactPhone.address.mobilePhone"),
                 node("电子发票 - 个人", "radio"), node("继续选择付款方式", "button")]
        street2 = field("附加详细地址", "text", "addressSelector.newAddress.address.street2")
        street2["required"] = False
        nodes.insert(4, street2)
        return snapshot(nodes, text="你的送货地址是哪里？ 输入收件人姓名和地址： 收货地址： 详细地址 联系人手机号码 验证",
                        url="https://secure10.www.apple.com.cn/shop/checkout?_s=Shipping-init")

    def test_shipping_page_verifies_the_region_picker_and_fills_the_street(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        dec = adapter.plan(self._shipping_page())
        self.assertEqual(dec.kind, "fill")
        self.assertEqual(dec.node["name"], "street", "the picker showing 上海 嘉定区 is verified, not typed into")
        self.assertEqual(dec.value, self.cfg.shipping_address)
        dec = adapter.plan(self._shipping_page(street=self.cfg.shipping_address))
        self.assertTrue(adapter.address_verified)
        self.assertEqual((dec.kind, dec.node["name"]), ("fill", "lastName"))

    def test_after_the_sms_code_the_lingering_verify_button_is_waited_out(self):
        import time as _time
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        page = self._shipping_page(street=self.cfg.shipping_address)
        for n in page["nodes"]:
            if n["kind"] in ("text", "tel", "email") and not n["value"] and n["required"]:
                n["value"] = {"lastName": self.cfg.last_name, "firstName": self.cfg.first_name,
                              "emailAddress": self.cfg.email}.get(n["name"], self.cfg.phone)
        page["nodes"][-2]["selected"] = True                      # 电子发票 - 个人
        verify = node("验证", "button")
        page["nodes"].append(verify)
        page["text"] += " 手机"
        self.assertEqual(adapter.plan(page).kind, "send-code")
        adapter.sms_requested = True
        adapter.otp_submitted_at = _time.monotonic()              # the code was just typed
        self.assertEqual(adapter.plan(page).kind, "wait", "Apple has not re-rendered yet")
        adapter.otp_submitted_at = _time.monotonic() - 20
        self.assertEqual(adapter.plan(page).kind, "manual", "still there long after: ask, never resend")
        page["nodes"].remove(verify)                              # Apple accepted the code
        dec = adapter.plan(page)
        self.assertTrue(adapter.phone_confirmed)
        self.assertEqual(dec.node["label"], "继续选择付款方式")

    def test_shipping_page_with_wrong_or_missing_region_pauses(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        self.assertIn("浦东新区", adapter.plan(self._shipping_page("上海 浦东新区")).message)
        self.assertFalse(adapter.address_verified)
        self.assertEqual(Adapter(self.cfg).delivery(self._shipping_page("")).kind, "manual")

    def test_real_overlay_with_wrong_district_is_not_confirmed(self):
        adapter = Adapter(self.cfg)
        adapter.delivery_verified = True
        dec = adapter.plan(self._real_overlay("上海 浦东新区"))
        self.assertEqual(dec.kind, "manual")
        self.assertIn("浦东新区", dec.message)
        self.assertFalse(adapter.address_verified)

    def test_date_ranking_uses_calendar_not_dom_order(self):
        later = node("预计送达日期：2026/11/01 免费")
        early = node("预计送达日期：2026/10/23 免费")
        self.assertLess(Adapter.shipping_date_key(early), Adapter.shipping_date_key(later))
        today, tomorrow = node("预计送达日期： 今天送达 免费"), node("预计送达日期： 明天 白天 免费")
        self.assertLess(Adapter.shipping_date_key(today), Adapter.shipping_date_key(tomorrow))
        self.assertLess(Adapter.shipping_date_key(tomorrow), Adapter.shipping_date_key(node("预计送达日期： 后天 免费")))

    def test_earliest_free_shipping_slot_is_chosen_from_real_option_names(self):
        # Live 10-14 fulfillment page: a standing "请选择其他送货方式以继续结账" prompt is not a rejection.
        adapter = Adapter(self.cfg)
        tab = node("为我送货", selected=True)
        slots = []
        for label in ("预计送达日期： 明天 白天 免费", "预计送达日期： 今天送达 免费", "预计送达日期： 明天 09:00 - 11:00 免费"):
            s = node(label)
            s["name"] = "deliveryOptions-checkout.fulfillment.deliveryTab.delivery.shipmentGroups.shipmentGroup-1.shippingOptions"
            slots.append(s)
        go = node("继续填写送货地址", "button")
        snap = snapshot([tab] + slots + [go], text="你希望如何收到订单商品？请选择其他送货方式以继续结账。")
        dec = adapter.plan(snap)
        self.assertEqual((dec.kind, dec.node["label"]), ("select", "预计送达日期： 今天送达 免费"))
        slots[1]["selected"] = True
        self.assertEqual(adapter.plan(snap).node["label"], "继续填写送货地址")


class DualWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pickup = Worker(root, "pickup")
        self.delivery = Worker(root / "delivery", "delivery")

    async def test_stop_submit_journal_and_pause_are_local_to_one_worker(self):
        for worker in (self.pickup, self.delivery):
            worker.active = True
            worker.adapter = Adapter(worker.cfg)
        self.pickup.checkpoint("submit-attempt")
        self.assertEqual(self.delivery.read_journal(), {})
        self.assertNotEqual(self.pickup.journal_path, self.delivery.journal_path)
        self.pickup.attention("等待验证码")
        self.assertTrue(self.pickup.paused)
        self.assertFalse(self.delivery.paused)
        await self.pickup.handle("stop", None)
        self.assertTrue(self.delivery.active)
        self.delivery.checkpoint("add-attempt")
        self.assertEqual(self.pickup.read_journal()["phase"], "submit-attempt")
        self.assertEqual(self.delivery.read_journal()["fulfillment"], "delivery")

    async def test_mode_cannot_be_changed_and_settings_are_copied(self):
        with self.assertRaises(ValueError):
            await self.pickup.handle("now", Config(fulfillment="delivery"))
        cfg = Config()
        with patch.object(self.pickup, "ensure_browser"), patch.object(self.pickup, "prepare_race"):
            await self.pickup.handle("now", cfg)
        cfg.stores.clear()
        cfg.phone = "18100009999"
        self.assertTrue(self.pickup.cfg.stores)
        self.assertNotEqual(self.pickup.cfg.phone, cfg.phone)

    async def test_otp_cannot_cross_flow_or_reuse_stale_request(self):
        self.pickup.otp_request, self.delivery.otp_request = "pickup-request", "delivery-request"
        with patch.object(self.pickup, "submit_otp") as p, patch.object(self.delivery, "submit_otp") as d:
            await self.delivery.handle("otp", {"request": "pickup-request", "code": "000000"})
            await self.pickup.handle("otp", {"request": "stale", "code": "000000"})
            p.assert_not_awaited()
            d.assert_not_awaited()
            await self.delivery.handle("otp", {"request": "delivery-request", "code": "000000"})
            d.assert_awaited_once_with("000000")

    async def test_masked_review_submit_preserves_proof_and_submits_only_once(self):
        worker = self.pickup
        worker.active = True
        worker.cfg = Config(last_name="李", first_name="测试")
        worker.adapter = Adapter(worker.cfg)
        worker.adapter.chosen_store = "环球港"
        worker.adapter.contact_verified = worker.adapter.store_verified = True
        fixture = Path(__file__).parent / "fixtures/apple-live-2026-10-05/air-review-cmb24.json"
        snap = json.loads(fixture.read_text(encoding="utf-8"))
        # Explicit mismatch injection: adapt the real masked summary's product
        # to exercise Duo submit, without claiming this is a captured Duo order.
        snap["summary"] = snap["summary"].replace("iPhone Air", "iPhone Duo").replace("云白色", "星光白色").replace("16,098", "24,098")
        snap["nodes"] = []
        dec = Decision("review", "提交")
        with patch("duo.operator.perform") as perform:
            await worker.review(snap, dec)
            self.assertTrue(worker.last_result["valid"])
            await worker.handle("submit", None)
            self.assertTrue(worker.adapter.contact_verified)
            await worker.review(snap, dec)
            await worker.review(snap, dec)
        perform.assert_awaited_once()
        self.assertEqual(worker.read_journal()["phase"], "submit-attempt")
        self.assertFalse(self.delivery.read_journal())

    async def test_changed_review_cannot_use_old_submit_confirmation(self):
        worker = self.pickup
        worker.active = worker.paused = True
        worker.adapter = Adapter(worker.cfg)
        worker.last_result = {"valid": True}
        worker.review_token = Worker.review_token_of({"url": "review", "summary": "old"})
        await worker.handle("submit", None)
        with patch("duo.operator.perform") as perform:
            await worker.review({"url": "review", "summary": "changed", "nodes": []}, Decision("review", "提交"))
        perform.assert_not_awaited()
        self.assertFalse(worker.submit_once)
        self.assertFalse(worker.read_journal())


class ThreadAndPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_worker_threads_continue_while_one_is_blocked(self):
        """Inject a hung browser preparation into the real QThread command loop."""
        with tempfile.TemporaryDirectory() as tmp:
            pickup = Worker(Path(tmp), "pickup")
            delivery = Worker(Path(tmp) / "delivery", "delivery")
            entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
            pickup_handle, delivery_handle = pickup.handle, delivery.handle
            async def slow(command, payload):
                if command == "blocked-network":
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.005)
                else:
                    await pickup_handle(command, payload)
            async def quick(command, payload):
                if command == "probe":
                    progressed.set()
                else:
                    await delivery_handle(command, payload)
            pickup.handle, delivery.handle = slow, quick
            pickup.start()
            delivery.start()
            try:
                pickup.send("blocked-network")
                self.assertTrue(entered.wait(2))
                delivery.send("probe")
                self.assertTrue(progressed.wait(2), "delivery was blocked by pickup")
                self.assertFalse(release.is_set())
            finally:
                release.set()
                pickup.send("shutdown")
                delivery.send("shutdown")
                self.assertTrue(pickup.wait(3000))
                self.assertTrue(delivery.wait(3000))

    def test_dispatcher_keeps_operators_profiles_and_otp_routing_separate(self):
        from duo.dispatcher import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher = Dispatcher(Path(tmp), testing=True, start_threads=False, health_interval_ms=0)
            try:
                p, d = dispatcher.ops["pickup"], dispatcher.ops["delivery"]
                self.assertIsNot(p, d)
                self.assertNotEqual(p.session_dir / "chromium-profile", d.session_dir / "chromium-profile")
                self.assertEqual(dispatcher.config("pickup").fulfillment, "pickup")
                self.assertEqual(dispatcher.config("delivery").fulfillment, "delivery")
                self.assertIs(p.coordinator, d.coordinator)
                with patch.object(p, "send") as ps, patch.object(d, "send") as ds:
                    dispatcher.command("pickup", "otp", {"request": "p", "code": "123456"})
                    ds.assert_not_called()
                    ps.assert_called_once_with("otp", {"request": "p", "code": "123456"})
                    dispatcher.command("delivery", "otp", {"request": "d", "code": ""})
                    ds.assert_called_once_with("otp", {"request": "d", "code": ""})
                with self.assertRaises(ValueError):
                    dispatcher.command("warehouse", "otp", {})
            finally:
                dispatcher.shutdown()


if __name__ == "__main__":
    unittest.main()

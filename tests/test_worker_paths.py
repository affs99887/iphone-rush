"""Fault injection for the real worker. No HTTP server or synthetic shop."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
from PySide6.QtCore import QCoreApplication
from duo.browser import Adapter, Decision
from duo.operator import Operator as Worker
from duo.model import AIR_TARGET, AIR_TEST_URL, CHINA, Config, parse_retry_after
from duo.race import EntryRace

FIXTURES = Path(__file__).parent / "fixtures" / "apple-live-2026-10-05"


def capture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


class WorkerPathTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worker = Worker(Path(self.temp.name))
        self.worker.cfg = Config(last_name="李", first_name="测试", phone="18100000000")
        self.worker.adapter = Adapter(self.worker.cfg)
        self.worker.page = MagicMock()
        self.worker.page.is_closed.return_value = False
        self.worker.page.url = "https://secure8.www.apple.com.cn/shop/checkout"
        self.worker.page.evaluate = AsyncMock()
        self.worker.page.bring_to_front = AsyncMock()
        self.worker.target = datetime.now(CHINA) - timedelta(seconds=10)
        self.worker.deadline = datetime.now(CHINA) + timedelta(minutes=10)
        # The page is already open and Apple's shield handshake has completed: these tests
        # exercise what happens on the page, not the entry race or the handshake wait.
        self.worker.entry_started = True
        self.worker.shield_for()["proven"] = True

    def summary(self):
        return "iPhone Duo 星光白色 1TB 数量：1 AppleCare+ 服务计划\n不折抵换购 取货：上海 环球港 李测试 18100000000 订单总计：RMB 23,898\n电子发票 - 个人 招商银行分期 24 期\n" + self.worker.cfg.email

    async def test_review_ticks_terms_automatically_then_reads_the_page_again(self):
        # User decision 10-15: accept Apple's terms automatically. Tick, never submit in the same pass.
        self.worker.adapter.chosen_store = "环球港"
        page = {"summary": self.summary(), "nodes": [{"kind": "checkbox", "selected": False, "label": "同意购买协议", "selector": "#t"}]}
        with patch("duo.operator.perform") as perform:
            await self.worker.review(page, Decision("review", "核对"))
            await self.worker.review(page, Decision("review", "核对"))
            self.assertEqual([c.args[1].kind for c in perform.await_args_list], ["select", "select"])
            self.assertFalse(self.worker.submitted)
            # Still unticked after two tries: ask instead of looping or submitting.
            await self.worker.review(page, Decision("review", "核对"))
        self.assertEqual(perform.await_count, 2)
        self.assertTrue(self.worker.paused)
        self.assertFalse(self.worker.submitted)
        self.assertIn("条款", self.worker.last_message)
        # With automatic terms switched off the user is asked straight away.
        self.worker.cfg.auto_consent, self.worker.terms_attempts, self.worker.paused = False, 0, False
        with patch("duo.operator.perform") as perform:
            await self.worker.review(page, Decision("review", "核对"))
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)

    async def test_checkout_maintenance_never_discards_cart(self):
        self.worker.page.evaluate.return_value = {"url": self.worker.page.url, "text": "我们很快就会回来", "nodes": []}
        self.worker.adapter.added = True
        with patch.object(self.worker, "prepare_race") as race:
            await self.worker.tick()
        race.assert_not_called()
        self.assertTrue(self.worker.paused)
        self.assertIn("已保留", self.worker.last_message)

    async def test_not_found_after_add_checks_existing_bag_once(self):
        # Fault injection uses the error text observed in the packaged Edge run.
        self.worker.page.evaluate.return_value = {
            "url": AIR_TEST_URL, "title": "Page Not Found - Apple",
            "text": "The page you’re looking for can’t be found.", "nodes": []
        }
        self.worker.adapter.added = True
        events = []
        self.worker.event.connect(lambda value: events.append(value["type"]))
        self.worker.pending = (Decision("add", "添加", {"selector": "#add"}),
                               AIR_TEST_URL, "previous", time.monotonic())
        with patch.object(self.worker, "navigate") as navigate, patch("duo.operator.perform") as perform:
            await self.worker.tick()
            await self.worker.tick()
        navigate.assert_awaited_once_with("https://www.apple.com.cn/shop/bag")
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)
        self.assertNotIn("operator.timing", events)

    async def test_not_found_after_submit_never_restarts_checkout(self):
        self.worker.page.evaluate.return_value = {
            "url": "https://www.apple.com.cn/shop/404",
            "text": "Page Not Found", "nodes": []
        }
        self.worker.adapter.added = self.worker.submitted = True
        with patch.object(self.worker, "navigate") as navigate, patch("duo.operator.perform") as perform:
            await self.worker.tick()
        navigate.assert_not_called()
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)

    async def test_trial_routes_to_official_air_with_multiple_entry_pages(self):
        self.worker.submit_once = True
        with patch.object(self.worker, "ensure_browser"), patch.object(self.worker, "prepare_race") as race, patch.object(self.worker, "awake"):
            await self.worker.handle("trial", Config(auto_submit=True))
        self.assertTrue(self.worker.trial)
        self.assertFalse(self.worker.submit_once)
        self.assertEqual(self.worker.entry_url, AIR_TEST_URL)
        self.assertEqual(self.worker.adapter.target, AIR_TARGET)
        race.assert_awaited_once()

    async def test_trial_follows_the_auto_submit_switch_and_never_submits_an_invalid_order(self):
        # Switch off: a valid trial review stops and waits for the user's click.
        self.worker.trial = self.worker.active = True
        self.worker.cfg.auto_submit = False
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.chosen_store = "环球港"
        air = self.summary().replace("iPhone Duo", "iPhone Air").replace("星光白色", "云白色").replace("23,898", "16,098")
        with patch("duo.operator.perform") as perform:
            await self.worker.review({"summary": air, "nodes": []}, Decision("review", "立即下单"))
        perform.assert_not_called()
        self.assertTrue(self.worker.active and self.worker.paused)
        self.assertFalse(self.worker.submitted)
        self.assertTrue(self.worker.last_result["valid"])
        # Even the user's one-time permission cannot submit an order that failed the review.
        bad = {"summary": air.replace("1TB", "256GB"), "nodes": []}
        self.worker.submit_once, self.worker.submit_token = True, Worker.review_token_of(bad)
        with patch("duo.operator.perform") as perform:
            await self.worker.review(bad, Decision("review", "立即下单"))
        perform.assert_not_called()
        self.assertFalse(self.worker.submitted)
        self.assertFalse(self.worker.last_result["valid"])
        # …and a review page that changed after the user's click is refused too.
        self.worker.submit_once, self.worker.submit_token = True, "an-older-review"
        with patch("duo.operator.perform") as perform:
            await self.worker.review({"summary": air, "nodes": []}, Decision("review", "立即下单"))
        perform.assert_not_called()
        self.assertFalse(self.worker.submitted)
        # Switch on (default since 10-15): an invalid trial review still never submits…
        self.worker.cfg.auto_submit, self.worker.submit_once, self.worker.paused = True, False, False
        with patch("duo.operator.perform") as perform:
            await self.worker.review(bad, Decision("review", "立即下单"))
        perform.assert_not_called()
        self.assertFalse(self.worker.submitted)

    async def test_trial_with_auto_submit_submits_a_valid_review_once_by_itself(self):
        from duo.orders import OrderCoordinator
        gate = OrderCoordinator(Path(self.temp.name))
        self.worker.coordinator = gate
        self.worker.trial = self.worker.active = True
        self.assertTrue(self.worker.cfg.auto_submit, "on by default")
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.chosen_store = "环球港"
        air = self.summary().replace("iPhone Duo", "iPhone Air").replace("星光白色", "云白色").replace("23,898", "16,098")
        review = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Review", "summary": air, "nodes": []}
        with patch("duo.operator.perform") as perform:
            await self.worker.review(review, Decision("review", "立即下单"))
            self.assertIn("自动提交测试订单", self.worker.last_message)
            await self.worker.review(review, Decision("review", "立即下单"))     # a later pass never resubmits
        perform.assert_awaited_once()
        self.assertTrue(self.worker.submitted)
        self.assertIn("trial-headless", str(self.worker.journal_path))
        self.assertEqual(gate.status()["attempts"], {}, "a test order never claims the real submit right")

    async def test_user_confirmed_trial_order_submits_once_without_touching_the_formal_record(self):
        from duo.orders import OrderCoordinator
        gate = OrderCoordinator(Path(self.temp.name))
        self.worker.coordinator = gate
        self.worker.trial = self.worker.active = True
        self.worker.cfg.auto_submit = False
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.chosen_store = "环球港"
        air = self.summary().replace("iPhone Duo", "iPhone Air").replace("星光白色", "云白色").replace("23,898", "16,098")
        review = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Review", "summary": air, "nodes": []}
        with patch("duo.operator.perform") as perform:
            await self.worker.review(review, Decision("review", "立即下单"))
            perform.assert_not_called()
            await self.worker.handle("submit", None)          # the user clicked 提交已核对订单
            await self.worker.review(review, Decision("review", "立即下单"))
            await self.worker.review(review, Decision("review", "立即下单"))
        perform.assert_awaited_once()
        self.assertTrue(self.worker.submitted)
        self.assertEqual(self.worker.read_journal()["phase"], "submit-attempt")
        self.assertIn("trial-headless", str(self.worker.journal_path))
        self.assertFalse((Path(self.temp.name) / "purchase-state.json").exists(), "formal journal untouched")
        self.assertEqual(gate.status()["attempts"], {}, "a test order never claims the real submit right")

    async def test_legacy_trial_record_in_the_formal_folder_does_not_block_the_formal_run(self):
        # Found in release\data on 10-15: 1.x trials wrote add-attempts into the formal folder.
        formal = Path(self.temp.name) / "purchase-state.json"
        formal.write_text('{"phase": "add-attempt", "trial": true, "time": "2026-10-05T18:46:15+08:00"}', encoding="utf-8")
        with patch.object(self.worker, "ensure_browser"), patch.object(self.worker, "prepare_race"), \
                patch.object(self.worker, "awake"):
            await self.worker.handle("arm", Config())
        self.assertTrue(self.worker.active, "the formal run is armed")
        self.assertFalse(formal.exists())
        archived = list(Path(self.temp.name).glob("purchase-state.legacy-trial-*.json"))
        self.assertEqual(len(archived), 1, "the old record is kept, only renamed")
        # A submitted record still blocks, even if it claims to be a trial.
        blocked = Worker(Path(self.temp.name) / "other")
        blocked.data_dir.mkdir(parents=True, exist_ok=True)
        blocked.journal_path.write_text('{"phase": "submit-attempt", "trial": true}', encoding="utf-8")
        with patch.object(blocked, "ensure_browser"), patch.object(blocked, "prepare_race"), patch.object(blocked, "awake"):
            await blocked.handle("arm", Config())
        self.assertFalse(blocked.active)
        self.assertTrue(blocked.journal_path.exists())
        # A real (non-trial) add-attempt still blocks as before.
        real = Worker(Path(self.temp.name) / "real")
        real.data_dir.mkdir(parents=True, exist_ok=True)
        real.journal_path.write_text('{"phase": "add-attempt", "trial": false}', encoding="utf-8")
        with patch.object(real, "ensure_browser"), patch.object(real, "prepare_race"), patch.object(real, "awake"):
            await real.handle("arm", Config())
        self.assertFalse(real.active)

    async def test_new_trial_clears_the_previous_trial_bag_but_never_the_formal_record(self):
        self.worker.trial = True
        self.worker.checkpoint("add-attempt")                   # left by an earlier trial
        formal = Path(self.temp.name) / "purchase-state.json"
        formal.write_text('{"phase": "submit-attempt", "trial": false, "fulfillment": "pickup"}', encoding="utf-8")
        cleared = AsyncMock(return_value=True)
        with patch.object(self.worker, "ensure_browser"), patch.object(self.worker, "clear_trial_bag", cleared), \
                patch.object(self.worker, "prepare_race"), patch.object(self.worker, "awake"):
            await self.worker.handle("trial", Config())
        cleared.assert_awaited_once()
        self.assertTrue(self.worker.active, "the trial starts after the test bag was cleared")
        self.assertIn("submit-attempt", formal.read_text(encoding="utf-8"))
        # If the test bag cannot be emptied the trial does not start.
        stuck = Worker(Path(self.temp.name))
        stuck.trial = True
        stuck.checkpoint("add-attempt")
        with patch.object(stuck, "ensure_browser"), patch.object(stuck, "clear_trial_bag", AsyncMock(return_value=False)), \
                patch.object(stuck, "prepare_race"), patch.object(stuck, "awake"):
            await stuck.handle("trial", Config())
        self.assertFalse(stuck.active)

    async def test_trial_review_requires_real_configuration_and_fulfillment(self):
        self.worker.trial = True
        self.worker.cfg.auto_submit = False
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.chosen_store = "环球港"
        text = self.summary().replace("iPhone Duo", "iPhone Air").replace("星光白色", "云白色").replace("23,898", "16,098")
        with patch("duo.operator.perform") as perform:
            await self.worker.review({"summary": text, "nodes": []}, Decision("review", "立即下单"))
            self.assertTrue(self.worker.last_result["valid"])
            for bad in (text.replace("1TB", "256GB"), text.replace("24 期", "12 期"), text.replace("数量：1", "数量：2")):
                await self.worker.review({"summary": bad, "nodes": []}, Decision("review", "立即下单"))
                self.assertFalse(self.worker.last_result["valid"])
        perform.assert_not_called()

    async def test_headless_trial_preserves_prior_production_order_record(self):
        self.worker.checkpoint("submit-attempt")
        production = self.worker.journal_path
        saved = production.read_bytes()
        with patch.object(self.worker, "ensure_browser"), patch.object(self.worker, "prepare_race"), patch.object(self.worker, "awake"):
            await self.worker.handle("trial", Config())
        self.assertTrue(self.worker.trial)
        self.assertNotEqual(production, self.worker.journal_path)
        self.worker.checkpoint("add-attempt")
        self.assertEqual(production.read_bytes(), saved)

    async def test_website_rejection_stops_before_any_further_click(self):
        # A 541 on a protected fetch is the shield race (§8.0): no click, no permanent pause —
        # the operator schedules a jittered re-challenge in the same context instead.
        self.worker.network_failure = {"status": 541, "path": "/shop/fulfillment-messages", "document": False}
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_not_called()
        self.assertFalse(self.worker.paused)
        self.assertEqual(self.worker.after_cooldown, "rechallenge")
        self.assertGreater(self.worker.cooldown_until, time.monotonic())
        self.assertIn("HTTP 541", self.worker.last_message)
        # While cooling down nothing is clicked either.
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_not_called()

    async def test_document_block_cools_down_all_entries_then_asks_after_four(self):
        # A blocked document (403/541 on navigation) is an entry-level cooldown, not a shield race.
        self.worker.network_failure = {"status": 403, "path": "/shop/buy-iphone/iphone-air", "document": True}
        with patch.object(self.worker, "prepare_race") as race:
            await self.worker.tick()
            self.assertEqual(self.worker.after_cooldown, "entry")
            self.assertFalse(self.worker.paused)
            self.worker.cooldown_until = time.monotonic() - 1
            await self.worker.tick()
        race.assert_awaited_once()
        self.worker.consecutive_blocks = 3
        self.worker.network_failure = {"status": 403, "path": "/shop/buy-iphone/iphone-air", "document": True}
        await self.worker.tick()
        self.assertTrue(self.worker.paused)
        self.assertIn("连续 4 次", self.worker.last_message)

    async def test_block_after_add_reads_bag_back_instead_of_re_adding(self):
        self.worker.adapter.added = True
        self.worker.network_failure = {"status": 541, "path": "/shop/bag/status", "document": False}
        with patch.object(self.worker, "navigate") as navigate, patch("duo.operator.perform") as perform:
            await self.worker.tick()
            self.assertEqual(self.worker.after_cooldown, "verify-bag")
            self.worker.cooldown_until = time.monotonic() - 1
            await self.worker.tick()
        navigate.assert_awaited_once_with("https://www.apple.com.cn/shop/bag")
        perform.assert_not_called()
        self.assertTrue(self.worker.adapter.added)

    async def test_block_after_submit_never_touches_the_page_again(self):
        self.worker.submitted = True
        self.worker.network_failure = {"status": 541, "path": "/shop/checkout", "document": True}
        with patch.object(self.worker, "navigate") as navigate, patch("duo.operator.perform") as perform:
            await self.worker.tick()
        navigate.assert_not_called()
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)
        self.assertIn("订单列表", self.worker.last_message)

    async def test_shield_rechallenge_is_bounded_then_asks_user(self):
        self.worker.shield_attempts = 6
        self.worker.network_failure = {"status": 541, "path": "/shop/fulfillment-messages", "document": False}
        await self.worker.tick()
        self.assertTrue(self.worker.paused)
        self.assertIn("6 次", self.worker.last_message)
        self.assertEqual(self.worker.after_cooldown, "")

    async def test_empty_bag_after_journaled_add_allows_exactly_one_more_add(self):
        bag = {"url": "https://www.apple.com.cn/shop/bag", "text": "你的购物袋中没有商品。", "nodes": [], "summary": ""}
        self.worker.page.url = bag["url"]
        self.worker.page.evaluate.return_value = bag
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.added = True
        self.worker.add_attempts = 1
        with patch.object(self.worker, "prepare_race") as race:
            await self.worker.tick()
            race.assert_awaited_once()
            self.assertFalse(self.worker.adapter.added)
            self.assertFalse(self.worker.paused)
            # Second empty bag after the second add: stop and ask.
            self.worker.adapter.added = True
            self.worker.add_attempts = 2
            await self.worker.tick()
            race.assert_awaited_once()
        self.assertTrue(self.worker.paused)
        self.assertIn("上限 2 次", self.worker.last_message)

    async def test_perform_refuses_to_press_place_order_unless_it_is_the_submit(self):
        from duo.browser import FinalButtonGuard, perform as real_perform
        page = MagicMock()
        loc = MagicMock()
        page.locator.return_value = loc
        loc.first.evaluate = AsyncMock(return_value="立即下单")
        scoped = MagicMock()
        scoped.click = AsyncMock()
        loc.filter.return_value = scoped
        loc.click = AsyncMock()
        node = {"selector": "#rs-checkout-continue-button-bottom", "kind": "button", "label": "检查订单", "id": "", "name": ""}
        with self.assertRaises(FinalButtonGuard):
            await real_perform(page, Decision("click", "继续结账流程。", node))
        loc.click.assert_not_called()
        scoped.click.assert_not_called()
        # Still 检查订单: clicked through a locator scoped so a later swap to 立即下单 cannot match.
        loc.first.evaluate = AsyncMock(return_value="检查订单")
        await real_perform(page, Decision("click", "继续结账流程。", node))
        scoped.click.assert_awaited_once()
        self.assertIn("has_not_text", loc.filter.call_args.kwargs)
        # The submit itself (review decision) presses the real button.
        loc.first.evaluate = AsyncMock(return_value="立即下单")
        await real_perform(page, Decision("review", "提交", dict(node, label="立即下单")))
        loc.click.assert_awaited_once()

    async def test_any_planned_click_on_place_order_is_routed_through_review(self):
        go = {"selector": "#go", "kind": "button", "label": "立即下单", "selected": False, "disabled": False,
              "value": "", "name": "", "id": "rs-checkout-continue-button-bottom", "required": False, "options": [], "context": ""}
        page = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Review", "title": "", "text": "准备下单了吗？",
                "nodes": [go], "summary": ""}
        self.worker.page.url = page["url"]
        self.worker.page.evaluate.return_value = page
        with patch.object(self.worker.adapter, "plan", return_value=Decision("click", "继续结账流程。", go)), \
                patch.object(self.worker, "review", AsyncMock()) as review, patch("duo.operator.perform") as perform:
            await self.worker.tick()
        review.assert_awaited_once()
        perform.assert_not_called()

    async def test_checkout_click_waits_for_apples_in_flight_request(self):
        go = {"selector": "#go", "kind": "button", "label": "继续选择付款方式", "selected": False, "disabled": False,
              "value": "", "name": "", "id": "rs-checkout-continue-button-bottom", "required": False, "options": [], "context": ""}
        page = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Shipping-init", "title": "", "text": "继续",
                "nodes": [go], "summary": ""}
        self.worker.page.url = page["url"]
        self.worker.page.evaluate.return_value = page
        request = MagicMock(url="https://secure10.www.apple.com.cn/shop/checkoutx/fulfillment", method="POST")
        self.worker.on_request(request)
        with patch.object(self.worker.adapter, "plan", return_value=Decision("click", "继续结账流程。", go)), \
                patch("duo.operator.perform") as perform:
            await self.worker.tick()
            perform.assert_not_called()
            self.assertEqual(self.worker.last_state, "等待官网处理")
            self.worker.on_request_done(request)
            self.worker.last_checkout_activity = time.monotonic() - 1
            await self.worker.tick()
        perform.assert_awaited_once()
        # A request that never finishes cannot block the run forever.
        self.worker.on_request(MagicMock(url="https://secure10.www.apple.com.cn/shop/checkoutx/x", method="POST"))
        self.worker.quiet_wait_started = time.monotonic() - 26
        self.assertTrue(self.worker.checkout_quiet())
        self.assertFalse(Worker.is_checkout_call(MagicMock(url="https://www.apple.com.cn/shop/bag/status", method="GET")))

    async def test_after_submit_it_waits_for_apples_order_page_and_reports_the_order_number(self):
        # Live 10-15 (W0000000001): the 立即下单 POST took 18.7 s with the review page still shown and its
        # button enabled again, then "正在处理" (/shop/checkout/status) for ~1 min, then the thank-you page.
        live = Path(__file__).parent / "fixtures" / "apple-live-2026-10-15"
        load = lambda name: json.loads((live / (name + ".json")).read_text(encoding="utf-8"))
        self.worker.trial = self.worker.active = True
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.submitted, self.worker.submitted_at = True, time.monotonic()
        finished, attention = [], []
        self.worker.event.connect(lambda ev: finished.append(ev) if ev["type"] == "operator.finished" else None)
        self.worker.event.connect(lambda ev: attention.append(ev) if ev["type"] == "operator.attention" else None)
        for name in ("review-after-submit", "order-status"):
            page = load(name)
            self.worker.page.url = page["url"]
            self.worker.page.evaluate.return_value = page
            self.worker.last_progress = self.worker.last_checkout_activity = time.monotonic() - 30   # long and quiet
            with patch("duo.operator.perform") as perform:
                await self.worker.tick()
            perform.assert_not_called()
            self.assertFalse(self.worker.paused, name)
            self.assertEqual(self.worker.last_state, "等待下单结果", name)
        self.assertEqual(attention, [], "a slow order answer is never a take-over prompt")
        page = load("order-thankyou")
        self.worker.page.url = page["url"]
        self.worker.page.evaluate.return_value = page
        await self.worker.tick()
        self.assertEqual(self.worker.last_state, "下单成功")
        self.assertIn("W0000000001", self.worker.last_message)
        self.assertEqual(finished[-1]["success"], True)
        self.assertEqual(self.worker.read_journal()["order"], "W0000000001")
        self.assertFalse(self.worker.active)
        # No order page 3 minutes after the click: ask the user to check (still never a second click).
        self.worker.active, self.worker.submitted_at = True, time.monotonic() - 181
        page = load("order-status")
        self.worker.page.url = page["url"]
        self.worker.page.evaluate.return_value = page
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)
        self.assertIn("订单列表", self.worker.last_message)

    def test_a_lost_or_endless_checkout_request_cannot_stall_the_route(self):
        # Live 10-15: the delivery route sat on "等待官网处理送货选项" until the 20 s stall check paused it.
        lost = MagicMock(url="https://secure10.www.apple.com.cn/shop/checkoutx/fulfillment", method="POST")
        self.worker.on_request(lost)
        self.assertTrue(self.worker.checkout_busy())
        self.worker.inflight[id(lost)] = (time.monotonic() - 19, "/shop/checkoutx/fulfillment")
        self.worker.last_checkout_activity = time.monotonic() - 19
        self.assertFalse(self.worker.checkout_busy(), "a call with no answer for 18 s is treated as finished")
        self.assertEqual(self.worker.inflight, {})
        # An endless stream of calls: busy at most CHECKOUT_BUSY_MAX_S in a row.
        self.worker.on_request(MagicMock(url="https://secure10.www.apple.com.cn/shop/checkoutx/x", method="POST"))
        self.assertTrue(self.worker.checkout_busy())
        self.worker.busy_since = time.monotonic() - 21
        self.assertFalse(self.worker.checkout_busy())
        # Apple answering slowly is activity, not a stall.
        self.worker.last_progress = time.monotonic() - 25
        self.worker.last_checkout_activity = time.monotonic() - 3
        self.assertFalse(self.worker.stalled())
        self.worker.last_checkout_activity = time.monotonic() - 21
        self.assertTrue(self.worker.stalled())

    async def test_every_page_and_action_is_kept_in_the_runs_screenshot_history(self):
        self.worker.page.screenshot = AsyncMock()
        self.worker.start_shot_history("trial")
        snap = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Fulfillment-init&x=1", "text": "李测试 18100000000",
                "nodes": [], "summary": ""}
        self.worker.archive("page-checkout", snap, "选择门店取货。")
        await self.worker.shot_task
        self.worker.archive("click", snap, "继续")
        await self.worker.shot_task
        files = sorted(p.name for p in self.worker.shot_dir.iterdir())
        self.assertEqual([f for f in files if f.endswith(".json")][0][:3], "001")
        self.assertEqual(len([f for f in files if f.endswith(".json")]), 2)
        record = json.loads((self.worker.shot_dir / [f for f in files if f.endswith(".json")][0]).read_text(encoding="utf-8"))
        self.assertEqual(record["url"], "https://secure10.www.apple.com.cn/shop/checkout?_s=Fulfillment-init")
        self.assertNotIn("18100000000", json.dumps(record, ensure_ascii=False), "personal data is masked")
        self.assertEqual(self.worker.page.screenshot.await_count, 2)
        # Verification / login pages are never recorded.
        self.worker.archive("verify", dict(snap, phone_verification=True), "")
        self.assertEqual(self.worker.page.screenshot.await_count, 2)
        # Old runs are pruned by count.
        root = self.worker.shot_dir.parent
        for i in range(40):
            (root / f"20261001-0000{i:02d}-trial").mkdir(parents=True)
        self.worker.start_shot_history("trial")
        self.assertLessEqual(len([p for p in root.iterdir() if p.is_dir()]), 30)

    def test_checkout_clicks_get_a_longer_read_back_window(self):
        click = Decision("click", "在送货地址浮层点“使用”确认地址。", {"selector": "#apply", "kind": "button"})
        checkout = {"url": "https://secure10.www.apple.com.cn/shop/checkout?_s=Fulfillment-init"}
        self.assertEqual(Worker.confirm_limit(click, checkout), 20.0)
        self.assertEqual(Worker.confirm_limit(click, {"url": "https://www.apple.com.cn/shop/bag"}), 8.0)
        self.assertEqual(Worker.confirm_limit(click, {"url": "https://www.apple.com.cn/shop/bag", "busy": True}), 20.0)
        self.worker.page.url = checkout["url"]
        self.worker.page.evaluate.return_value = {"url": checkout["url"], "text": "同一页面", "nodes": []}
        same = {"url": checkout["url"], "text": "同一页面", "nodes": []}
        unchanged = self.worker.fingerprint_of(same)
        self.worker.pending = (click, checkout["url"], unchanged, time.monotonic() - 9)
        self.assertTrue(self.worker.check_pending(same))
        self.assertFalse(self.worker.paused, "9 s on checkout is still inside the window")
        self.worker.pending = (click, checkout["url"], unchanged, time.monotonic() - 21)
        self.worker.check_pending(same)
        self.assertTrue(self.worker.paused)

    async def test_add_waits_for_the_last_configuration_change_to_settle(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.page.evaluate.return_value = capture("air-configured")
        self.worker.last_config_change = time.monotonic()           # AC+ was just selected
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
            perform.assert_not_called()
            self.assertEqual(self.worker.last_state, "等待配置生效")
            self.assertFalse(self.worker.adapter.added)
            self.worker.last_config_change = time.monotonic() - 2   # settled, shop XHR quiet
            self.worker.last_shop_activity = time.monotonic() - 1
            await self.worker.tick()
        perform.assert_awaited_once()
        self.assertTrue(self.worker.adapter.added)
        # The wait is bounded: a page that never goes quiet is not a deadlock.
        fresh = Worker(Path(self.temp.name))
        fresh.last_config_change = fresh.last_shop_activity = time.monotonic()
        fresh.add_settle_started = time.monotonic() - 3.5
        self.assertTrue(fresh.add_settled())

    async def test_bag_mismatch_inside_grace_period_waits_instead_of_pausing(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.added = True
        self.worker.page.url = "https://www.apple.com.cn/shop/bag"
        half_rendered = {"url": self.worker.page.url, "title": "购物袋", "text": "购物袋 正在加载", "nodes": [], "summary": ""}
        self.worker.page.evaluate.return_value = half_rendered
        await self.worker.tick()
        self.assertFalse(self.worker.paused)
        self.assertEqual(self.worker.phase, "bag")
        self.assertIn("等待购物袋加载", self.worker.last_message)
        self.worker.bag_entered_at = time.monotonic() - 9
        await self.worker.tick()
        self.assertTrue(self.worker.paused, "after the grace period a real mismatch still asks the user")

    async def test_signin_without_guest_button_waits_briefly_then_reports_login_required(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.added = True
        self.worker.page.url = "https://secure10.www.apple.com.cn/shop/signIn"
        widget_only = {"url": self.worker.page.url, "title": "登录", "text": "现在登录，结账更快捷。", "nodes": [], "summary": ""}
        self.worker.page.evaluate.return_value = widget_only
        seen = []
        self.worker.event.connect(lambda ev: seen.append(ev["type"]))
        await self.worker.tick()
        self.assertFalse(self.worker.paused)
        self.assertIn("游客结账入口", self.worker.last_message)
        self.assertNotIn("internal.login-required", seen)
        self.worker.signin_seen_at = time.monotonic() - 7
        await self.worker.tick()
        self.assertTrue(self.worker.paused)
        self.assertIn("internal.login-required", seen)

    async def test_warm_shield_cookie_without_new_challenge_is_ready_quickly(self):
        record = self.worker.shield_for()
        record["proven"] = False
        record["verify"] = {"status": 200, "t": time.monotonic() - 1.2}
        self.worker.context = MagicMock()
        self.worker.context.cookies = AsyncMock(return_value=[{"name": "shld_bt_ck", "value": "abc|123|sig"}])
        self.assertTrue(await self.worker.shield_ready())
        self.assertTrue(record["proven"])
        cold = Worker(Path(self.temp.name))
        cold.page = MagicMock()
        cold.context = MagicMock()
        cold.context.cookies = AsyncMock(return_value=[])
        fresh = cold.shield_for()
        fresh["verify"] = {"status": 200, "t": time.monotonic() - 1.2}
        self.assertFalse(await cold.shield_ready(), "no cookie and no challenge yet: keep waiting")

    async def test_first_click_waits_for_shield_handshake(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.page.evaluate.return_value = capture("air-configured")
        record = self.worker.shield_for()
        record["proven"] = False
        record["verify"] = {"status": 200, "t": time.monotonic()}    # verify.js seen, PoW not done yet
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
            perform.assert_not_called()
            self.assertEqual(self.worker.last_state, "等待安全盾")
            self.assertFalse(self.worker.adapter.added)
            record["proven"] = True
            await self.worker.tick()
        perform.assert_awaited_once()
        self.assertTrue(self.worker.adapter.added)

    async def test_trial_retry_requires_observed_empty_bag_even_without_journal(self):
        self.worker.page.url = "https://www.apple.com.cn/shop/bag"
        self.worker.page.evaluate.return_value = {"url": self.worker.page.url, "text": "购物袋中有商品", "nodes": []}
        with patch.object(self.worker, "ensure_browser"), patch.object(self.worker, "navigate"), patch.object(self.worker, "prepare_race") as race:
            await self.worker.handle("trial-retry", Config())
        race.assert_not_called()

    async def test_new_headless_profile_cannot_clear_or_recover_legacy_edge_record(self):
        record = {"phase": "add-attempt", "trial": False, "fulfillment": "pickup"}
        self.worker.journal_path.write_text(json.dumps(record), encoding="utf-8")
        with patch.object(self.worker, "ensure_browser") as browser:
            await self.worker.handle("reset-record", Config())
            await self.worker.handle("recover", Config())
        browser.assert_not_called()
        self.assertEqual(self.worker.read_journal(), record)

    async def test_unattended_trial_stops_before_sms_and_can_auto_accept_consent(self):
        self.worker.trial = True
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.adapter.added = self.worker.adapter.product_verified = True
        self.worker.adapter.chosen_store = "环球港"
        verify = {"selector": "#verify", "kind": "button", "label": "验证", "selected": False, "disabled": False,
                  "value": "", "name": "", "id": "verify", "required": False, "options": [], "context": "手机"}
        page = {"url": "https://secure8.www.apple.com.cn/shop/checkout?_s=PickupContact", "title": "", "nodes": [verify],
                "text": "请验证手机号码 手机", "summary": "", "phone_verified": False}
        self.worker.page.url = page["url"]
        self.worker.page.evaluate.return_value = page
        seen = []
        self.worker.event.connect(seen.append)
        # Default (attended rehearsal): the SMS is requested, as the user asked on 10-05.
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_awaited_once()
        self.assertTrue(self.worker.adapter.sms_requested)
        # Unattended run: stop right before the verification click.
        unattended = Worker(Path(self.temp.name))
        unattended.cfg, unattended.trial, unattended.trial_sms_allowed = self.worker.cfg, True, False
        unattended.adapter = Adapter(unattended.cfg, AIR_TARGET)
        unattended.adapter.added = unattended.adapter.product_verified = True
        unattended.adapter.chosen_store = "环球港"
        unattended.page = self.worker.page
        unattended.active, unattended.entry_started = True, True
        unattended.target, unattended.deadline = self.worker.target, self.worker.deadline
        unattended.shield_for()["proven"] = True
        finished = []
        unattended.event.connect(lambda ev: finished.append(ev) if ev["type"] == "operator.finished" else None)
        with patch("duo.operator.perform") as perform:
            await unattended.tick()
        perform.assert_not_called()
        self.assertFalse(unattended.active)
        self.assertEqual(finished[-1]["reason"], "trial-contact")
        self.assertEqual(unattended.phase, "verify")
        # With automatic terms switched off the user is asked through the app's consent dialog.
        consent_page = {"url": page["url"], "title": "", "text": "需要你签署本知情同意书",
                        "nodes": [{"selector": "#c", "kind": "checkbox", "selected": False, "label": "我已阅读", "value": "", "name": "", "id": "c",
                                   "required": False, "options": [], "context": ""}], "summary": ""}
        self.worker.page.evaluate.return_value = consent_page
        self.worker.cfg.auto_consent = False
        self.worker.active, self.worker.paused = True, False
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_not_called()
        self.assertIn("operator.consent", [e["type"] for e in seen])
        self.assertTrue(self.worker.paused)
        # Switched on (default since 10-15, formal runs too): ticked without asking first.
        self.worker.cfg.auto_consent = True
        self.worker.paused = False
        asked = len([e for e in seen if e["type"] == "operator.consent"])
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
        perform.assert_awaited()
        self.assertEqual(len([e for e in seen if e["type"] == "operator.consent"]), asked + 1,
                         "only a box that stays unticked after the attempt re-asks the user")

    async def test_consent_click_is_read_back_and_reasked_when_the_box_stays_unticked(self):
        self.worker.active = True
        box = {"selector": "#consent", "kind": "checkbox", "selected": False, "label": "我已阅读", "value": "", "name": "", "id": "consent"}
        page = {"url": "https://secure8.www.apple.com.cn/shop/checkout", "text": "需要你签署本知情同意书", "nodes": [box]}
        self.worker.approval_request = ("req", self.worker.fingerprint_of(page))
        self.worker.page.evaluate.return_value = page
        seen = []
        self.worker.event.connect(lambda ev: seen.append(ev["type"]))
        with patch("duo.operator.perform", side_effect=TimeoutError) as perform:
            await self.worker.handle("consent", {"request": "req", "accept": True})
        perform.assert_awaited_once()
        self.assertTrue(self.worker.paused, "an unticked box never lets the flow continue")
        self.assertIn("operator.consent", seen, "the user is asked again with a fresh request id")
        self.assertNotEqual(self.worker.approval_request[0], "req")
        # Once the box reads back as ticked the operator resumes.
        ticked = dict(page, nodes=[dict(box, selected=True)])
        request = self.worker.approval_request[0]
        self.worker.page.evaluate.side_effect = [page, ticked, ticked]
        with patch("duo.operator.perform") as perform:
            await self.worker.handle("consent", {"request": request, "accept": True})
        self.assertFalse(self.worker.paused)

    async def test_stale_privacy_confirmation_cannot_check_changed_page(self):
        self.worker.active = True
        self.worker.approval_request = ("old", "stale-page-token")
        self.worker.page.evaluate.return_value = {"url": self.worker.page.url, "text": "知情同意书", "nodes": []}
        with patch("duo.operator.perform") as perform:
            await self.worker.handle("consent", "old")
        perform.assert_not_called()
        self.assertTrue(self.worker.paused)

    async def test_timeout_after_submit_is_durable_and_never_repeated(self):
        self.worker.cfg.auto_submit = True
        self.worker.adapter.chosen_store = "环球港"
        with patch("duo.operator.perform", side_effect=TimeoutError) as perform:
            await self.worker.review({"summary": self.summary(), "nodes": []}, Decision("review", "提交"))
            await self.worker.review({"summary": self.summary(), "nodes": []}, Decision("review", "提交"))
        self.assertEqual(perform.await_count, 1)
        self.assertEqual(self.worker.read_journal()["phase"], "submit-attempt")
        restarted = Worker(Path(self.temp.name))
        with patch.object(restarted, "ensure_browser") as launch:
            await restarted.handle("now", Config())
        launch.assert_not_called()
        self.assertFalse(restarted.active)

    async def test_store_click_is_not_confirmation(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        snap = capture("air-stores-ready")
        self.worker.page.evaluate.return_value = snap
        with patch("duo.operator.perform") as perform:
            await self.worker.tick()
            self.assertFalse(self.worker.adapter.chosen_store)
            await self.worker.tick()
        self.assertEqual(perform.await_count, 1)
        self.worker.page.evaluate.return_value = capture("air-store-ready")
        with patch("duo.operator.perform"):
            await self.worker.tick()
        self.assertEqual(self.worker.adapter.chosen_store, "环球港")
        self.assertTrue(self.worker.adapter.store_verified)

    async def test_add_timeout_is_never_repeated(self):
        self.worker.adapter = Adapter(self.worker.cfg, AIR_TARGET)
        self.worker.page.evaluate.return_value = capture("air-configured")
        with patch("duo.operator.perform", side_effect=TimeoutError) as perform:
            await self.worker.tick()
            await self.worker.tick()
        self.assertEqual(perform.await_count, 1)
        self.assertEqual(self.worker.read_journal()["phase"], "add-attempt")


class EntryRaceTests(unittest.IsolatedAsyncioTestCase):
    def page(self, status=200):
        response = MagicMock(status=status)
        response.all_headers = AsyncMock(return_value={})
        page = MagicMock()
        page.url = AIR_TEST_URL
        page.goto = AsyncMock(return_value=response)
        page.evaluate = AsyncMock(return_value=capture("air-start"))
        page.close = AsyncMock()
        page.is_closed.return_value = False
        return page

    def race(self, pages):
        cfg = Config()
        return EntryRace(pages, AIR_TEST_URL, cfg, lambda: Adapter(cfg, AIR_TARGET), MagicMock())

    async def test_stuck_first_page_does_not_block_second_winner(self):
        first, second = self.page(), self.page()
        blocked = asyncio.Event()
        first.goto.side_effect = lambda *args, **kwargs: None
        async def stuck(*args, **kwargs):
            await blocked.wait()
        first.goto.side_effect = stuck
        race = self.race([first, second])
        await race.poll()
        await asyncio.sleep(0)
        race.next_launch = 0
        await race.poll()
        await asyncio.sleep(0)
        result = await race.poll()
        self.assertIs(result[0], second)
        first.close.assert_awaited_once()
        second.close.assert_not_awaited()
        self.assertTrue(race.closed)

    async def test_429_blocks_every_new_lane_for_full_retry_after(self):
        first, second = self.page(429), self.page()
        first.goto.return_value.all_headers.return_value = {"retry-after": "300"}
        race = self.race([first, second])
        await race.poll()
        await asyncio.sleep(0)
        race.next_launch = 0
        await race.poll()
        self.assertGreater(race.cooldown - time.monotonic(), 299)
        second.goto.assert_not_awaited()
        await race.stop()

    async def test_network_timeout_releases_lane_with_backoff(self):
        page = self.page()
        page.goto.side_effect = TimeoutError
        race = self.race([page])
        await race.poll()
        await asyncio.sleep(0)
        await race.poll()
        self.assertEqual(race.lanes[0].errors, 1)
        self.assertGreater(race.lanes[0].due, time.monotonic() + 2)
        await race.stop()

    async def test_cancellation_does_not_leave_background_navigation(self):
        page = self.page()
        race = self.race([page])
        await race.poll()
        task = race.lanes[0].task
        await race.stop()
        self.assertTrue(task.done())
        self.assertIsNone(await race.poll())

    def test_retry_after_accepts_http_date_and_long_seconds(self):
        self.assertEqual(parse_retry_after("300"), 300)
        self.assertEqual(parse_retry_after("nonsense"), 0)
        self.assertGreater(parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 300)


if __name__ == "__main__":
    unittest.main()

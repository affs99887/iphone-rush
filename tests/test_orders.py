"""Dispatcher races and failure injection; no test creates a merchant order."""
import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PySide6.QtCore import QCoreApplication
from duo.browser import Adapter, Decision
from duo.operator import Operator as Worker
from duo.model import Config
from duo.orders import OrderCoordinator, one_per_customer

URL = "https://www.apple.com.cn/shop/checkout"


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gate = OrderCoordinator(self.root)

    def test_per_person_limit_detection_is_explicit(self):
        for text in ["每人限购1台", "每位顾客最多只能购买一部", "每个账户仅可下一单", "同一手机号限购：1 件", "Limit 1 per customer"]:
            with self.subTest(text=text):
                self.assertTrue(one_per_customer({"url": URL, "text": text}))
        for text in ["每个订单限购1台", "数量：1", "每人限购10台", "每人限购一百台", "每位顾客限购2部", "每人最多绑定1张卡"]:
            self.assertFalse(one_per_customer({"url": URL, "text": text}))
        self.assertFalse(one_per_customer({"url": "https://apple.com.cn.example/checkout", "text": "每人限购1台"}))

    def test_default_allows_two_before_any_observed_restriction(self):
        self.assertEqual(self.gate.status()["policy"], "adaptive")
        self.assertTrue(self.gate.claim("pickup")[0])
        self.assertTrue(self.gate.claim("delivery")[0])
        self.assertFalse(self.gate.claim("pickup")[0])

    def test_two_real_threads_compete_for_one_durable_claim(self):
        self.gate.observe({"url": URL, "text": "每人限购1台"})
        barrier = threading.Barrier(2)
        def claim(mode):
            barrier.wait(timeout=3)
            return mode, self.gate.claim(mode)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("pickup", "delivery")))
        self.assertEqual(sum(allowed for _, allowed in results), 1)
        winner = next(mode for mode, allowed in results if allowed)
        self.assertEqual(list(json.loads(self.gate.path.read_text(encoding="utf-8"))["attempts"]), [winner])

    def test_late_limit_uses_existing_attempt_and_never_issues_second(self):
        self.assertTrue(self.gate.claim("delivery")[0])
        self.gate.observe({"url": URL + "?session=secret", "text": "每位顾客限购一部"})
        self.assertFalse(self.gate.claim("pickup")[0])
        self.assertNotIn("secret", self.gate.path.read_text(encoding="utf-8"))

    def test_timeout_and_restart_never_release_the_winner(self):
        self.gate.set_policy("single")
        self.gate.claim("pickup")
        self.gate.outcome("pickup", "unknown")
        restarted = OrderCoordinator(self.root)
        self.assertFalse(restarted.claim("delivery")[0])
        self.assertFalse(restarted.claim("pickup")[0])
        with self.assertRaises(ValueError):
            restarted.set_policy("adaptive")

    def test_legacy_submission_is_respected_after_limit_appears(self):
        (self.root / "purchase-state.json").write_text(json.dumps({"phase": "submit-attempt", "trial": False}), encoding="utf-8")
        gate = OrderCoordinator(self.root)
        gate.observe({"url": URL, "text": "每人限购1台"})
        self.assertFalse(gate.claim("delivery")[0])

    def test_corrupt_record_and_disk_failure_never_grant_permission(self):
        self.gate.path.write_text("not json", encoding="utf-8")
        broken = OrderCoordinator(self.root)
        self.assertFalse(broken.claim("pickup")[0])
        with patch.object(self.gate, "_write", side_effect=OSError("disk full")):
            self.gate.observe({"url": URL, "text": "每人限购1台"})
            self.assertFalse(self.gate.claim("delivery")[0])


class DispatchWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gate = OrderCoordinator(self.root)
        self.gate.observe({"url": URL, "text": "每人限购1台"})
        self.workers = []
        for mode in ("pickup", "delivery"):
            worker = Worker(self.root / mode, mode, self.gate)
            worker.cfg = Config(fulfillment=mode, auto_submit=True, last_name="李", first_name="测试", phone="18100000000", email="fixture@example.invalid", shipping_address="测试路1号")
            worker.adapter = Adapter(worker.cfg)
            worker.adapter.chosen_store = "静安" if mode == "pickup" else ""
            worker.active = True
            self.workers.append(worker)

    def review(self, mode):
        destination = "取货：上海 静安" if mode == "pickup" else "送货地址：上海市嘉定区测试路1号"
        return {"url": URL, "text": "", "summary": "iPhone Duo 星光白色 1TB 数量：1 AppleCare+ 服务计划\n" + destination + " 李测试 18100000000 fixture@example.invalid\n订单总计：RMB 24,098\n电子发票 - 个人 招商银行分期 24 期", "nodes": []}

    async def test_worker_never_clicks_loser_even_when_winner_response_is_blocked(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending_click(*args):
            entered.set()
            await release.wait()
        with patch("duo.operator.perform", side_effect=pending_click) as perform:
            pickup, delivery = self.workers
            task = asyncio.create_task(pickup.review(self.review("pickup"), Decision("review", "提交")))
            await asyncio.wait_for(entered.wait(), 2)
            await delivery.review(self.review("delivery"), Decision("review", "提交"))
            self.assertTrue(delivery.paused)
            self.assertFalse(delivery.submitted)
            release.set()
            await task
            perform.assert_awaited_once()
        self.assertEqual(self.gate.status()["attempts"]["pickup"]["phase"], "submit-attempt")

    async def test_invalid_or_unaccepted_review_cannot_win(self):
        pickup, delivery = self.workers
        bad = self.review("pickup")
        bad["summary"] = bad["summary"].replace("1TB", "2TB")
        with patch("duo.operator.perform") as perform:
            await pickup.review(bad, Decision("review", "提交"))
            self.assertFalse(self.gate.status()["attempts"])
            agreement = self.review("pickup")
            agreement["nodes"] = [{"kind": "checkbox", "label": "同意购买条款", "selected": False}]
            pickup.cfg.auto_consent = False            # terms that were not accepted (automatic terms is off)
            await pickup.review(agreement, Decision("review", "提交"))
            self.assertFalse(self.gate.status()["attempts"])
            await delivery.review(self.review("delivery"), Decision("review", "提交"))
        perform.assert_awaited_once()
        self.assertIn("delivery", self.gate.status()["attempts"])

    async def test_old_otp_and_resume_cannot_restart_loser_after_winner_claims(self):
        self.gate.claim("pickup")
        delivery = self.workers[1]
        delivery.otp_request = "old"
        delivery.otp_waiting = True
        delivery.snapshot = AsyncMock()
        await delivery.handle("check-dispatcher", None)
        await delivery.handle("otp", {"request": "old", "code": "000000"})
        await delivery.handle("resume", None)
        self.assertTrue(delivery.paused)
        self.assertFalse(delivery.otp_waiting)
        delivery.snapshot.assert_not_awaited()

    async def test_trial_never_claims_or_submits_on_its_own(self):
        worker = self.workers[0]
        worker.trial = True
        with patch("duo.operator.perform") as perform:
            await worker.review(self.review("pickup"), Decision("review", "提交"))
        perform.assert_not_called()
        self.assertFalse(self.gate.status()["attempts"])

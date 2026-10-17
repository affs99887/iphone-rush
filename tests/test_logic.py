import unittest
from datetime import datetime, timezone

from duo.browser import Adapter
from duo.model import Config, product_checks, retry_delay, review_checks, trusted_url


def node(label, kind="radio", selected=False, disabled=False, value="", context=""):
    return dict(label=label, kind=kind, selector="#" + str(abs(hash(label))), selected=selected,
                disabled=disabled, value=value, context=context or label, name="", id="", options=[], required=False)


def snapshot(nodes=None, text="", url="https://www.apple.com.cn/shop/checkout", summary=""):
    return dict(nodes=nodes or [], text=text, url=url, summary=summary, auth=False, challenge=False, busy=False)


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(last_name="李", first_name="测试", phone="18100000000")
        self.adapter = Adapter(self.cfg)

    def test_beijing_schedule_independent_of_host_timezone(self):
        self.assertEqual(self.cfg.start_time().astimezone(timezone.utc), datetime(2026, 10, 16, 12, tzinfo=timezone.utc))

    def test_apple_host_cannot_be_spoofed(self):
        self.assertTrue(trusted_url("https://secure.www.apple.com.cn/shop/checkout"))
        for url in ["https://apple.com.cn.evil.example", "http://apple.com.cn", "https://evilapple.com.cn", "https://apple.com.cn@evil.example"]:
            self.assertFalse(trusted_url(url))

    def test_maintenance_and_presale_refresh(self):
        self.assertEqual(self.adapter.plan(snapshot(text="我们很快就会回来")).kind, "refresh")
        self.assertEqual(self.adapter.plan(snapshot(text="暂未发售", url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo")).kind, "refresh")

    def test_disabled_ac_alone_does_not_prove_presale(self):
        snap = snapshot([node("AppleCare+ 服务计划", disabled=True)], url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo")
        self.assertEqual(self.adapter.plan(snap).kind, "unknown")

    def test_negative_ac_and_two_tb_are_not_valid(self):
        checks = product_checks(["星光白色", "2TB", "不折抵换购", "不加 AppleCare+ 服务计划"])
        self.assertFalse(checks["care"])
        self.assertFalse(checks["capacity"])

    def test_consent_overlay_clicks_only_the_affirmative_button_once_enabled(self):
        # Live 10-14 overlay: two consent boxes, "同意并继续" (disabled until both are ticked),
        # "不同意并继续购物" and "关闭".
        boxes = [node("我同意 Apple 的隐私政策", "checkbox"), node("我了解有限的个人信息可能会在中国境外存储和处理", "checkbox")]
        agree = node("同意并继续", "button", disabled=True)
        buttons = [agree, node("不同意并继续购物", "button"), node("关闭", "button")]
        text = "Apple 和你的数据隐私 需要你签署本知情同意书。"
        url = "https://secure10.www.apple.com.cn/shop/signIn"
        self.assertEqual(self.adapter.plan(snapshot(boxes + buttons, text=text, url=url)).kind, "manual")
        for b in boxes:
            b["selected"] = True
        self.assertEqual(self.adapter.plan(snapshot(boxes + buttons, text=text, url=url)).kind, "wait")
        agree["disabled"] = False
        dec = self.adapter.plan(snapshot(boxes + buttons, text=text, url=url))
        self.assertEqual((dec.kind, dec.node["label"]), ("click", "同意并继续"))

    def test_capacity_in_bag_does_not_merge_with_next_line(self):
        # Like Apple's real bag, each line item (phone and AC+) has its own 移除 control.
        snap = snapshot([node("数量", "select-one", value="1"), node("结账", "button"),
                         node("移除 iPhone Duo 1TB 星光白色", "button"), node("移除 AppleCare+ 服务计划", "button")],
                        url="https://www.apple.com.cn/shop/bag",
                        summary="iPhone Duo 星光白色 1TB\nAppleCare+ 服务计划")
        self.assertEqual(self.adapter.plan(snap).kind, "click")
        snap["nodes"][0]["value"] = ""
        self.assertEqual(self.adapter.plan(snap).kind, "manual")
        # Without the AC+ line item the same text is only an upsell: never check out without AppleCare+.
        snap["nodes"][0]["value"] = "1"
        snap["nodes"].pop()
        self.assertEqual(self.adapter.plan(snap).kind, "manual")

    def test_correct_product_selected_before_add(self):
        controls = [node(t, selected=True) for t in ["星光白色", "1TB", "不折抵换购", "AppleCare+ 服务计划"]]
        controls.append(node("继续", "button"))
        snap = snapshot(controls, url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo")
        self.assertEqual(self.adapter.plan(snap).kind, "add")
        self.adapter.added = True
        self.assertEqual(self.adapter.plan(snap).kind, "manual")

    def test_any_stock_selected_without_waiting_for_preferred(self):
        controls = [node("Apple 浦东", context="浦东 10 月 23 日可取货"), node("Apple 静安", disabled=True, context="静安 暂无")]
        dec = self.adapter.plan(snapshot(controls, text="上海取货"))
        self.assertEqual(dec.kind, "store")
        self.assertEqual(dec.value, "浦东（国金中心）")

    def test_preferred_group_follows_visible_order(self):
        # 环球港 appears first; no rigid preference for 静安 or 环贸.
        controls = [node("Apple 浦东"), node("Apple 环球港"), node("Apple 静安"), node("Apple 上海环贸 iapm")]
        self.assertEqual(self.adapter.plan(snapshot(controls, text="上海取货")).value, "环球港")

    def test_unavailable_store_not_selected(self):
        controls = [node("Apple 静安", context="静安 无法取货"), node("Apple 南京东路", context="南京东路 10 月 23 日可取货")]
        self.assertEqual(self.adapter.plan(snapshot(controls, text="上海取货")).value, "南京东路")

    def test_one_sold_out_shop_does_not_block_selected_available_shop(self):
        self.adapter.chosen_store = "上海环贸 iapm"
        controls = [node("Apple 上海环贸 iapm", selected=True), node("Apple 静安", disabled=True), node("继续", kind="button")]
        dec = self.adapter.plan(snapshot(controls, text="上海门店 静安暂无可用取货，环贸可取货"))
        self.assertEqual(dec.kind, "click")

    def test_faq_id_mention_is_not_identity_form(self):
        snap = snapshot(text="取货需携带身份证原件", nodes=[node("继续", kind="button")])
        self.assertNotIn("证件号码字段", self.adapter.plan(snap).message)
        snap["nodes"].append(node("居民身份证号码", kind="text"))
        self.assertEqual(self.adapter.plan(snap).kind, "manual")

    def test_review_requires_all_evidence_and_budget(self):
        summary = "iPhone Duo 星光白色 1TB\n数量：1\nAppleCare+ 服务计划\n不折抵换购\n取货：上海 静安\n李测试\n18100000000\n订单总计：RMB 23,898.00\n电子发票 - 个人\n招商银行分期 24 期\n" + self.cfg.email
        self.assertTrue(review_checks(summary, self.cfg, "静安")[0])
        for bad in [summary.replace("1TB", "2TB"), summary.replace("数量：1", "数量：2"), summary.replace("AppleCare+", "不加 AppleCare+"), summary.replace("23,898", "33,898"), summary.replace("18100000000", "***0000")]:
            self.assertFalse(review_checks(bad, self.cfg, "静安")[0])
        self.assertFalse(review_checks(summary, self.cfg, "环球港")[0])

    def test_throttling_and_retry_after(self):
        self.assertGreaterEqual(retry_delay(1.8, 1, 429), 15)
        self.assertEqual(retry_delay(1.8, 1, 503, 60), 60)
        self.assertEqual(retry_delay(1.8), 1.8)

    def test_payment_or_confirmation_is_terminal(self):
        for text, expected in [("请扫码完成付款", "payment"), ("订单已确认", "complete")]:
            self.assertEqual(self.adapter.plan(snapshot(text=text)).kind, expected)

    def test_required_email_pauses_before_continuing(self):
        self.cfg.email = ""
        self.adapter.chosen_store = "静安"
        email = node("电子邮箱", kind="email")
        email["required"] = True
        dec = self.adapter.plan(snapshot([email, node("继续", kind="button")]))
        self.assertEqual(dec.kind, "manual")
        self.assertIn("电子邮箱", dec.message)

    def test_date_dropdown_skips_unavailable_slots(self):
        self.adapter.chosen_store = "静安"
        date = node("取货日期", kind="select-one")
        date["options"] = [dict(value="", text="请选择", disabled=False), dict(value="23", text="10 月 23 日", disabled=True), dict(value="24", text="10 月 24 日", disabled=False)]
        dec = self.adapter.plan(snapshot([date]))
        self.assertEqual((dec.kind, dec.value), ("option", "24"))


if __name__ == "__main__":
    unittest.main()

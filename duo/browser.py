from __future__ import annotations

import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import CHINA, CMB_24, Config, DUO_TARGET, ProductTarget, STORE_ALIASES, has_one_tb, normalize, product_checks

OTP_SETTLE_S = 15.0   # after an SMS code was typed, Apple may take this long to replace the 验证 button
FINAL_BUTTON = r"^(下订单|立即下单|提交订单|确认订单|确认并下单|立即支付|立即付款|placeorder|支付并订购)$"
FINAL_LABEL = re.compile(r"下订单|立即下单|提交订单|确认订单|确认并下单|立即支付|立即付款|place\s*order|支付并订购", re.I)


def is_review_page(snap) -> bool:
    """Apple's order review step (checkout?_s=Review / "准备下单了吗？")."""
    url = (snap.get("url") or "").lower()
    return "_s=review" in url or "准备下单了吗" in (snap.get("text") or "")[:400]


def is_final_label(text: str) -> bool:
    return bool(FINAL_LABEL.search(normalize(text or "")))


# Read only DOM inventory. It does not use private Apple APIs or network endpoints.
INVENTORY = r"""() => {
  const visible = e => !!e && !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const root = [...document.querySelectorAll('[role=dialog]')].find(visible) || document.querySelector('main,[role=main]') || document.body;
  const labelText = e => (e.innerText || '').trim() || [...e.querySelectorAll('img[alt]')].map(i=>i.alt).join(' ');
  const label = e => e.getAttribute('aria-label') ||
    (e.labels && [...e.labels].map(labelText).join(' ').trim()) ||
    (e.getAttribute('aria-labelledby') || '').split(' ').map(id => document.getElementById(id)?.innerText || '').join(' ').trim() ||
    e.innerText || e.getAttribute('placeholder') || e.getAttribute('title') || '';
  const selector = e => {
    if (e.id && document.querySelectorAll('#' + CSS.escape(e.id)).length === 1) return '#' + CSS.escape(e.id);
    let parts = [];
    for (let p = e; p && p !== document.body; p = p.parentElement) {
      const tag = p.tagName.toLowerCase();
      const peers = p.parentElement ? [...p.parentElement.children].filter(x => x.tagName === p.tagName) : [p];
      parts.unshift(tag + (peers.length > 1 ? ':nth-of-type(' + (peers.indexOf(p) + 1) + ')' : ''));
      if (p.id) {parts[0] = '#' + CSS.escape(p.id); break;}
    }
    return parts.join(' > ');
  };
  const nodes = [...root.querySelectorAll('button,a,input,select,[role=radio],[role=checkbox],[role=button]')]
    .filter(e => !e.closest('nav,footer') && e.type !== 'password' &&
       (visible(e) || (e.labels && [...e.labels].some(visible))))
    .map(e => {
      let context = e.closest('label,li,article,[data-store-id],[data-store-number]') || e.parentElement;
      for (let p = context, depth = 0; p && p !== root && depth < 5; p = p.parentElement, depth++) {
        if ((p.innerText || '').length > 1000 || p.querySelectorAll('input[type=radio],[role=radio]').length > 1) break;
        context = p;
      }
      return {
        selector: selector(e), label: label(e).replace(/\s+/g,' ').trim(),
        kind: ['radio','checkbox'].includes(e.getAttribute('role')) ? e.getAttribute('role') : e.type || e.getAttribute('role') || e.tagName.toLowerCase(),
        name: e.name || '', id: e.id || '',
        selected: !!e.checked || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true',
        disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
        value: /验证码|校验码|位数\s*\d|verification.?code|one.?time|password|card.?number|security.?code|cvv|信用卡|卡号/i.test(label(e)+' '+e.name+' '+e.id) ? '' :
          ['text','search','tel','email','number','select-one'].includes(e.type) ? e.value : '',
        required: !!e.required || e.getAttribute('aria-required') === 'true',
        context: (context?.innerText || '').slice(0,1500),
        href: e.tagName === 'A' ? e.href : '',
        options: e.tagName === 'SELECT' ? [...e.options].map(o => ({text:o.text, value:o.value, disabled:o.disabled})) : []
      };
    });
  let summary = [...root.querySelectorAll('[data-order-summary],.rs-checkout-summary,.rs-bag-item,.rs-bag-product,.rs-review,.rs-checkout-review,[class*=order-summary]')]
    .filter(visible).map(x => x.innerText).join('\n');
  // On checkout, use visible main content; product FAQ text is excluded.
  if (/checkout|review/.test(location.pathname) || (!summary && /bag/.test(location.pathname))) summary = root.innerText;
  const imageLabels = [...root.querySelectorAll('img[alt]')].filter(visible).map(e=>e.alt);
  if (/checkout|review/.test(location.pathname)) summary += '\n' + imageLabels.filter(x=>/^(招商银行|支付宝|微信支付|中国建设银行|工商银行)$/.test(x)).join('\n');
  return {url: location.href, title: document.title, ready_state: document.readyState, text: root.innerText.slice(0,18000), nodes, summary,
    phone_verified: imageLabels.includes('验证成功'),
    auth: [...document.querySelectorAll('input[type=password]')].some(visible),
    challenge: [...document.querySelectorAll('iframe')].some(e => /captcha|challenge/i.test(e.src || '') && visible(e)),
    frames: [...document.querySelectorAll('iframe')].filter(visible).map(e=>({selector:selector(e),name:e.name,id:e.id,src:(()=>{try{return new URL(e.src).origin+new URL(e.src).pathname}catch{return ''}})()})),
    busy: [...root.querySelectorAll('[aria-busy=true],[alt="正在处理"]')].some(visible)};
}"""


@dataclass
class Decision:
    kind: str
    message: str
    node: dict | None = None
    value: str = ""


def missing_page(snap):
    return "/shop/404" in snap["url"].lower() or bool(re.search(
        r"page not found|the page you.{0,2}re looking for[\s\S]{0,40}can.{0,2}t be found|找不到你要查找的页面|找不到你要找的页面|网页不存在",
        snap.get("title", "") + "\n" + snap["text"], re.I
    ))


EMPTY_BAG = re.compile(r"购物袋(?:中|里)?(?:还)?(?:是)?空的|购物袋里没有|购物袋为空|购物袋中没有(?:任何)?商品|your bag is empty", re.I)


def bag_is_empty(snap):
    """Apple's own empty-bag notice on the bag page; never inferred from a missing summary."""
    return "/bag" in snap.get("url", "").lower() and bool(EMPTY_BAG.search(snap.get("text", "")))


def blocked_page(snap):
    """Akamai / Apple access-denied interstitials (HTTP 541 and friends render these)."""
    return bool(re.search(r"access denied|访问被拒绝|you don.{0,2}t have permission to access|请完成安全验证|验证你是人类|人机验证",
                          snap.get("title", "") + "\n" + snap.get("text", "")[:3000], re.I))


class Adapter:
    def __init__(self, config: Config, target: ProductTarget = DUO_TARGET):
        self.cfg = config
        self.target = target
        self.product_verified = False
        self.chosen_store = ""
        self.added = False
        self.last_clicked = ""
        self.manual_nodes: set[str] = set()
        self.sms_requested = False
        self.payment_verified = False
        self.contact_verified = False
        self.store_verified = False
        self.delivery_verified = False
        self.address_verified = False
        self.delivery_eta = ""
        self.page_error_recovered = False
        # Delivery address overlay / shipping option bookkeeping (live 2026-10-14).
        self.apply_selector = ""          # the overlay's "使用" button once clicked
        self.address_applied = False      # the overlay was confirmed and has closed again
        self.rejected_slots: set = set()  # slot identities Apple refused to continue with
        self.current_slot = None          # identity of the shipping option currently kept
        self.continued_with = None        # identity selected when "继续填写送货地址" was last clicked
        self.otp_submitted_at = 0.0       # set by the operator when it typed an SMS code
        self.phone_confirmed = False      # the 验证 button disappeared after the code was accepted
        self.order_number = ""            # "W…" read from Apple's thank-you page

    @staticmethod
    def matches(node, pattern):
        return bool(re.search(pattern, normalize(node["label"]), re.I))

    def find(self, snap, pattern, kinds=None, include_disabled=False):
        return next((n for n in snap["nodes"] if self.matches(n, pattern)
                     and (kinds is None or n["kind"] in kinds)
                     and (include_disabled or not n["disabled"])), None)

    def choice(self, snap, pattern, message):
        node = self.find(snap, pattern, ["radio", "checkbox"])
        if node and not node["selected"]:
            return Decision("select", message, node)
        return None

    def click(self, snap, pattern, message):
        node = self.find(snap, pattern, ["button", "submit", "a"])
        if node:
            return Decision("click", message, node)
        return None

    def selected_checks(self, snap):
        return product_checks([n["label"] for n in snap["nodes"] if n["selected"]], self.target)

    def plan(self, snap) -> Decision:
        text = snap["text"]
        ntext = normalize(text)
        url = snap["url"].lower()
        product = self.target.path in url
        otp = next((n for n in snap["nodes"] if n["kind"] in ("text", "tel", "number") and
                    re.search(r"验证码|校验码|verification.?code|one.?time|(?:^|[._-])otp(?:$|[._-])",
                              n["label"] + " " + n["name"] + " " + n["id"], re.I)), None)
        if self.cfg.guest_checkout and "/signin" in url and not snap.get("challenge") and not otp:
            guest = self.click(snap, r"^以游客身份继续$", "使用本路独立会话，以游客身份结账。")
            if guest:
                return guest
        if snap.get("phone_verification") or (otp and "/checkout" in url and not snap.get("auth")):
            return Decision("otp", "请在软件窗口输入 Apple 发来的短信验证码。", otp)
        if snap.get("auth") or otp or re.search(r"输入验证码|双重认证", text):
            return Decision("manual", "请在浏览器完成 Apple 账户登录或验证码，再点“继续自动操作”。")
        if snap.get("challenge") or re.search(r"验证你是人类|安全验证|人机验证|访问被拒绝|access denied", text, re.I):
            return Decision("manual", "官网要求安全验证，请在浏览器处理后继续。")
        if re.search(r"需要你签署|知情同意书", text):
            if any(n["kind"] == "checkbox" and not n["selected"] for n in snap["nodes"]):
                return Decision("manual", "官网要求确认隐私知情同意书，请在软件确认框阅读后选择。")
            # Live 10-14: the overlay's confirm button is "同意并继续" (disabled until both boxes are
            # ticked) next to "不同意并继续购物" and "关闭" — only the affirmative one is ever clicked.
            consent_next = self.find(snap, r"^(同意并继续|继续|继续结账)$", ["button", "submit"], include_disabled=True)
            if consent_next:
                if consent_next["disabled"]:
                    return Decision("wait", "等待官网启用“同意并继续”。")
                return Decision("click", "继续已确认的官网隐私步骤。", consent_next)
            if snap.get("busy"):
                return Decision("wait", "等待同意书页面更新。")
        if "/signin" in url:
            # Guest entry missing (or account session expired): the dispatcher decides (§13.2).
            return Decision("login-required", "官网要求登录 Apple 账户，当前没有可用的游客结账入口。"
                            if self.cfg.guest_checkout else "Apple 账户会话已失效，需要重新登录。")
        if re.search(r"你的连接不是私密连接|您的连接不是私密连接|证书错误", text):
            return Decision("manual", "浏览器显示连接安全问题，请检查连接。")
        if missing_page(snap):
            if not self.added:
                return Decision("refresh", "官网购买页暂时无法打开，重新检查入口。")
            if not self.page_error_recovered:
                return Decision("recover-bag", "官网返回页面不存在，核对现有购物袋，不重复加购。")
            return Decision("manual", "官网仍返回页面不存在，已保留加购记录。请检查现有购物袋后继续。")
        if "/session_expired" in url or "你的操作已超时" in text:
            node = self.find(snap, r"^返回购物袋$", ["a", "button"])
            if self.added and node:
                return Decision("recover-bag", "会话已过期，返回现有购物袋重新核对，不重复加购。", node)
            return Decision("manual", "官网会话已过期，请重新准备并登录。")
        # Live 10-15 (both test orders): after 立即下单 Apple goes /shop/checkout/status ("正在处理", ~1 min)
        # → /shop/checkout/thankyou: "你的订单正在等待付款，请使用“掌上生活” App 扫描二维码支付。… 订单 #W0000000001".
        order = re.search(r"订单\s*(?:号\s*[:：#]?\s*|#\s*)([Ww]\d{6,})", text)
        if "/shop/checkout/thankyou" in url or (order and re.search(r"等待付款|扫码支付|感谢|订单已", text)):
            self.order_number = order.group(1).upper() if order else ""
            number = f"订单号 {self.order_number}" if self.order_number else "官网已显示订单页"
            pay = ("请在 30 分钟内用“掌上生活” App 扫描页面二维码付款。" if "掌上生活" in text
                   else "请按页面提示在 30 分钟内完成付款。")
            return Decision("complete", f"下单成功（{number}）。{pay}")
        if "/shop/checkout/status" in url:
            return Decision("wait", "官网正在处理订单，等待订单结果页。")
        if re.search(r"订单已确认|感谢你的订购|感谢您的订购|thank you for your order|订单号\s*[:：]\s*[Ww]\d+", text, re.I):
            return Decision("complete", "官网已显示订单确认，请按页面提示完成付款并查看取货通知。")
        if re.search(r"请扫码|扫描二维码|支付剩余时间|付款二维码|打开微信.*支付|等待付款", text):
            return Decision("payment", "已到付款页面，请立即在浏览器付款。自动操作已停止。")
        id_node = next((n for n in snap["nodes"] if n["kind"] in ("text", "number") and
                       re.search(r"身份证|证件号码|identity|national.?id", n["label"] + n["id"] + n["name"], re.I) and not n["value"]), None)
        if id_node:
            return Decision("manual", "官网出现证件号码字段，请直接在官网填写；软件不保存身份证号。")
        if re.search(r"我们很快|马上回来|正在更新|正在维护|we.?ll be back|be right back", text[:2400], re.I):
            return Decision("refresh", "官网处于维护状态，继续刷新购买入口。")
        if snap.get("busy") or re.search(r"^正在处理\s*$", text, re.M):
            return Decision("wait", "等待页面完成加载。")

        if self.product_verified:
            bag_link = self.click(snap, r"^(查看购物袋|检查购物袋|查看购物袋并结账|查看购物袋和结账)$", "查看已配置的购物袋。")
            if bag_link:
                return bag_link
            if "配件" in text and "/checkout" not in url:
                skip = self.click(snap, r"^(继续，不添加配件|继续而不添加配件|不需要，谢谢|跳过配件)$", "继续，不添加额外配件。")
                if skip:
                    return skip

        if product:
            return self.product(snap)
        if "/bag" in url:
            return self.bag(snap)

        # Live 10-15: Apple renders 立即下单 *disabled* for a few seconds while the review page settles,
        # and gives it the same id (rs-checkout-continue-button-bottom) as every 继续 button. If the
        # planner looked only for enabled final buttons it would fall through to the generic 继续
        # patterns and a stale "继续" click would land on the now-enabled 立即下单 — placing an order
        # without the submit gate. So a final button, enabled or not, always means "review": wait.
        final = self.find(snap, FINAL_BUTTON, ["button", "submit"], include_disabled=True)
        if final:
            if final["disabled"]:
                return Decision("wait", "等待官网启用“立即下单”，先核对订单摘要。")
            return Decision("review", "核对订单摘要。", final)
        if is_review_page(snap):
            return Decision("wait", "已到订单核对页，等待“立即下单”出现。")

        fulfillment = self.pickup(snap) if self.cfg.fulfillment == "pickup" else self.delivery(snap)
        if fulfillment:
            return fulfillment
        contact = self.contact(snap)
        if contact:
            return contact

        invoice = self.choice(snap, r"^电子发票[-－—]?个人$", "选择个人电子发票。")
        if invoice:
            return invoice

        if self.cfg.payment == CMB_24:
            installment = self.installment(snap)
            if installment:
                return installment
        elif self.cfg.payment != "手动选择":
            pat = r"^(微信支付|wechatpay)$" if self.cfg.payment == "微信支付" else r"^(支付宝|alipay)$"
            node = self.find(snap, pat, ["radio", "button"])
            if node and not node["selected"] and node["selector"] != self.last_clicked:
                return Decision("select" if node["kind"] == "radio" else "click", "选择" + self.cfg.payment, node)
        elif re.search(r"选择付款方式|付款信息|支付方式", text):
            return Decision("manual", "请在官网选择付款方式，再继续自动操作。")

        # Never accept legal checkboxes automatically or blindly press an unknown submit.
        required = [n for n in snap["nodes"] if n["required"] and not n["disabled"] and
                    not (self.payment_verified and n["id"] == "checkout.billing.billingOptions.bankLookUp.selectBank") and
                    ((n["kind"] in ("text", "tel", "email", "number") and not n["value"]) or
                     (n["kind"] == "checkbox" and not n["selected"]))]
        if required:
            return Decision("manual", "官网还有必填项或协议待确认，请在浏览器完成后继续。")
        verify_phone = self.find(snap, r"^(验证|验证手机号码|验证手机号)$", ["button", "submit"], True)
        if verify_phone and "手机" in text:
            if verify_phone["disabled"]:
                return Decision("wait", "等待官网启用手机号验证。")
            if self.sms_requested:
                # Live 10-14: right after the code was typed Apple still showed "验证" for a moment while
                # POST /shop/checkoutx/shipping was in flight; the button vanished once it answered.
                if snap.get("checkout_busy") or time.monotonic() - self.otp_submitted_at < OTP_SETTLE_S:
                    return Decision("wait", "验证码已提交，等待官网确认手机验证结果。")
                return Decision("manual", "已请求发送验证码，但官网未确认成功。请检查官网提示；软件不会重复发送。")
            return Decision("send-code", "向联系手机号发送 Apple 短信验证码。", verify_phone)
        if self.sms_requested and not snap.get("phone_verification") and "手机" in text:
            # The 验证 button is gone and the code frame closed: Apple accepted the phone verification.
            self.phone_confirmed = True
        for pattern in [r"^(继续填写取货详情|继续填写送货地址|继续选择付款方式|继续付款|继续至付款|继续前往付款|继续查看订单|查看订单|检查订单|继续至订单检查|继续)$",
                        r"^(保存并继续|使用此取货信息|继续结账)$"]:
            dec = self.click(snap, pattern, "继续结账流程。")
            if dec:
                if self.cfg.fulfillment == "pickup" and not self.chosen_store:
                    return Decision("manual", "尚未确认上海门店取货，请在官网选择取货门店再继续。")
                if self.cfg.fulfillment == "delivery":
                    if not self.delivery_verified:
                        return Decision("manual", "尚未确认送货到家，请检查收货方式。")
                    if dec.node["label"] != "继续填写送货地址" and not self.address_verified:
                        return Decision("manual", "送货地址尚未完整核对，请检查官网地址后继续。")
                return dec
        return Decision("unknown", "页面暂时没有可识别的下一步。")

    def product(self, snap):
        # SSR can expose enabled native controls before the purchase handlers
        # are loaded. A visible button alone does not establish readiness.
        if snap.get("ready_state", "complete") != "complete":
            return Decision("wait", "等待官网购买脚本完成加载，避免点击尚未就绪的加购表单。")
        ntext = normalize(snap["text"])
        # Apple unlocks capacity, trade-in, then AC+ progressively. A disabled
        # downstream control is not evidence that preorders have not opened.
        continuation = self.find(snap, r"^(继续|添加到购物袋|加入购物袋)$", ["button", "submit"], True)
        choices = [
            (r"^(" + re.escape(normalize(self.target.color)) + r"|白色)$", "选择" + self.target.color + "。"),
            (r"^1tb(?:脚注|rmb|$)", "选择 1TB。"),
            (r"^(不折抵换购|不折抵|不需要折抵|不抵扣|无折抵)$", "选择不折抵换购。"),
            (r"^applecare\+", "添加 AppleCare+ 服务计划。"),
        ]
        for pattern, message in choices:
            dec = self.choice(snap, pattern, message)
            if dec:
                return dec
        checks = self.selected_checks(snap)
        if not all(checks.values()):
            if "暂未发售" in ntext or "尚未开售" in ntext:
                return Decision("refresh", "已处理当前可选配置，尚未开放预购，继续刷新。")
            missing = {"color": "白色", "capacity": "1TB", "trade": "不折抵", "care": "AppleCare+"}
            return Decision("unknown", "等待配置控件：" + "、".join(missing[k] for k, v in checks.items() if not v))
        self.product_verified = True
        if "暂未发售" in ntext or "尚未开售" in ntext:
            return Decision("refresh", "白色、1TB、不折抵及 AC+ 已核对，等待开售并刷新。")
        if continuation and not continuation["disabled"]:
            if self.added:
                return Decision("manual", "已添加过一台设备。请核对购物袋，避免重复添加。")
            return Decision("add", "配置已核对，继续添加一台到购物袋。", continuation)
        return Decision("wait", "配置已选好，等待官网允许继续。")

    def bag(self, snap):
        if bag_is_empty(snap):
            # The operator decides: a second add is only allowed once, after a
            # journaled add-attempt whose item is verifiably absent (brief §8.5).
            return Decision("empty-bag", "官网购物袋为空。")
        # Confirm exact model and AC+ in the actual bag, not in a footer or FAQ.
        summary = normalize(snap.get("summary") or "")
        phone_ok = all(s in summary for s in (normalize(self.target.name), normalize(self.target.color))) and has_one_tb(snap.get("summary") or "")
        # The bag's own "添加 AppleCare+" upsell also says "AppleCare+", so text presence proves nothing.
        # A real AC+ line item is the one Apple lets you remove (10-05 capture: "移除 适用于 … AppleCare+ …").
        care_in_bag = any(self.matches(n, r"^移除.*applecare\+") for n in snap["nodes"])
        if phone_ok and not care_in_bag:
            # The add raced Apple's asynchronous AC+ attach (live 10-14): add it right here in the bag,
            # read back on the next pass. One click per control; then the user is asked.
            offer = self.find(snap, r"^添加.*applecare\+", ["button", "a"])
            if offer and offer["selector"] != self.last_clicked:
                return Decision("click", "购物袋中缺少 AppleCare+，直接在购物袋内添加。", offer)
            plan = self.find(snap, r"^applecare\+(?!.*(?:不加|月付|每月))", ["radio"])
            if plan and not plan["selected"]:
                return Decision("select", "选择 AppleCare+ 服务计划。", plan)
            confirm = self.find(snap, r"^(添加到购物袋|加入购物袋|添加|确认添加|确认)$", ["button", "submit"])
            if confirm and offer is None and confirm["selector"] != self.last_clicked:
                return Decision("click", "确认添加 AppleCare+。", confirm)
            if snap.get("busy"):
                return Decision("wait", "等待购物袋更新 AppleCare+。")
            return Decision("manual", "购物袋中有 " + self.target.name + " 但缺少 AppleCare+，且未找到可用的添加按钮，请在官网添加后继续。")
        if not phone_ok:
            return Decision("manual", "请核对购物袋：需要 " + self.target.name + " " + self.target.color + " 1TB 和 AppleCare+。")
        quantities = [n for n in snap["nodes"] if n["kind"] in ("select-one", "number", "text") and
                      re.search(r"数量|quantity", n["label"] + n["name"], re.I)]
        if not quantities:
            return Decision("manual", "请在官网确认购物袋中只有一台 iPhone，再继续。")
        if len(quantities) != 1 or quantities[0]["value"] != "1":
            return Decision("manual", "购物袋商品数量不是 1，请在官网修正。")
        # Do not buy someone else's existing cart contents.
        if re.search(r"iphone(?:18|17|16|air|duo)|ipad|macbook|airpods|保护壳|电源适配器", summary.replace(normalize(self.target.name), "")):
            return Decision("manual", "购物袋中检测到其他商品，请先核对。")
        self.product_verified = True
        return self.click(snap, r"^(结账|结算|前往结账|checkout)$", "进入结账，保留购物袋配置。") or Decision("unknown", "等待结账按钮。")

    def pickup(self, snap):
        text = snap["text"]
        # Store search and city selectors belong to the pickup section only.
        for node in snap["nodes"]:
            desc = node["label"] + node["name"] + node["id"]
            if node["kind"] == "select-one" and re.search(r"城市|city", desc, re.I):
                option = next((o for o in node["options"] if "上海" in o["text"]), None)
                if option and node["value"] != option["value"]:
                    return Decision("option", "筛选上海直营店。", node, option["value"])
            if node["kind"] in ("text", "search") and re.search(r"邮政编码|城市或|城市、|查找零售店|postal|store.?search", desc, re.I) and not node["value"]:
                return Decision("fill-search", "查找上海门店。", node, "200120")
        choice = self.choice(snap, r"^(我要取货|到店取货|零售店取货|取货|pickup)(?:$|：|在)", "选择 Apple Store 门店取货。")
        if choice:
            return choice
        pickup_button = self.find(snap, r"^(我要取货|到店取货|取货)$", ["button"])
        if pickup_button and pickup_button["selector"] != self.last_clicked:
            return Decision("click", "选择门店取货。", pickup_button)
        tab = self.find(snap, r"^(我要取货|到店取货|取货)$", ["radio"], True)
        if tab and tab["selected"] and not self.chosen_store:
            if snap.get("checkout_busy"):
                return Decision("wait", "等待官网处理取货选项。")
            if self.find(snap, r"^继续填写送货地址$", ["button", "submit"], True):
                # Live 10-15 (and the 10-05 capture): right after 我要取货 Apple keeps the delivery panel
                # (送货至… / 继续填写送货地址) on screen until the store list arrives. Not a missing store.
                return Decision("wait", "等待官网加载上海取货门店列表。")
        candidates = []
        for store in self.cfg.stores:
            aliases = STORE_ALIASES.get(store, [store])
            for node in snap["nodes"]:
                if node["kind"] not in ("radio", "button", "a") or node["disabled"]:
                    continue
                if not any(normalize(a) in normalize(node["label"]) for a in aliases):
                    continue
                context = normalize(node["context"] + node["label"])
                if re.search(r"无货|无法取货|不可取货|暂无|不提供|售罄|无可用|unavailable|outofstock", context):
                    continue
                if node["kind"] == "a" and not re.search(r"取货|pickup|零售店", normalize(text[:2000])):
                    continue
                # Inspect the current list once; never wait for a preferred shop.
                candidates.append((0 if store in self.cfg.preferred_stores else 1,
                                   snap["nodes"].index(node), store, node))
        if candidates:
            _, _, store, node = min(candidates, key=lambda c: (c[0], c[1]))
            if node["selected"]:
                self.chosen_store = store
                self.store_verified = True
            elif node["selector"] != self.last_clicked:
                self.store_verified = False
                return Decision("store", "选择上海 " + store + " 取货。", node, store)
        if not self.chosen_store and not candidates:
            more = self.click(snap, r"^显示更多取货地点$", "展开其余门店，检查更多上海库存。")
            if more and more.node["selector"] != self.last_clicked:
                return more
        if not self.chosen_store and not candidates and re.search(r"无可用取货|暂无取货|门店.*暂无|所有.*无货|暂时无法.*取货", text):
            return Decision("stock", "上海门店暂时没有可选库存，等待下一轮查询。")
        if not self.chosen_store and re.search(r"取货.*查看供货|选择.*零售店|选择取货地点", text):
            return self.click(snap, r"^(查看供货情况|查看其他零售店|选择零售店|查找零售店|选择取货地点)$", "打开门店供货查询。") or Decision("stock", "等待上海门店可取货。")
        if self.chosen_store:
            # Choose the first enabled date/time offered by Apple's chronological UI.
            for node in snap["nodes"]:
                if node["kind"] == "select-one" and not node["disabled"] and re.search(r"取货日期|取货时间|取货时段|pickupdate|pickuptime", normalize(node["label"] + node["name"])):
                    first = next((o for o in node["options"] if o["value"] and not o["disabled"] and not re.search(r"请选择|选择日期|选择时间", o["text"])), None)
                    if first and node["value"] != first["value"]:
                        return Decision("option", "选择首个可用取货时段。", node, first["value"])
            for pattern in [r"^\d{1,2}月\d{1,2}日", r"^\d{1,2}[:：]\d{2}[-–至]", r"^(上午|下午|晚上)\d"]:
                node = self.find(snap, pattern, ["radio", "button"])
                if node and not node["selected"] and node["selector"] != self.last_clicked:
                    return Decision("select" if node["kind"] == "radio" else "click", "选择页面列出的首个可用取货时段。", node)
        return None

    def installment(self, snap):
        if not re.search(r"你希望如何付款|分期付款方案", snap["text"]):
            return None
        if is_review_page(snap):
            # The review page summarises "分期付款方案：24 个月…" without the bank radios; it is not the
            # payment step (live 10-15 this was misread as "招商银行分期不可用").
            return None
        self.payment_verified = False
        bank = self.find(snap, r"^招商银行$", ["radio"], True)
        if not bank or bank["disabled"]:
            return Decision("manual", "官网当前未提供招商银行信用卡分期，已停止；不会更换银行。")
        if not bank["selected"]:
            return Decision("select", "选择招商银行信用卡分期。", bank)
        term = self.find(snap, r"^24期(?:0%年化利率|年化利率|rmb|$)", ["radio"], True)
        if not term or term["disabled"]:
            # After selecting the bank Apple lazily loads its term controls.
            other_terms = self.find(snap, r"^(1|3|6|12)期", ["radio"], True)
            return Decision("manual" if other_terms else "wait", "招商银行 24 期不可选，已停止。" if other_terms else "等待官网加载招商银行分期期数。")
        if not term["selected"]:
            return Decision("select", "选择招商银行 24 期。", term)
        self.payment_verified = True
        return None

    @staticmethod
    def shipping_date_key(node):
        text = node["label"]
        dates = re.findall(r"(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})", text)
        if dates:
            values = [tuple(map(int, date)) for date in dates]
            return max(values), min(values)
        for word, days in (("今天", 0), ("明天", 1), ("后天", 2)):
            if word in text:
                day = datetime.now(CHINA) + timedelta(days=days)
                return (day.year, day.month, day.day), (day.year, day.month, day.day)
        return (9999, 12, 31), (9999, 12, 31)

    def performed(self, dec: "Decision") -> None:
        """The operator calls this only after a decision was actually performed on the page.

        Live 10-14: recording "continued with option X" when the 继续 decision was merely *planned*
        (the click was then held back while Apple was still busy) made every option look refused.
        """
        node = dec.node or {}
        if dec.kind != "click":
            return
        if "deliveryaddressoverlay" in normalize(node.get("id", "") + node.get("name", "")):
            self.apply_selector = node.get("selector", "")
        elif normalize(node.get("label", "")) == "继续填写送货地址" and self.current_slot is not None:
            self.continued_with = self.current_slot

    @classmethod
    def slot_identity(cls, node):
        """(day, time window) of a shipping option; the same option may be labelled differently."""
        window = re.search(r"\d{1,2}[:：]\d{2}\s*[-–至~]\s*\d{1,2}[:：]\d{2}|白天|上午|下午|晚上", node["label"])
        return cls.shipping_date_key(node)[1], re.sub(r"\s", "", window.group(0)) if window else ""

    def delivery(self, snap):
        overlay_open = any("deliveryaddressoverlay" in normalize(n["id"] + n["name"]) for n in snap["nodes"])
        self.current_slot = None
        if overlay_open:
            self.continued_with = None        # the last 继续 opened the overlay; that is not a refusal
        elif self.apply_selector:
            # The overlay's "使用" was clicked and the overlay has closed: the address is now on the order.
            self.address_applied = True
            if self.last_clicked == self.apply_selector:
                self.last_clicked = ""        # a later re-opened overlay may be confirmed again
        tab = self.find(snap, r"^(为我送货|送货到家|我要送货)$", ["radio"], True)
        if tab:
            if snap.get("checkout_busy") and not overlay_open:
                # Live 10-14: selecting an option or clicking 继续 makes Apple POST /shop/checkoutx/fulfillment
                # for ~10 s. Until it answers, the page still shows the old state — never judge it then.
                return Decision("wait", "等待官网处理送货选项。")
            self.delivery_verified = bool(tab["selected"] and not tab["disabled"])
            if tab["disabled"]:
                return Decision("stock", "官网当前不能选择送货到家，等待供货更新。")
            if not tab["selected"]:
                self.address_verified = False
                return Decision("select", "选择送货到家。", tab)
            slots = [n for n in snap["nodes"] if n["kind"] == "radio" and
                     "shippingOptions" in n["name"] and not n["disabled"] and "免费" in n["label"]]
            if not slots:
                if snap.get("busy"):
                    return Decision("wait", "等待官网加载送货日期。")
                return Decision("stock", "没有可确认的免费送货时段，等待供货更新。")
            chosen = next((s for s in slots if s["selected"]), None)
            # Refusal = we clicked 继续 with this option, Apple finished, and we are back here with it still
            # selected. (The standing "请选择其他送货方式以继续结账" text alone proves nothing.)
            if (chosen is not None and not overlay_open and self.continued_with is not None
                    and self.slot_identity(chosen) == self.continued_with):
                self.rejected_slots.add(self.continued_with)
                self.continued_with = None
            # "最快今天送达" and "预计送达日期： 今天送达" are the same option written twice: compare by date + window.
            candidates = [s for s in slots if self.slot_identity(s) not in self.rejected_slots]
            if not candidates:
                return Decision("manual", "官网不接受已尝试的送货时段，且没有其他免费送货时段，请在官网检查。")
            if self.address_applied and chosen is not None and self.slot_identity(chosen) not in self.rejected_slots:
                # Live 10-14: after the address was confirmed Apple re-rendered the options and the express
                # "今天送达" came and went; chasing it re-opened the overlay forever. Once the address is on the
                # order, keep the option Apple currently has selected and move on.
                slot = chosen
            else:
                slot = min(candidates, key=self.shipping_date_key)
            if not slot["selected"]:
                message = ("官网不接受“" + chosen["label"] + "”，改选下一个最早的免费送货时段。"
                           if chosen is not None and self.slot_identity(chosen) in self.rejected_slots
                           else "选择官网当前最早的免费送货时段。")
                return Decision("select", message, slot)
            self.current_slot = self.slot_identity(slot)
            self.delivery_eta = slot["label"]

        # Only fill fields explicitly identified as address controls. Other
        # required fields, suggestions and saved-address selectors stay manual.
        matched = set()
        region_text = normalize(snap["text"])
        for node in snap["nodes"]:
            if node["disabled"] or node["kind"] not in ("text", "select-one"):
                continue
            desc = normalize(node["label"] + " " + node["name"] + " " + node["id"])
            field = value = None
            if "provincecitydistrict" in desc:
                # Live 10-14 (Shipping-init): "收货地址：" is a picker input already showing "上海 嘉定区".
                # It cannot be typed into; verify what it shows instead.
                shown = normalize(node["value"])
                if not shown:
                    return Decision("manual", "请在官网选择收货地址的省市区（" + self.cfg.shipping_province +
                                    self.cfg.shipping_district + "），我会继续。")
                wanted = (self.cfg.shipping_province, self.cfg.shipping_district)
                if not all(re.sub(r"[省市]$", "", normalize(v)) in shown for v in wanted):
                    self.address_verified = False
                    return Decision("manual", "官网收货地址的省市区为“" + node["value"].strip() + "”，与设置不一致，请在官网修正。")
                matched.update(("province", "city", "district"))
                continue
            if re.search(r"province|stateprovince|省份|省/直辖市|^省", desc):
                field, value = "province", self.cfg.shipping_province
            elif re.search(r"district|county|区县|市辖区|^区", desc):
                field, value = "district", self.cfg.shipping_district
            elif re.search(r"(?:^|[._-])city|城市|^市", desc):
                field, value = "city", self.cfg.shipping_city
            elif re.search(r"street2|addressline2|address2|地址第二行|地址行2|补充地址", desc):
                field, value = "extra", ""
            elif re.search(r"street|addressline1|address1|详细地址|街道|地址行1", desc):
                field, value = "address", self.cfg.shipping_address
            elif re.search(r"postalcode|postcode|zipcode|邮政编码|邮编", desc):
                if self.cfg.shipping_postcode:
                    field, value = "postcode", self.cfg.shipping_postcode
                elif node["required"] and not node["value"]:
                    return Decision("manual", "官网要求邮政编码，请在送货设置填写准确邮编；不会猜测。")
            if field is None:
                continue
            if node["kind"] == "select-one":
                # Municipalities may be rendered as 上海 or 上海市.
                def region(s):
                    return re.sub(r"[省市]$", "", normalize(s))
                option = next((o for o in node["options"] if not o["disabled"] and region(o["text"]) == region(value)), None)
                if not option:
                    return Decision("manual", "官网地址选项尚未提供所填写的省市区，请检查地址。")
                value = option["value"]
            if node["value"] != value:
                self.address_verified = False
                return Decision("option" if node["kind"] == "select-one" else "fill", "填写送货地址。", node, value)
            matched.add(field)
        def region(s):
            # Municipalities are written 上海 / 上海市; compare without a trailing 省/市.
            return re.sub(r"[省市]$", "", normalize(s))

        if "address" in matched:
            regions = (("province", self.cfg.shipping_province), ("district", self.cfg.shipping_district))
            self.address_verified = all(key in matched or region(val) in region_text for key, val in regions)
        # Live 10-14: "继续填写送货地址" opens a modal address overlay (ids checkout.fulfillment.
        # deliveryAddressOverlay.*): a province/city/district picker button labelled "上海 嘉定区", street /
        # street2 inputs, and the confirm button "使用" (id …addressStreets.apply) next to 取消 / 关闭.
        # Its own "使用" must be clicked; the page-level continue button behind the modal only times out.
        overlay = [n for n in snap["nodes"] if "deliveryaddressoverlay" in normalize(n["id"] + n["name"])]
        if overlay:
            if "address" not in matched:
                return None          # the overlay's street field is not readable yet
            picker = next((n for n in overlay if "provincecitydistrict" in normalize(n["id"] + n["name"])), None)
            if picker is not None:
                shown = normalize(picker["label"])
                if not all(region(v) in shown for v in (self.cfg.shipping_province, self.cfg.shipping_district)):
                    self.address_verified = False
                    return Decision("manual", "送货地址浮层中的省市区为“" + picker["label"].strip() +
                                    "”，与设置不一致，请在浮层中修正后点“使用”。")
                self.address_verified = True
            if not self.address_verified:
                return Decision("manual", "无法核对送货地址的省市区，请在浮层中确认后点“使用”。")
            save = next((n for n in overlay if n["kind"] in ("button", "submit") and not n["disabled"]
                         and (self.matches(n, r"^(使用|使用此地址|使用这个地址|使用该地址|保存|保存地址|应用|完成|确认)$")
                              or re.search(r"\.(apply|save|submit)$", n["id"], re.I))
                         and not self.matches(n, r"取消|返回|放弃|编辑|cancel|back|dismiss|close")), None)
            if save and save["selector"] != self.last_clicked:
                return Decision("click", "在送货地址浮层点“使用”确认地址。", save)
            return Decision("manual", "送货地址浮层已填写，请在浮层中点击“使用/保存”确认，我会继续。")
        return None

    def contact(self, snap):
        if self.cfg.fulfillment == "pickup" and not self.chosen_store:
            return None
        if self.cfg.fulfillment == "delivery" and not self.delivery_verified:
            return None
        missing_email = False
        matched = set()
        for node in snap["nodes"]:
            if node["kind"] not in ("text", "tel", "email") or node["disabled"]:
                continue
            desc = normalize(node["label"] + " " + node["name"] + " " + node["id"])
            value = None
            if re.search(r"身份证|证件|信用卡|银行卡|卡号|密码|验证码|搜索|search|promo|优惠", desc):
                continue
            if re.search(r"firstname|givenname|(^|姓名)名|^名$", desc):
                value = self.cfg.first_name
                matched.add("first")
            elif re.search(r"lastname|familyname|^姓$|姓氏", desc):
                value = self.cfg.last_name
                matched.add("last")
            elif re.search(r"姓名|全名|fullname|取货人", desc) and "电话" not in desc:
                value = self.cfg.last_name + self.cfg.first_name
            elif re.search(r"手机|电话|phone|mobile", desc):
                value = self.cfg.phone
                matched.add("phone")
            elif re.search(r"邮箱|电子邮件|email", desc):
                if not self.cfg.email and not node["value"]:
                    missing_email = True
                    continue
                value = self.cfg.email or node["value"]
                matched.add("email")
            if value is not None and node["value"] != value:
                self.contact_verified = False
                return Decision("fill", "填写联系信息。", node, value)
        if missing_email:
            return Decision("manual", "官网需要电子邮箱，请直接在官网填写后继续。下次运行前也可在面板预填。")
        if matched == {"first", "last", "phone", "email"}:
            self.contact_verified = (bool(snap.get("phone_verified")) or self.phone_confirmed
                                     or self.cfg.fulfillment == "delivery")
        return None


class FinalButtonGuard(RuntimeError):
    """Raised instead of clicking: the element is now an order-placing button but the decision is not a submit."""


async def perform(page, dec: Decision):
    node = dec.node
    if not node:
        return
    loc = page.locator(node["selector"])
    if dec.kind not in ("review", "fill", "fill-search", "option"):
        # Last line of defence. Apple reuses ids: 检查订单 on the payment page and 立即下单 on the review
        # page are both #rs-checkout-continue-button-bottom. Live 10-15 a 检查订单 click waited for the
        # element to become enabled while the page swapped, and pressed 立即下单. So (1) refuse if the
        # element already reads as an order button, and (2) scope the locator so Playwright's own
        # actionability retries can never resolve to an order button (it times out instead of clicking).
        try:
            live = await loc.first.evaluate("e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim()",
                                            timeout=1500)
        except Exception:
            live = ""
        if is_final_label(live):
            raise FinalButtonGuard("refused to click an order-placing button for '" + dec.message + "'")
        loc = loc.filter(has_not_text=FINAL_LABEL)
    if dec.kind in ("fill", "fill-search"):
        await loc.fill(dec.value, timeout=2500)
        if dec.kind == "fill-search":
            await loc.press("Enter", timeout=2500)
    elif dec.kind == "option":
        await loc.select_option(dec.value, timeout=2500)
    elif node["kind"] in ("radio", "checkbox"):
        # Apple's swatches cover the native color input; trade-in controls do
        # the reverse. Use their observed targets, with an idempotent fallback.
        if await loc.is_checked():
            return
        linked = page.locator("label[for=" + json.dumps(node["id"]) + "]") if node["id"] else None
        has_label = linked is not None and await linked.count() == 1 and await linked.is_visible()
        if node["name"] in ("dimensionColor", "dimensionCapacity") and has_label:
            await linked.click(timeout=2500)
        else:
            try:
                await loc.check(timeout=700)
            except Exception:
                if await loc.is_checked():
                    return
                # Live 10-14: the privacy-consent checkboxes are opacity-0 inputs behind styled labels
                # inside an animating overlay; the first label's centre is a policy link, so a label
                # click opens the link instead of toggling the box. A DOM click on the input itself is
                # geometry-free and reaches React's change handler; the label square on the far left
                # is the last resort. The caller always reads the state back before moving on.
                try:
                    await loc.evaluate("e => e.click()")
                except Exception:
                    pass
                if await loc.is_checked():
                    return
                if not has_label:
                    raise
                try:
                    await linked.click(timeout=900, force=True, position={"x": 8, "y": 10})
                except Exception:
                    pass
                if not await loc.is_checked():
                    await linked.evaluate("e => e.click()")
    else:
        await loc.click(timeout=2500)

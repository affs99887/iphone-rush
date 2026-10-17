from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

CHINA = timezone(timedelta(hours=8))
PRODUCT_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo/mk2t4ch/a"
ACCOUNT_URL = "https://secure.www.apple.com.cn/shop/account/home"
STORES = ["浦东（国金中心）", "南京东路", "静安", "上海环贸 iapm", "香港广场", "环球港", "五角场", "七宝"]
STORE_ALIASES = {"浦东（国金中心）": ["浦东", "国金中心", "IFC"], "上海环贸 iapm": ["上海环贸 iapm", "环贸 iapm", "iapm"]}


@dataclass(frozen=True)
class ProductTarget:
    name: str = "iPhone Duo"
    color: str = "星光白色"
    path: str = "/shop/buy-iphone/iphone-duo"


DUO_TARGET = ProductTarget()
AIR_TARGET = ProductTarget("iPhone Air", "云白色", "/shop/buy-iphone/iphone-air")
AIR_TEST_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-air"
CMB_24 = "招商银行信用卡分期 24 期"
FULFILLMENTS = {"pickup": "门店自取", "delivery": "送货到家"}
BROWSER_MODES = {"auto": "自动（无头优先，被拦截时切换隐藏窗口）", "headless": "无头（隐身）", "hidden": "隐藏窗口"}
CHECKOUT_MODES = {"guest": "游客结账", "account": "Apple 账户结账"}
LAUNCH_AT = "2026-10-16 20:00:00"
PREWARM_FLOOR_MINUTES = 30
# Public examples only; enter real contact details in Settings before use.
# One contact phone per route distinguishes the two SMS verification codes.
DEFAULT_PHONES = {"pickup": "13800000000", "delivery": "13900000000"}
LEGACY_PHONE = "13800000000"


@dataclass
class Config:
    starts_at: str = "2026-10-16 20:00:00"
    first_name: str = "三"
    last_name: str = "张"
    phone: str = ""             # filled per route in __post_init__ (DEFAULT_PHONES)
    email: str = "fixture@example.invalid"
    stores: list[str] = field(default_factory=lambda: list(STORES))
    preferred_stores: list[str] = field(default_factory=lambda: ["静安", "上海环贸 iapm", "环球港"])
    refresh_seconds: float = 1.8
    stock_seconds: float = 6.0
    max_minutes: int = 45
    max_total: float = 30000.0
    payment: str = CMB_24
    # User decision 2026-10-15: submit the verified order and accept Apple's terms without asking.
    auto_submit: bool = True
    auto_consent: bool = True
    entry_tabs: int = 3
    guest_checkout: bool = True
    fulfillment: str = "pickup"
    shipping_province: str = "上海市"
    shipping_city: str = "上海市"
    shipping_district: str = "嘉定区"
    shipping_address: str = "示例路1号101室"
    shipping_postcode: str = "200000"   # Example only; enter the delivery postcode in Settings.
    browser_mode: str = "auto"
    prewarm_minutes: int = 30
    clock_sync: bool = True
    max_restarts: int = 3
    headless_fallback: bool = True
    block_resources: bool = False
    schema_version: int = 7

    def __post_init__(self):
        # An empty phone means "the preset for this route" (each route has its own default phone).
        if not self.phone:
            self.phone = DEFAULT_PHONES.get(self.fulfillment, LEGACY_PHONE)

    # §13.2: "guest" | "account". Backed by the ``guest_checkout`` flag the settings
    # dialog still edits, so the two can never disagree.
    @property
    def checkout_mode(self) -> str:
        return "guest" if self.guest_checkout else "account"

    @checkout_mode.setter
    def checkout_mode(self, value: str) -> None:
        if value not in CHECKOUT_MODES:
            raise ValueError("结账方式只能是游客或账户。")
        self.guest_checkout = value == "guest"

    @property
    def effective_prewarm_minutes(self) -> int:
        """§8.0: the shield handshake must be warm ≥ 30 min before T (0 disables pre-warming)."""
        minutes = int(self.prewarm_minutes)
        return 0 if minutes <= 0 else max(minutes, PREWARM_FLOOR_MINUTES)

    def start_time(self) -> datetime:
        return datetime.strptime(self.starts_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)

    def validate(self) -> None:
        self.start_time()
        if self.fulfillment not in FULFILLMENTS:
            raise ValueError("请选择门店自取或送货到家。")
        if self.browser_mode not in BROWSER_MODES:
            raise ValueError("浏览器模式只能是自动、无头或隐藏窗口。")
        if not 0 <= int(self.prewarm_minutes) <= 120:
            raise ValueError("预热提前量应为 0–120 分钟。")
        if not 0 <= int(self.max_restarts) <= 10:
            raise ValueError("自动重启上限应为 0–10 次。")
        if self.fulfillment == "delivery" and not all(s.strip() for s in (
            self.shipping_province, self.shipping_city, self.shipping_district, self.shipping_address
        )):
            raise ValueError("请填写完整的送货省市区及街道门牌。")
        if self.shipping_postcode and not re.fullmatch(r"\d{6}", self.shipping_postcode):
            raise ValueError("邮政编码应为 6 位数字；不确定时请留空。")
        if not self.first_name.strip() or not self.last_name.strip():
            raise ValueError("请填写取货人的姓和名。")
        if not re.fullmatch(r"1[3-9]\d{9}", self.phone):
            raise ValueError("请输入 11 位中国大陆手机号。")
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise ValueError("请检查电子邮箱。")
        if self.fulfillment == "pickup" and (not self.stores or any(s not in STORES for s in self.stores)):
            raise ValueError("请选择至少一家上海 Apple 直营店。")
        if len(self.stores) != len(set(self.stores)):
            raise ValueError("门店不能重复。")
        if any(s not in STORES for s in self.preferred_stores):
            raise ValueError("优先门店必须是上海直营店。")
        if not 1.0 <= self.refresh_seconds <= 30 or not 3 <= self.stock_seconds <= 120:
            raise ValueError("维护刷新间隔应为 1–30 秒，门店重试间隔应为 3–120 秒。")
        if not 1 <= self.max_minutes <= 180 or not 21499 <= self.max_total <= 100000:
            raise ValueError("请检查最长运行时间和订单金额上限。")
        if not 1 <= self.entry_tabs <= 4:
            raise ValueError("并发入口数量应为 1–4 个。")
        if self.payment not in (CMB_24, "微信支付", "支付宝", "手动选择"):
            raise ValueError("付款方式不受支持。")

    def to_dict(self):
        data = asdict(self)
        data["checkout_mode"] = self.checkout_mode
        return data

    @property
    def fulfillment_label(self):
        return FULFILLMENTS[self.fulfillment]

    @property
    def shipping_summary(self):
        parts = [self.shipping_province, self.shipping_city, self.shipping_district, self.shipping_address]
        return "".join(v for i, v in enumerate(parts) if not i or v != parts[i - 1])

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        version = data.get("schema_version", 1)
        if version < 2:
            data["email"] = data.get("email") or cls.email
            data["payment"] = CMB_24
        if version < 4:
            # 1.x settings predate the dispatcher: adopt the 2.0 browser/health defaults.
            for key in ("browser_mode", "prewarm_minutes", "clock_sync", "max_restarts", "headless_fallback", "block_resources"):
                data.pop(key, None)
        checkout_mode = data.pop("checkout_mode", None)
        if version < 5 and data.get("fulfillment") == "delivery" and data.get("phone", LEGACY_PHONE) == LEGACY_PHONE:
            # 2026-10-15: the delivery route gets its own phone so the two SMS codes can be told apart.
            data["phone"] = DEFAULT_PHONES["delivery"]
        if version < 6:
            # 2026-10-15: the user asked for automatic terms acceptance and automatic submission.
            data["auto_submit"] = True
            data["auto_consent"] = True
        if version < 7 and not data.get("shipping_postcode"):
            # Supply the default postcode when missing; an entered postcode is never replaced.
            data["shipping_postcode"] = "200000"
        data["schema_version"] = 7
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if checkout_mode in CHECKOUT_MODES and "guest_checkout" not in data:
            cfg.checkout_mode = checkout_mode
        cfg.prewarm_minutes, cfg.max_restarts = int(cfg.prewarm_minutes), int(cfg.max_restarts)
        cfg.clock_sync, cfg.headless_fallback = bool(cfg.clock_sync), bool(cfg.headless_fallback)
        cfg.block_resources = bool(cfg.block_resources)
        return cfg

    def copy(self) -> "Config":
        """Deep copy including mutable store lists (each operator owns its own)."""
        clone = Config.from_dict(self.to_dict())
        clone.stores, clone.preferred_stores = list(self.stores), list(self.preferred_stores)
        return clone


@dataclass
class MissionPlan:
    """What the dispatcher is going to do: when, which product, which policy, which operators."""
    starts_at: str = LAUNCH_AT
    target: ProductTarget = DUO_TARGET
    policy: str = "adaptive"
    operators: dict = field(default_factory=dict)
    browser_mode: str = "auto"
    clock_offset_ms: int | None = None
    prewarm_minutes: int = 10

    def start_time(self) -> datetime:
        return datetime.strptime(self.starts_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)

    def to_dict(self) -> dict:
        return {"starts_at": self.starts_at, "target": asdict(self.target), "policy": self.policy,
                "operators": {k: v.to_dict() for k, v in self.operators.items()},
                "browser_mode": self.browser_mode, "clock_offset_ms": self.clock_offset_ms,
                "prewarm_minutes": self.prewarm_minutes}


def normalize(text: str) -> str:
    return re.sub(r"[\s\u200b-\u200f\u2060\ufeff]", "", text).lower()


def trusted_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and (p.hostname == "apple.com.cn" or (p.hostname or "").endswith(".apple.com.cn"))


def mask(text: str, cfg: Config | None = None) -> str:
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
    text = re.sub(r"(?<!\w)\d{17}[0-9Xx](?!\w)", "[证件号码]", text)
    if cfg:
        for val in (cfg.last_name + cfg.first_name, cfg.email, cfg.shipping_summary, cfg.shipping_address):
            if val:
                text = text.replace(val, "[联系信息]")
    return text


def retry_delay(base: float, errors: int = 0, status: int = 200, retry_after: float = 0) -> float:
    # All entry tabs share the same server cooldown. 541 is Apple's bot-block
    # status; it gets the same shared 15–60 s backoff as 403/429.
    if status in (429, 403, 541):
        return max(15, retry_after, min(60, base * 2 ** min(errors + 2, 5)))
    if errors:
        return max(min(30, base * 2 ** min(errors, 4)), retry_after)
    return max(base, retry_after)


def parse_retry_after(value: str) -> float:
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def has_one_tb(text: str) -> bool:
    # Apple appends a footnote and price to the capacity label. Chinese letters
    # are Unicode word characters, so a \\w boundary rejects the actual label.
    return bool(re.search(r"(?<![\d.])1\s*tb(?![a-z0-9])", text, re.I))


def product_checks(selected: list[str], target: ProductTarget = DUO_TARGET) -> dict[str, bool]:
    values = [normalize(s) for s in selected]
    return {
        "color": any(s in (normalize(target.color), "白色") for s in values),
        "capacity": any(has_one_tb(s) for s in selected),
        "trade": any(re.search(r"不(?:折抵换购|折抵|抵扣|需要折抵)|无折抵", s) for s in values),
        "care": any("applecare+" in s and not re.search(r"不加|无需|不需要|无applecare|不购买", s) for s in values),
    }


def review_checks(text: str, cfg: Config, chosen_store: str, *, contact_verified=False, store_verified=False,
                  target: ProductTarget = DUO_TARGET) -> tuple[bool, list[str], float | None]:
    """Require a visible order summary, never use marketing copy as evidence."""
    n = normalize(text)
    failures = []
    if normalize(target.name) not in n or normalize(target.color) not in n or not has_one_tb(text):
        failures.append("订单摘要中的机型、白色或 1TB 无法确认")
    if re.search(r"不加applecare|未添加applecare|无需applecare", n) or "applecare+" not in n:
        failures.append("订单摘要中未确认 AppleCare+")
    # A selected trade-in line with a nonzero credit is a mismatch.
    if re.search(r"(?:折抵金额|折抵抵扣|trade-incredit)[:：]?(?:rmb|¥|￥)?[1-9]", n):
        failures.append("订单包含折抵抵扣")
    if cfg.fulfillment == "pickup":
        aliases = STORE_ALIASES.get(chosen_store, [chosen_store])
        if chosen_store not in cfg.stores or (not any(normalize(a) in n for a in aliases) and not store_verified) or "取货" not in n:
            failures.append("无法确认所选上海门店取货")
    else:
        if not re.search(r"送货|配送|收货地址|送达", n) or re.search(r"取货地点|取货联系人|取货方式[:：]?到店", n):
            failures.append("无法确认送货到家，或页面仍为门店取货")
        # Require the actual address in the final summary, never substitute a
        # remembered form value for a masked or changed shipping destination.
        # Live 10-14 review page: "上海 上海, 嘉定区 <street>" — municipalities are written without 市,
        # so the province/district compare without a trailing 省/市; the street must match verbatim.
        regions = [re.sub(r"[省市]$", "", normalize(v)) for v in (cfg.shipping_province, cfg.shipping_district)]
        if not all(r in n for r in regions) or normalize(cfg.shipping_address) not in n:
            failures.append("送货地址与设置不一致或无法完整核对")
    phone_ok = cfg.phone in n or (contact_verified and bool(re.search(r"[•*●]{4,}" + re.escape(cfg.phone[-4:]) + r"(?!\d)", n)))
    if normalize(cfg.last_name + cfg.first_name) not in n or not phone_ok:
        failures.append("联系人姓名或手机号无法确认")
    email_ok = normalize(cfg.email) in n
    if cfg.email and contact_verified:
        local, domain = cfg.email.lower().rsplit("@", 1)
        email_ok = email_ok or bool(re.search(re.escape(local[0]) + r"[•*●]+" + re.escape(local[-1] + "@" + domain), n))
    if cfg.email and not email_ok:
        failures.append("联系邮箱无法确认")
    if not re.search(r"(?:电子发票|发票类型|发票)[:：-]?个人", n):
        failures.append("个人发票无法确认")
    if cfg.payment == CMB_24 and not ("招商银行" in n and re.search(r"24(?:期|个月)", n) and "分期" in n):
        failures.append("招商银行信用卡分期 24 期无法确认")
    count = re.search(r"(?:数量|quantity)[:：x×]?(\d+)", n)
    if not count or int(count[1]) != 1:
        failures.append("无法确认 iPhone 数量为 1")
    match = re.search(r"(?:订单总计|订单总额|应付总额|总计|合计|ordertotal)\s*[:：]?\s*(?:RMB|CNY|¥|￥)?\s*([\d,]+(?:\.\d{1,2})?)", text, re.I)
    amount = float(match[1].replace(",", "")) if match else None
    if amount is None or not (14299 if target == AIR_TARGET else 21499) <= amount <= cfg.max_total:
        failures.append("订单总价未确认或超出金额上限")
    return not failures, failures, amount

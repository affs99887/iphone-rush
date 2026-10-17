"""Settings dialog with a grouped side navigation. Validates through Config.validate(); saving is the caller's job."""
from __future__ import annotations

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from duo.model import CMB_24, FULFILLMENTS, STORES, Config

from .widgets import button, divider, label

PAYMENTS = [CMB_24, "微信支付", "支付宝", "手动选择"]
BROWSER_MODES = [("auto", "自动（优先无头）"), ("headless", "始终无头"), ("hidden", "始终隐藏窗口")]
CHECKOUT_MODES = [("guest", "游客结账"), ("account", "登录 Apple 账户")]
PAGES = ["联系人与预约", "门店策略", "送货地址", "运行与浏览器"]
DATE_FORMAT = "yyyy-MM-dd HH:mm:ss"
CONFIG_FIELDS = set(Config.__dataclass_fields__)


def build_config(base: Config, values: dict) -> Config:
    """Copy `base`, apply `values`, drop keys the installed Config does not know (schema 3 / 4 / 4+checkout_mode)."""
    data = base.to_dict()
    data.update({k: v for k, v in values.items() if k in CONFIG_FIELDS})
    return Config(**{k: v for k, v in data.items() if k in CONFIG_FIELDS})


def checkout_mode_of(cfg: Config) -> str:
    """`Config.checkout_mode` when the engine has it, else derived from the legacy `guest_checkout` flag."""
    return getattr(cfg, "checkout_mode", "guest" if getattr(cfg, "guest_checkout", True) else "account")


def _form(parent: QWidget) -> QFormLayout:
    form = QFormLayout(parent)
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(12)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    return form


def _page(title: str, intro: str = "") -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    page.setObjectName("transparent")
    box = QVBoxLayout(page)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(12)
    box.addWidget(label(title, "appTitle"))
    if intro:
        box.addWidget(label(intro, "muted", wrap=True))
    return page, box


class SettingsDialog(QDialog):
    """Edits both operators' Config objects: shared contact/time/runtime, pickup stores, delivery address."""

    saved = Signal(dict)   # {"pickup": Config, "delivery": Config}

    def __init__(self, parent=None, configs: dict[str, Config] | None = None, locked: bool = False):
        super().__init__(parent)
        self.setWindowTitle("设置 · iPhone Duo 预约助手")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(900, 620)
        self.setMinimumSize(760, 520)
        self.configs: dict[str, Config] = {}
        self._build()
        self.load(configs or {op: Config(fulfillment=op) for op in FULFILLMENTS})
        self.set_locked(locked)

    # ---- layout --------------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QWidget()
        side.setObjectName("transparent")
        side.setFixedWidth(200)
        side_box = QVBoxLayout(side)
        side_box.setContentsMargins(16, 20, 12, 16)
        side_box.setSpacing(8)
        side_box.addWidget(label("设置", "title"))
        side_box.addWidget(label("保存后立即生效；运行中的任务继续使用启动时的设置。", "muted", wrap=True))
        self.nav = QListWidget()
        self.nav.setObjectName("sideNav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for title in PAGES:
            QListWidgetItem(title, self.nav)
        side_box.addWidget(self.nav, 1)
        body.addWidget(side)
        body.addWidget(divider(vertical=True))
        right = QWidget()
        right.setObjectName("settingsPane")
        right.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(28, 24, 28, 16)
        right_box.setSpacing(12)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._contact_page())
        self.pages.addWidget(self._store_page())
        self.pages.addWidget(self._address_page())
        self.pages.addWidget(self._runtime_page())
        right_box.addWidget(self.pages, 1)
        right_box.addWidget(divider())
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.error = label("", "error", wrap=True)
        self.error.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.error, 1)
        self.reset_button = button("恢复预设", tip="恢复为软件预设（张三 / 自取 13800000000 · 送货 13900000000 / 星光白色 1TB / 招行 24 期）")
        self.cancel_button = button("取消")
        self.save_button = button("保存", "primary")
        footer.addWidget(self.reset_button)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        right_box.addLayout(footer)
        body.addWidget(right, 1)
        root.addLayout(body)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.reset_button.clicked.connect(lambda: self.load({op: Config(fulfillment=op) for op in FULFILLMENTS}))
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.save)

    def _contact_page(self) -> QWidget:
        page, box = _page("联系人与预约", "两路共用姓名与邮箱，各用一个手机号以便区分验证码；取货时需携带与姓名一致的身份证原件。"
                                         "软件不保存验证码、卡号或证件号。")
        holder = QWidget()
        holder.setObjectName("transparent")
        form = _form(holder)
        names = QHBoxLayout()
        names.setSpacing(8)
        self.last_name = QLineEdit()
        self.last_name.setPlaceholderText("姓")
        self.last_name.setMaximumWidth(120)
        self.first_name = QLineEdit()
        self.first_name.setPlaceholderText("名")
        names.addWidget(self.last_name)
        names.addWidget(self.first_name, 1)
        form.addRow("姓名（姓 / 名）", names)
        # One phone per route so the two SMS verification codes can be told apart.
        self.phones: dict[str, QLineEdit] = {}
        for op, title in FULFILLMENTS.items():
            edit = QLineEdit()
            edit.setMaxLength(11)
            edit.setPlaceholderText(f"11 位手机号，接收{title}这一路的官网验证码")
            self.phones[op] = edit
            form.addRow(f"{title}手机号", edit)
        self.phone = self.phones["pickup"]          # kept for older callers / tests
        self.email = QLineEdit()
        self.email.setPlaceholderText("官网结账必填")
        form.addRow("电子邮箱", self.email)
        self.starts_at = QDateTimeEdit()
        self.starts_at.setDisplayFormat(DATE_FORMAT)
        self.starts_at.setCalendarPopup(True)
        self.starts_at.setMaximumWidth(240)
        form.addRow("开售时间（北京时间）", self.starts_at)
        self.payment = QComboBox()
        self.payment.addItems(PAYMENTS)
        self.payment.setMaximumWidth(300)
        form.addRow("付款方式", self.payment)
        submit_row = QHBoxLayout()
        submit_row.setSpacing(18)
        self.auto_submit = {}
        for op, title in FULFILLMENTS.items():
            cb = QCheckBox(title)
            self.auto_submit[op] = cb
            submit_row.addWidget(cb)
        submit_row.addStretch(1)
        form.addRow("自动提交已核对订单", submit_row)
        self.auto_consent = QCheckBox("自动同意官网条款（隐私知情同意书等），不再弹窗确认")
        form.addRow("官网条款", self.auto_consent)
        checkout_row = QHBoxLayout()
        checkout_row.setSpacing(10)
        self.checkout_mode: dict[str, QComboBox] = {}
        for op, title in FULFILLMENTS.items():
            caption = label(title, "caption")
            combo = QComboBox()
            for key, text in CHECKOUT_MODES:
                combo.addItem(text, key)
            combo.setMinimumWidth(150)
            self.checkout_mode[op] = combo
            checkout_row.addWidget(caption)
            checkout_row.addWidget(combo)
            checkout_row.addSpacing(8)
        checkout_row.addStretch(1)
        form.addRow("结账方式", checkout_row)
        box.addWidget(holder)
        box.addWidget(label("自动提交：订单核对通过且取得提交权后，不等人工确认直接点一次“立即下单”。关闭则在卡片上手动点“提交已核对订单”。\n"
                            "结账方式：游客结账两路购物袋彼此独立；选择“登录 Apple 账户”的一路会在预热时检查登录状态，未登录会提示“登录账户”"
                            "（同一账号两路的购物袋会同步，建议只有一路使用账户）。\n"
                            "发票固定为个人电子发票；短信验证码在软件弹窗输入；隐私知情同意书需你在弹窗确认。", "muted", wrap=True))
        box.addStretch(1)
        return page

    def _store_page(self) -> QWidget:
        page, box = _page("门店策略", "门店自取操作员在同一页面比较库存：有货立即选，不等待指定门店；多家同时有货时优先同组门店。")
        box.addWidget(label("允许取货的上海直营店", "sectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.store_boxes: dict[str, QCheckBox] = {}
        for i, name in enumerate(STORES):
            cb = QCheckBox(name)
            self.store_boxes[name] = cb
            grid.addWidget(cb, i // 3, i % 3)
        box.addLayout(grid)
        box.addSpacing(6)
        box.addWidget(label("多家同时有货时优先（同组不设固定顺序）", "sectionTitle"))
        prefs = QGridLayout()
        prefs.setHorizontalSpacing(24)
        prefs.setVerticalSpacing(8)
        self.preferred_boxes: dict[str, QCheckBox] = {}
        for i, name in enumerate(STORES):
            cb = QCheckBox(name)
            self.preferred_boxes[name] = cb
            prefs.addWidget(cb, i // 3, i % 3)
        box.addLayout(prefs)
        box.addWidget(label("取货时段选择页面首个可用时段；缺货时按“缺货后查询间隔”重查。", "muted", wrap=True))
        box.addStretch(1)
        return page

    def _address_page(self) -> QWidget:
        page, box = _page("送货地址", "送货到家操作员使用此地址；提交前会与官网订单摘要中的完整地址逐项核对。收件人为上面的联系人。")
        holder = QWidget()
        holder.setObjectName("transparent")
        form = _form(holder)
        self.address_fields: dict[str, QLineEdit] = {}
        for key, title in [("shipping_province", "省 / 直辖市"), ("shipping_city", "城市"), ("shipping_district", "区 / 县"),
                           ("shipping_address", "街道及门牌"), ("shipping_postcode", "邮编（官网要求时填写）")]:
            edit = QLineEdit()
            edit.setAccessibleName(title)
            if key != "shipping_address":
                edit.setMaximumWidth(300)
            self.address_fields[key] = edit
            form.addRow(title, edit)
        box.addWidget(holder)
        box.addWidget(label("地址只加密保存在本机；官网送货日期选择当前最早的免费时段。", "muted", wrap=True))
        box.addStretch(1)
        return page

    def _runtime_page(self) -> QWidget:
        page, box = _page("运行与浏览器", "两路共用这些运行参数。网络故障和限流时自动延长间隔；布防期间阻止自动睡眠。")
        holder = QWidget()
        holder.setObjectName("transparent")
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(12)
        left = QWidget()
        left.setObjectName("transparent")
        form = _form(left)
        self.refresh_seconds = QDoubleSpinBox()
        self.refresh_seconds.setRange(1, 30)
        self.refresh_seconds.setSingleStep(0.2)
        self.refresh_seconds.setSuffix(" 秒")
        form.addRow("维护页最短刷新间隔", self.refresh_seconds)
        self.entry_tabs = QSpinBox()
        self.entry_tabs.setRange(1, 4)
        self.entry_tabs.setSuffix(" 个")
        form.addRow("每路并发购买入口", self.entry_tabs)
        self.stock_seconds = QDoubleSpinBox()
        self.stock_seconds.setRange(3, 120)
        self.stock_seconds.setSuffix(" 秒")
        form.addRow("缺货后查询间隔", self.stock_seconds)
        self.max_minutes = QSpinBox()
        self.max_minutes.setRange(1, 180)
        self.max_minutes.setSuffix(" 分钟")
        form.addRow("最长运行时间", self.max_minutes)
        self.max_total = QDoubleSpinBox()
        self.max_total.setRange(21499, 100000)
        self.max_total.setDecimals(0)
        self.max_total.setPrefix("RMB ")
        form.addRow("单路订单金额上限", self.max_total)
        right = QWidget()
        right.setObjectName("transparent")
        form2 = _form(right)
        self.browser_mode = QComboBox()
        for key, title in BROWSER_MODES:
            self.browser_mode.addItem(title, key)
        form2.addRow("浏览器模式", self.browser_mode)
        self.prewarm_minutes = QSpinBox()
        self.prewarm_minutes.setRange(0, 120)
        self.prewarm_minutes.setSuffix(" 分钟")
        form2.addRow("开售前预热", self.prewarm_minutes)
        self.max_restarts = QSpinBox()
        self.max_restarts.setRange(0, 10)
        self.max_restarts.setSuffix(" 次")
        form2.addRow("浏览器自动重启上限", self.max_restarts)
        self.clock_sync = QCheckBox("按官网时钟校准开售时间")
        form2.addRow("时钟校准", self.clock_sync)
        self.headless_fallback = QCheckBox("被拦截时切换隐藏窗口")
        form2.addRow("自动降级", self.headless_fallback)
        grid.addWidget(left, 0, 0)
        grid.addWidget(right, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        box.addWidget(holder)
        box.addWidget(label("只使用软件自带 Chromium 与独立 profile，不会启动或读取你的 Edge。所有点击都读回结果；"
                            "任何重启都不会绕过防重复记录。", "muted", wrap=True))
        box.addStretch(1)
        return page

    # ---- data ------------------------------------------------------------------------------
    def load(self, configs: dict[str, Config]) -> None:
        self.configs = {op: configs.get(op) or Config(fulfillment=op) for op in FULFILLMENTS}
        shared = self.configs["pickup"]
        delivery = self.configs["delivery"]
        self.last_name.setText(shared.last_name)
        self.first_name.setText(shared.first_name)
        for op, edit in self.phones.items():
            edit.setText(self.configs[op].phone)
        self.email.setText(shared.email)
        self.starts_at.setDateTime(QDateTime.fromString(shared.starts_at, DATE_FORMAT))
        self.payment.setCurrentText(shared.payment)
        for op, cb in self.auto_submit.items():
            cb.setChecked(bool(self.configs[op].auto_submit))
        self.auto_consent.setChecked(bool(getattr(shared, "auto_consent", True)))
        for op, combo in self.checkout_mode.items():
            combo.setCurrentIndex(max(0, combo.findData(checkout_mode_of(self.configs[op]))))
        for name, cb in self.store_boxes.items():
            cb.setChecked(name in shared.stores)
        for name, cb in self.preferred_boxes.items():
            cb.setChecked(name in shared.preferred_stores)
        for key, edit in self.address_fields.items():
            edit.setText(getattr(delivery, key, ""))
        self.refresh_seconds.setValue(shared.refresh_seconds)
        self.entry_tabs.setValue(shared.entry_tabs)
        self.stock_seconds.setValue(shared.stock_seconds)
        self.max_minutes.setValue(shared.max_minutes)
        self.max_total.setValue(shared.max_total)
        mode = getattr(shared, "browser_mode", "auto")
        self.browser_mode.setCurrentIndex(max(0, self.browser_mode.findData(mode)))
        self.prewarm_minutes.setValue(int(getattr(shared, "prewarm_minutes", 10)))
        self.max_restarts.setValue(int(getattr(shared, "max_restarts", 3)))
        self.clock_sync.setChecked(bool(getattr(shared, "clock_sync", True)))
        self.headless_fallback.setChecked(bool(getattr(shared, "headless_fallback", True)))
        self.error.setText("")

    def _shared_values(self) -> dict:
        return {
            "last_name": self.last_name.text().strip(), "first_name": self.first_name.text().strip(),
            "email": self.email.text().strip(),
            "starts_at": self.starts_at.dateTime().toString(DATE_FORMAT), "payment": self.payment.currentText(),
            "refresh_seconds": round(self.refresh_seconds.value(), 2), "entry_tabs": self.entry_tabs.value(),
            "stock_seconds": round(self.stock_seconds.value(), 2), "max_minutes": self.max_minutes.value(),
            "max_total": float(self.max_total.value()),
            "browser_mode": self.browser_mode.currentData(), "prewarm_minutes": self.prewarm_minutes.value(),
            "max_restarts": self.max_restarts.value(), "clock_sync": self.clock_sync.isChecked(),
            "headless_fallback": self.headless_fallback.isChecked(),
            "auto_consent": self.auto_consent.isChecked(),
        }

    def collect(self) -> dict[str, Config]:
        """Build and validate both configs; raises ValueError with a Chinese message."""
        shared = self._shared_values()
        stores = [name for name, cb in self.store_boxes.items() if cb.isChecked()]
        preferred = [name for name, cb in self.preferred_boxes.items() if cb.isChecked()]
        address = {key: edit.text().strip() for key, edit in self.address_fields.items()}
        result = {}
        for op in FULFILLMENTS:
            values = dict(shared)
            mode = self.checkout_mode[op].currentData() or "guest"
            # `checkout_mode` is only applied when the dataclass has the field; `guest_checkout` keeps old engines in sync.
            values.update(fulfillment=op, auto_submit=self.auto_submit[op].isChecked(),
                          stores=list(stores), preferred_stores=list(preferred),
                          checkout_mode=mode, guest_checkout=(mode == "guest"),
                          phone=self.phones[op].text().strip())
            values.update(address)
            cfg = build_config(self.configs.get(op) or Config(fulfillment=op), values)
            try:
                cfg.validate()
            except ValueError as exc:
                raise ValueError(f"{FULFILLMENTS[op]}：{exc}") from exc
            result[op] = cfg
        return result

    def page_for_error(self, message: str) -> int:
        if any(k in message for k in ("送货", "邮政", "邮编")):
            return 2
        if "门店" in message:
            return 1
        if any(k in message for k in ("刷新", "入口", "运行时间", "金额", "付款", "间隔")):
            return 3
        return 0

    def save(self) -> None:
        try:
            configs = self.collect()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.nav.setCurrentRow(self.page_for_error(str(exc)))
            return
        self.error.setText("")
        self.configs = configs
        self.saved.emit(configs)
        self.accept()

    def set_locked(self, locked: bool) -> None:
        self.pages.setEnabled(not locked)
        self.save_button.setEnabled(not locked)
        self.reset_button.setEnabled(not locked)
        self.error.setText("任务进行中，设置已锁定；停止全部后可修改。" if locked else "")

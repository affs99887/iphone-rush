"""Operator: one checkout flow (pickup or delivery) in its own QThread + asyncio loop.

Evolved from the 1.5 ``Worker``. Safety invariants are unchanged: a durable journal
before any add/submit click, at most one submit ever, trial runs never submit, legal
checkboxes are never ticked automatically (the privacy consent only after the user
confirms it in the app), and every click is read back before the next decision.

2.0 (brief §7/§8/§12/§13/§14): a ``phase`` state machine with per-phase timing, a
heartbeat for the dispatcher's health monitor, soft/hard ``restart`` (same profile,
journal preserved), Akamai "shld" shield handling (wait for the handshake before the
first protected request; on 541 re-challenge in the same context with jittered
backoff instead of a permanent pause), pre-warming ≥ 30 min before T with a gentle
keep-alive, entry race at T-2 s on the calibrated clock, batched form filling, an
in-app ``login`` command with transient credentials and ``show-window``/``hide-window``.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import queue
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QThread, Signal

from . import events, stealth
from .browser import (Adapter, Decision, FinalButtonGuard, INVENTORY, bag_is_empty, blocked_page, is_final_label,
                      missing_page, perform)
from .model import (ACCOUNT_URL, AIR_TARGET, AIR_TEST_URL, CHINA, DUO_TARGET, PRODUCT_URL, Config, mask,
                    retry_delay, review_checks, trusted_url)
from .orders import per_order_quantity_cap
from .race import EntryRace

BAG_URL = "https://www.apple.com.cn/shop/bag"
ENTRY_LEAD = timedelta(seconds=2)
ENTRY_PARK = timedelta(seconds=30)
KEEPALIVE_S = 60.0
MAX_ADD_ATTEMPTS = 2
BLOCK_STATUSES = (403, 429, 541)
SUBMITTED_PHASES = ("submit-attempt", "complete")
# Akamai "shld" shield (brief §8.0)
SHIELD_SETTLE_S = 1.5          # after the PoW POST before the first protected request
SHIELD_WAIT_S = 8.0            # never wait longer than this for a handshake
SHIELD_WARM_GRACE_S = 1.0      # verify.js seen, no new challenge issued, cookie present → already warm
BAG_GRACE_S = 8.0              # the bag page renders its items asynchronously (/shop/bag/status)
SIGNIN_GRACE_S = 6.0           # the sign-in page renders the Apple ID widget before the guest block
CONFIRM_S = 8.0                # read-back window for a click on product / bag pages
CONFIRM_SLOW_S = 20.0          # checkout clicks: Apple's checkoutx calls took up to ~9 s live
CHECKOUT_QUIET_S = 0.4         # after the last checkout POST finished, before the next checkout click
CHECKOUT_QUIET_MAX_S = 25.0    # never wait longer than this for Apple to finish a request
# Live 10-15: a checkout POST whose finish event never arrived kept the delivery route "waiting for Apple"
# until the 20 s stall check paused it. Real checkoutx calls answered within ~12 s.
CHECKOUT_REQUEST_MAX_S = 18.0  # a single checkout call older than this is treated as lost
CHECKOUT_BUSY_MAX_S = 20.0     # never treat the page as "Apple is busy" for longer than this in a row
STALL_S = 20.0                 # no page change and no Apple activity for this long → ask the user
SHOT_KEEP_RUNS = 30            # screenshot history: newest run folders kept per route
SHOT_KEEP_MB = 600             # … and at most this much disk per route
# Live 10-15: the 立即下单 POST took 18.7 s (the review page stayed, its button re-enabled), then
# /shop/checkout/status showed "正在处理" for ~1 min before /shop/checkout/thankyou with the order number.
SUBMIT_WATCH_S = 180.0         # after the single submit click, keep watching (never clicking) this long
SHIELD_MAX_ATTEMPTS = 6        # reload + re-challenge cycles per page before asking the user
SHIELD_IGNORED = ("/shop/shld/", "/shop/bag/status", "/shop/beacon", "/shop/dc", "/shop/mdp/", "/shop/favoritesx",
                  "/shop/sba/", "/shop/shld/v3/verify.js")
BURST_KINDS = ("fill", "option", "select")
BURST_LIMIT = 8
# Live 10-14: clicking 添加到购物袋 130 ms after selecting AppleCare+ put the phone in the bag
# without AC+ — Apple attaches it asynchronously (/shop/api/applecare/data, /shop/updateSummary).
# Let the last configuration change age and the shop XHR go quiet before the add click.
ADD_SETTLE_S = 1.2
ADD_QUIET_S = 0.5
ADD_SETTLE_MAX_S = 3.0
STATE_SUGGESTIONS = {
    "review": "核对无误后点“提交已核对订单”；有疑问先点“查看页面”。",
    "default": "点“继续”让我重试；也可先点“查看页面”确认官网当前状态。",
}
ANALYTICS_HOSTS = ("doubleclick.net", "google-analytics.com", "googletagmanager.com", "facebook.net", "adobedtm.com",
                   "demdex.net", "omtrdc.net", "everesttech.net")


SCROLL_DIALOG_TO_END = r"""() => {
  const visible = e => !!e && !!e.getClientRects().length;
  const root = [...document.querySelectorAll('[role=dialog]')].find(visible) || document.body;
  let scrolled = 0;
  for (const el of root.querySelectorAll('*')) {
    const style = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 4 && /(auto|scroll)/.test(style.overflowY)) {
      el.scrollTop = el.scrollHeight;
      el.dispatchEvent(new Event('scroll', {bubbles: true}));
      scrolled++;
    }
  }
  return scrolled;
}"""


def jittered_backoff(attempt: int, base: float = 2.5, factor: float = 1.6, cap: float = 20.0) -> float:
    """Seconds to wait before the n-th (1-based) shield re-challenge: 2.5, 4, 6.4, … ≤ 20 s, +0–1.5 s jitter."""
    return min(cap, base * factor ** max(0, attempt - 1)) + random.uniform(0, 1.5)


class Operator(QThread):
    """Drives one Apple checkout. Commands arrive through ``send``; progress leaves as §4 event dicts."""

    event = Signal(dict)

    def __init__(self, data_dir: Path, fulfillment=None, coordinator=None, name: str | None = None,
                 browser_mode: str = "headless"):
        super().__init__()
        self.fulfillment = fulfillment
        self.name = name or fulfillment or "pickup"
        self.coordinator = coordinator
        self.data_dir, self.commands, self.cfg = Path(data_dir), queue.Queue(), Config()
        if fulfillment:
            self.cfg.fulfillment = fulfillment
        self.context = self.page = self.playwright = self.adapter = self.race = None
        self.active = self.paused = self.closed = self.trial = False
        self.deadline = self.target = None
        self.entry_url = PRODUCT_URL
        self.next_tick = 0.0
        self.refreshes = 0
        self.submitted = self.submit_once = False
        self.last_state = self.last_message = self.fingerprint = ""
        self.last_progress = time.monotonic()
        self.pending = self.last_result = self.reported_checks = None
        self.otp_waiting = False
        self.otp_kind = "sms"
        self.reported_store = ""
        self.review_token = self.submit_token = ""
        self.otp_request = ""
        self.profile_path = None
        self.capture_requested = False
        self.network_failure = None
        self.approval_request = None
        # 2.0 state
        self.phase = "idle"
        self.phase_entered = time.monotonic()
        self.run_started = None
        self.timeline_ms: dict[str, int] = {}
        self.attention_flag = self.blocked = self.failed = False
        self.heartbeat = time.monotonic()
        self.browser_mode = browser_mode if browser_mode in stealth.MODES else "headless"
        self.requested_mode = None
        self.clock_offset = timedelta(0)
        self.prewarmed = self.prewarming = self.prepared = False
        self.last_keepalive = 0.0
        self.parked = False
        self.entry_started = False
        self.cooldown_until = 0.0
        self.after_cooldown = ""
        self.consecutive_blocks = self.block_count = 0
        self.add_attempts = 0
        self.last_image = ""
        self.lanes = {"total": 0, "loading": 0, "requests": 0, "cooldown_s": 0.0, "winner": None}
        self.last_suggestion = ""
        self.restarts = 0
        self.loop = self.current = None
        self.last_hide = 0.0
        self.window_shown = False
        self.user_agent = ""
        self.shields: dict = {}
        self.shield_attempts = 0
        self.shield_log: list[dict] = []
        self.per_order_logged = False
        self.login_waiting = self.login_in_progress = False
        self.logged_in = None
        self.last_config_change = 0.0
        self.last_shop_activity = 0.0
        self.add_settle_started = 0.0
        self.bag_entered_at = 0.0
        self.signin_seen_at = 0.0
        self.inflight: dict[int, tuple[float, str]] = {}
        self.checkout_trace: list[dict] = []
        self.last_checkout_activity = 0.0
        self.quiet_wait_started = 0.0
        self.busy_since = 0.0
        self.busy_overrun_logged = False
        self.shot_dir: Path | None = None
        self.shot_seq = 0
        self.shot_task: asyncio.Task | None = None
        self.shot_stage = self.slow_shot = ""
        self.terms_attempts = 0
        self.submitted_at = 0.0
        # Trial-only switches (brief §10.5). Defaults keep the attended rehearsal behaviour:
        # the operator requests the SMS code and asks the user to confirm the privacy consent.
        # `--live-trial` turns SMS off (unattended run) and may auto-accept the consent.
        self.trial_sms_allowed = True
        self.trial_auto_consent = False

    # ------------------------------------------------------------------ paths / journal
    @property
    def journal_path(self):
        return self.session_dir / "purchase-state.json"

    @property
    def session_dir(self):
        return self.data_dir / "trial-headless" if self.trial else self.data_dir

    def read_journal(self):
        return json.loads(self.journal_path.read_text(encoding="utf-8")) if self.journal_path.exists() else {}

    def checkpoint(self, phase, **extra):
        self.session_dir.mkdir(parents=True, exist_ok=True)
        value = {"phase": phase, "trial": self.trial, "fulfillment": self.cfg.fulfillment,
                 "browser": "chromium", "browser_mode": self.browser_mode,
                 "time": datetime.now(CHINA).isoformat()}
        value.update(extra)
        temp = self.journal_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.journal_path)

    # ------------------------------------------------------------------ messaging
    def send(self, name, payload=None):
        self.commands.put((name, payload))

    def emit(self, value: dict):
        self.event.emit(value)

    def log(self, text, level="info"):
        self.emit(events.operator_log(self.name, mask(text, self.cfg), level))

    def set_phase(self, phase):
        if phase == self.phase or phase not in events.PHASES:
            return
        now = time.monotonic()
        previous = self.phase
        if previous not in ("idle",):
            spent = round((now - self.phase_entered) * 1000)
            self.timeline_ms[previous] = self.timeline_ms.get(previous, 0) + spent
            self.emit(events.operator_step(self.name, previous, True))
            self.emit(events.operator_timing(self.name, "阶段完成：" + previous, spent, phase=previous, phase_ms=spent))
        self.phase, self.phase_entered = phase, now
        if phase == "bag":
            self.bag_entered_at = now
        if self.run_started is not None:
            self.timeline_ms["total"] = round((now - self.run_started) * 1000)
        self.emit(events.operator_step(self.name, phase, False))

    def state(self, state, detail, phase=None):
        if phase:
            self.set_phase(phase)
        if (state, detail) != (self.last_state, self.last_message):
            self.last_state, self.last_message = state, detail
            self.emit(events.operator_state(self.name, state, mask(detail, self.cfg), self.phase))
            self.log(detail)

    def awake(self, enabled):
        if hasattr(ctypes, "windll"):
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if enabled else 0x80000000)

    def attention(self, message, suggestion=None, review=False):
        self.paused = self.attention_flag = True
        self.last_suggestion = suggestion or STATE_SUGGESTIONS["review" if review else "default"]
        self.state("待确认" if review else "请接管", message)
        self.emit(events.operator_attention(self.name, mask(message, self.cfg), self.last_suggestion, review))
        self.capture_requested = True

    def site_now(self) -> datetime:
        return datetime.now(CHINA) + self.clock_offset

    def status(self) -> dict:
        """Thread-tolerant snapshot for ``Dispatcher.status()`` (plain attribute reads)."""
        timeline = dict(self.timeline_ms)
        if self.run_started is not None and self.active:
            timeline["total"] = round((time.monotonic() - self.run_started) * 1000)
        return {"state": self.last_state, "detail": mask(self.last_message, self.cfg), "phase": self.phase,
                "running": self.active, "paused": self.paused, "trial": self.trial, "submitted": self.submitted,
                "review_ok": bool(self.last_result and self.last_result.get("valid")),
                "browser_mode": self.browser_mode, "lanes": dict(self.lanes), "refreshes": self.refreshes,
                "store": self.adapter.chosen_store if self.adapter else "",
                "checks": dict(self.reported_checks or {}), "last_image": self.last_image,
                "attention": self.attention_flag, "blocked": self.blocked, "failed": self.failed,
                "suggestion": self.last_suggestion, "restarts": self.restarts, "prewarmed": self.prewarmed,
                "heartbeat_age": round(time.monotonic() - self.heartbeat, 1), "add_attempts": self.add_attempts,
                "timeline_ms": timeline, "blocks": self.block_count, "shield_attempts": self.shield_attempts,
                "shield": self.shield_summary(), "checkout_mode": self.cfg.checkout_mode,
                "logged_in": self.logged_in, "window_shown": self.window_shown}

    # ------------------------------------------------------------------ dispatcher gate
    async def check_dispatcher(self):
        if not self.coordinator or self.trial or self.submitted:
            return False
        reason = self.coordinator.blocked_reason(self.cfg.fulfillment)
        if not reason:
            return False
        self.paused = True
        self.submit_once = self.otp_waiting = False
        self.otp_request = ""
        self.pending = None
        await self.stop_race()
        self.awake(False)
        self.state("已让出提交权", reason)
        self.emit(events.event(events.INTERNAL_COORDINATION, self.name, status=self.coordinator.status(), peer_blocked=True))
        return True

    def observe_limit(self, snap):
        if not self.per_order_logged:
            cap = per_order_quantity_cap(snap)
            if cap:
                self.per_order_logged = True
                self.log(f"官网提示“{cap}”属于每单数量限制，不是每人限购；两路照常各下一单。")
        if self.coordinator and not self.trial and self.coordinator.observe(snap):
            self.log("官网显示限购一单／一台，指派员已切换为两路竞速、总共一单。", "warn")
            self.emit(events.event(events.INTERNAL_COORDINATION, self.name, status=self.coordinator.status(), peer_blocked=False))

    # ------------------------------------------------------------------ shield (Akamai shld)
    def shield_for(self, page=None) -> dict:
        page = page or self.page
        record = self.shields.get(page)
        if record is None:
            record = {"nav_at": time.monotonic(), "verify": None, "get": None, "post": None, "cookie_at": None,
                      "first_protected": None, "proven": False, "waited": False, "url": getattr(page, "url", "")}
            if page is not None:
                self.shields[page] = record
            if len(self.shields) > 12:
                for stale in list(self.shields)[:-8]:
                    self.shields.pop(stale, None)
        return record

    def mark_navigation(self, page=None):
        page = page or self.page
        self.shields.pop(page, None)
        self.shield_for(page)

    def shield_summary(self) -> dict:
        record = self.shields.get(self.page) if self.page is not None else None
        if not record:
            return {}
        base = record["nav_at"]

        def rel(entry):
            return None if not entry else round((entry["t"] - base) * 1000)
        return {"verify_ms": rel(record["verify"]), "get": record["get"] and {"status": record["get"]["status"], "ms": rel(record["get"])},
                "post": record["post"] and {"status": record["post"]["status"], "ms": rel(record["post"])},
                "cookie_ms": None if record["cookie_at"] is None else round((record["cookie_at"] - base) * 1000),
                "first_protected": record["first_protected"] and {"status": record["first_protected"]["status"],
                                                                  "path": record["first_protected"]["path"],
                                                                  "ms": rel(record["first_protected"])},
                "proven": record["proven"], "attempts": self.shield_attempts}

    def record_response(self, response):
        url = urlsplit(response.url)
        if not (trusted_url(response.url) and url.path.startswith("/shop/")):
            return
        try:
            page = response.frame.page
        except Exception:
            page = self.page
        record = self.shield_for(page)
        now = time.monotonic()
        try:
            kind = response.request.resource_type
        except Exception:
            kind = ""
        if url.path.endswith("/shld/v3/verify.js"):
            record["verify"] = {"status": response.status, "t": now}
        elif url.path.startswith("/shop/shld/work/"):
            try:
                method = response.request.method
            except Exception:
                method = "GET"
            record["post" if method == "POST" else "get"] = {"status": response.status, "t": now}
        elif kind in ("fetch", "xhr") and not any(url.path.startswith(p) for p in SHIELD_IGNORED):
            self.last_shop_activity = now
            if record["first_protected"] is None:
                record["first_protected"] = {"status": response.status, "path": url.path, "t": now}
                summary = self.shield_summary() if page is self.page else {}
                self.shield_log.append({"time": datetime.now(CHINA).isoformat(), "url": record.get("url", ""),
                                        "event": "first-protected", **summary, "attempt": self.shield_attempts})
                self.write_shield_log()
            if response.status == 200:
                record["proven"] = True
                self.consecutive_blocks = 0
        if response.status in BLOCK_STATUSES:
            self.block_count += 1
            self.network_failure = {"status": response.status, "path": url.path, "time": now,
                                    "document": kind == "document"}
            self.emit(events.operator_network(self.name, response.status, url.path, self.block_count))

    @staticmethod
    def is_checkout_call(request) -> bool:
        try:
            url = urlsplit(request.url)
            return (trusted_url(request.url) and request.method == "POST"
                    and url.path.startswith(("/shop/checkoutx", "/shop/checkout/", "/shop/bagx")))
        except Exception:
            return False

    def on_request(self, request):
        if self.is_checkout_call(request):
            now = time.monotonic()
            path = urlsplit(request.url).path
            self.inflight[id(request)] = (now, path)
            self.last_checkout_activity = now
            self.checkout_trace = (self.checkout_trace + [{"path": path, "at": datetime.now(CHINA).strftime("%H:%M:%S.%f")[:-3]}])[-40:]

    def on_request_done(self, request):
        started = self.inflight.pop(id(request), None)
        if started is not None:
            now = time.monotonic()
            self.last_checkout_activity = now
            for item in reversed(self.checkout_trace):
                if item["path"] == started[1] and "ms" not in item:
                    item["ms"] = round((now - started[0]) * 1000)
                    break

    def prune_lost_requests(self) -> None:
        now = time.monotonic()
        for key, (started, path) in list(self.inflight.items()):
            if now - started > CHECKOUT_REQUEST_MAX_S:
                del self.inflight[key]
                self.log(f"官网请求 {path} 超过 {CHECKOUT_REQUEST_MAX_S:.0f} 秒没有返回，视为已结束，按当前页面继续。", "warn")

    def checkout_busy(self, window: float = CHECKOUT_QUIET_S * 2) -> bool:
        """Is Apple still processing a checkout call? Bounded: a lost request or an endless stream of calls
        can never keep a route waiting until the stall check pauses it (live 10-15)."""
        self.prune_lost_requests()
        now = time.monotonic()
        busy = bool(self.inflight) or now - self.last_checkout_activity < window
        if not busy:
            self.busy_since, self.busy_overrun_logged = 0.0, False
            return False
        self.busy_since = self.busy_since or now
        if now - self.busy_since > CHECKOUT_BUSY_MAX_S:
            if not self.busy_overrun_logged:
                self.busy_overrun_logged = True
                recent = "、".join(dict.fromkeys(i["path"] for i in self.checkout_trace[-6:]))
                self.log(f"官网连续 {CHECKOUT_BUSY_MAX_S:.0f} 秒都在处理请求（{recent}），不再等待，按当前页面继续。", "warn")
            return False
        return True

    def checkout_quiet(self) -> bool:
        """Live 10-14: clicking 继续 while Apple's previous checkoutx POST was in flight returned 503.
        Wait for the in-flight call to finish (bounded), then act."""
        if not self.checkout_busy(window=CHECKOUT_QUIET_S):
            self.quiet_wait_started = 0.0
            return True
        now = time.monotonic()
        if not self.quiet_wait_started:
            self.quiet_wait_started = now
        if now - self.quiet_wait_started >= CHECKOUT_QUIET_MAX_S:
            self.quiet_wait_started = 0.0
            self.inflight.clear()
            return True
        return False

    def awaiting_order_result(self) -> bool:
        return bool(self.submitted and self.submitted_at and time.monotonic() - self.submitted_at < SUBMIT_WATCH_S)

    def stalled(self) -> bool:
        """No page change and no Apple checkout activity for STALL_S: a genuine stall, not a slow answer."""
        return time.monotonic() - max(self.last_progress, self.last_checkout_activity) > STALL_S

    def write_shield_log(self):
        try:
            folder = self.session_dir / "diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "shield.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.shield_log[-1], ensure_ascii=False) + "\n")
        except (OSError, IndexError):
            pass

    async def shield_cookie_present(self) -> bool:
        try:
            cookies = await self.context.cookies("https://www.apple.com.cn/")
        except Exception:
            return False
        return any(c.get("name") == "shld_bt_ck" and "|" in (c.get("value") or "") for c in cookies)

    async def shield_ready(self) -> bool:
        """True once Apple's own verify.js handshake looks complete for the current page (brief §8.0)."""
        record = self.shield_for()
        if record["proven"]:
            return True
        now = time.monotonic()
        age = now - record["nav_at"]
        get_ok = record["get"] and record["get"]["status"] == 200
        post_ok = record["post"] and record["post"]["status"] == 200
        if get_ok and post_ok:
            if record["cookie_at"] is None and await self.shield_cookie_present():
                record["cookie_at"] = now
            if record["cookie_at"] is not None and now - record["post"]["t"] >= SHIELD_SETTLE_S:
                return True
        if record["verify"] is not None and record["get"] is None and now - record["verify"]["t"] >= SHIELD_WARM_GRACE_S:
            # Live 10-14 (warm profile): verify.js ran but issued no new challenge because the
            # shld_bt_ck cookie was still valid. Nothing to wait for — this is the pre-warmed
            # launch-day path and must not burn the 8 s cap.
            if record["cookie_at"] is None and await self.shield_cookie_present():
                record["cookie_at"] = now
            if record["cookie_at"] is not None:
                record["proven"] = True
                return True
        if record["verify"] is None and age > 2.0:
            return True          # this page does not load the shield script
        if age > SHIELD_WAIT_S:
            if not record["waited"]:
                record["waited"] = True
                self.log("安全盾握手超过 8 秒未完成，继续操作并观察官网响应。", "warn")
            return True
        return False

    async def shield_rechallenge(self):
        """Reload the current page in the same context so verify.js fetches a fresh challenge."""
        self.shield_attempts += 1
        self.shield_log.append({"time": datetime.now(CHINA).isoformat(), "event": "rechallenge",
                                "attempt": self.shield_attempts, **self.shield_summary()})
        self.write_shield_log()
        self.emit(events.event(events.INTERNAL_SHIELD, self.name, attempt=self.shield_attempts, summary=self.shield_summary()))
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            self.log("重新加载页面超时，继续读取当前页面。", "warn")
        self.mark_navigation()
        if self.adapter:
            self.adapter.last_clicked = ""
            if self.adapter.target.path in (self.page.url or "").lower():
                self.adapter.product_verified = False
        self.reported_checks = None
        self.pending = None
        self.fingerprint = ""
        self.next_tick = 0

    # ------------------------------------------------------------------ browser lifecycle
    async def ensure_browser(self):
        profile = self.session_dir / "chromium-profile"
        wanted = self.requested_mode or self.browser_mode
        if self.context and (self.profile_path != profile or wanted != self.browser_mode):
            await self.close_browser()
        if self.context:
            if self.page is None or self.page.is_closed():
                self.page = await self.context.new_page()
                self.hide_windows(force=True)
            return
        from playwright.async_api import async_playwright
        from .runtime import configure_runtime
        configure_runtime()
        if not self.playwright:
            self.playwright = await async_playwright().start()
        profile.mkdir(parents=True, exist_ok=True)
        if not self.user_agent:
            try:
                self.user_agent = stealth.detect_user_agent()
            except Exception:
                self.user_agent = ""
        options = stealth.launch_kwargs(wanted, user_agent=self.user_agent or None)
        hider = stealth.WindowHider().start(20) if wanted == "hidden" else None
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(str(profile), **options)
        finally:
            if hider:
                hider.stop()
        await self.context.add_init_script(stealth.INIT_SCRIPT)
        if self.cfg.block_resources:
            await self.context.route("**/*", self.route_filter)
        self.browser_mode, self.requested_mode = wanted, None
        self.profile_path = profile
        self.shields.clear()
        self.context.on("response", self.record_response)
        self.context.on("request", self.on_request)
        self.context.on("requestfinished", self.on_request_done)
        self.context.on("requestfailed", self.on_request_done)
        self.context.set_default_timeout(2500)
        self.context.set_default_navigation_timeout(8000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.hide_windows(force=True)
        self.emit(events.event(events.INTERNAL_BROWSER, self.name, ready=True, mode=self.browser_mode))
        self.log(f"已启动独立 Chromium（{stealth.describe(self.browser_mode)}模式）。")

    @staticmethod
    def should_block(url: str, resource_type: str) -> bool:
        """Opt-in bandwidth saver (§12.1) that never touches Apple's own scripts, XHR or documents (§8.0)."""
        host = (urlsplit(url).hostname or "").lower()
        apple = host.endswith(("apple.com.cn", "apple.com", "mzstatic.com", "cdn-apple.com", "aaplimg.com"))
        if not apple:
            return any(host.endswith(h) for h in ANALYTICS_HOSTS)
        return resource_type in ("image", "font", "media")

    async def route_filter(self, route, request):
        try:
            if self.should_block(request.url, request.resource_type):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    def hide_windows(self, force=False):
        if self.browser_mode != "hidden" or self.window_shown:
            return
        now = time.monotonic()
        if not force and now - self.last_hide < 2:
            return
        self.last_hide = now
        try:
            stealth.hide_browser_windows()
        except Exception:
            pass

    async def close_browser(self):
        await self.stop_race()
        context, self.context, self.page = self.context, None, None
        self.shields.clear()
        if context:
            try:
                await asyncio.wait_for(context.close(), 10)
            except Exception:
                self.log("关闭旧浏览器超时，继续以新会话启动。", "warn")

    async def navigate(self, url):
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=8000)
        except Exception:
            self.log("页面尚未加载完成，将检查当前进度。")
        self.mark_navigation()

    async def stop_race(self):
        if self.race:
            await self.race.stop(keep=self.page)
            self.race = None

    def race_event(self, kind, value):
        if kind == "log":
            self.log(value)
        elif kind == "lanes":
            self.lanes = dict(value)
            self.emit(events.operator_lanes(self.name, **value))
        elif kind == "blocked":
            self.network_failure = self.network_failure or {"status": value["status"], "path": urlsplit(value["path"]).path,
                                                            "time": time.monotonic(), "document": True}

    async def prepare_race(self):
        await self.stop_race()
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
        pages = [self.page]
        for _ in range(self.cfg.entry_tabs - 1):
            pages.append(await self.context.new_page())
        for page in pages:
            self.mark_navigation(page)
        self.hide_windows(force=True)
        self.race = EntryRace(pages, self.entry_url, self.cfg,
                              lambda: Adapter(self.cfg, self.adapter.target), self.race_event, self.observe_limit)
        self.entry_started = True
        self.set_phase("entry")

    async def prewarm(self):
        """Open the product page and the bag so the shield handshake and cookies exist long before T (§8.0/§8.3)."""
        if self.prewarming:
            return
        self.prewarming = True
        try:
            await self.ensure_browser()
            self.state("预热中", "提前打开商品页与购物袋，完成安全盾握手并建立官网会话。", "waiting" if self.active else None)
            await self.navigate(self.entry_url)
            waited = time.monotonic() + SHIELD_WAIT_S
            while time.monotonic() < waited and not await self.shield_ready():
                await asyncio.sleep(0.1)
            if self.network_failure:
                return
            await self.navigate(BAG_URL)
            await asyncio.sleep(0.8)
            if self.network_failure:
                return
            await self.navigate(self.entry_url)
            self.prewarmed = True
            self.last_keepalive = time.monotonic()
            self.emit(events.event(events.INTERNAL_PREWARMED, self.name, mode=self.browser_mode))
            self.log("预热完成：安全盾握手与购物袋会话均已建立。")
        finally:
            self.prewarming = False

    # ------------------------------------------------------------------ commands
    async def handle(self, command, payload):
        if isinstance(payload, Config):
            # The caller may edit its settings while the other operator is active.
            payload = payload.copy()
            if self.fulfillment and payload.fulfillment != self.fulfillment:
                raise ValueError("任务收货方式不匹配。")
        if command == "shutdown":
            self.closed, self.active = True, False
            await self.stop_race()
            return
        if command == "clock":
            self.clock_offset = timedelta(milliseconds=int(payload or 0))
            return
        if command == "mode":
            if payload in stealth.MODES:
                if self.context and payload != self.browser_mode:
                    await self.restart(hard=True, mode=payload, reason="切换浏览器模式")
                else:
                    self.browser_mode = payload
            return
        if command == "restart":
            payload = payload or {}
            await self.restart(bool(payload.get("hard")), payload.get("mode"), payload.get("reason", ""))
            return
        if command == "show-window":
            if not self.context:
                await self.ensure_browser()
            if self.browser_mode != "hidden":
                await self.restart(hard=True, mode="hidden", reason="显示浏览器窗口")
            self.window_shown = True
            shown = stealth.show_browser_windows()
            self.log(f"已显示本路浏览器窗口（{shown} 个）；处理完成后点“隐藏窗口”。")
            return
        if command == "hide-window":
            self.window_shown = False
            self.hide_windows(force=True)
            self.log("已重新隐藏本路浏览器窗口。")
            return
        if command == "login":
            credentials = payload if isinstance(payload, dict) else {}
            payload = None
            try:
                await self.login(credentials.get("apple_id", ""), credentials.get("password", ""))
            finally:
                credentials.clear()
            return
        if command == "login-check":
            await self.ensure_browser()
            await self.check_login()
            return
        if command == "show":
            if self.page and not self.page.is_closed():
                await self.capture_page(show=True)
            return
        if command == "trial-retry":
            if self.active:
                return
            self.trial = True
            empty = await self.handle("reset-record", payload)
            if empty is True:
                await self.handle("trial", payload)
            return
        if command == "consent":
            request = self.approval_request
            self.approval_request = None
            accept = payload.get("accept", True) if isinstance(payload, dict) else True
            token = payload.get("request") if isinstance(payload, dict) else payload
            if not request or token != request[0] or not self.active:
                return
            if not accept:
                self.attention("已暂不确认隐私知情同意书，本路保持暂停。", "准备好后在同意书窗口选择“同意”，或点“停止”。")
                return
            fresh = await self.snapshot()
            if not trusted_url(fresh["url"]) or self.fingerprint_of(fresh) != request[1]:
                self.attention("官网同意书页面已变化，请重新读取并确认。")
                return
            boxes = [n for n in fresh["nodes"] if n["kind"] == "checkbox" and not n["selected"]]
            # Apple's consent overlay only enables the checkbox once its text has been scrolled to the
            # end; the user has just read the same text in the app's dialog, so scroll it for them.
            try:
                await self.page.evaluate(SCROLL_DIALOG_TO_END)
                await asyncio.sleep(0.35)
            except Exception:
                pass
            for node in boxes:
                try:
                    await perform(self.page, Decision("select", "确认隐私知情同意书。", node))
                except Exception:
                    self.log("同意书复选框点击未返回，正在读回页面状态。", "warn")
            # Read back: only actually ticked boxes let the flow continue (the overlay can animate).
            deadline = time.monotonic() + 3.0
            remaining, latest = boxes, fresh
            while remaining and time.monotonic() < deadline:
                await asyncio.sleep(0.15)
                latest = await self.snapshot()
                selected = {n["selector"] for n in latest["nodes"] if n["kind"] == "checkbox" and n["selected"]}
                remaining = [n for n in boxes if n["selector"] not in selected]
            if remaining:
                # Re-ask against the page as it is now (some boxes may already be ticked).
                self.approval_request = (uuid.uuid4().hex, self.fingerprint_of(latest))
                self.attention("同意书复选框尚未勾选成功，请再确认一次。", "在同意书窗口再次选择“同意”；仍不行请点“查看页面”。")
                self.emit(events.operator_consent(self.name, self.approval_request[0], latest["url"].split("?")[0], latest["text"]))
                return
            self.paused = self.attention_flag = False
            self.adapter.last_clicked = ""
            self.next_tick = 0
            return
        if command == "check-dispatcher":
            if self.active:
                await self.check_dispatcher()
            return
        if command in ("prepare", "login-prepare", "product", "reset-record"):
            if self.active:
                return
            self.cfg = payload or self.cfg
            self.cfg.validate()
            journal = self.read_journal()
            if command == "reset-record" and journal and not self.trial and journal.get("browser") != "chromium":
                self.state("需要核对", "旧版浏览器记录已保留，不能用新的空购物袋清除旧记录。")
                self.emit(events.operator_finished(self.name, False, "legacy-record"))
                return False
            await self.ensure_browser()
            if command == "reset-record":
                empty = False
                if self.read_journal().get("phase") in SUBMITTED_PHASES:
                    self.state("需要核对", "已有订单提交记录，请在 Apple 订单列表核对；禁止自动清除提交记录。")
                else:
                    await self.navigate(BAG_URL)
                    snap = await self.snapshot()
                    if trusted_url(self.page.url) and bag_is_empty(snap):
                        self.journal_path.unlink(missing_ok=True)
                        self.add_attempts = 0
                        empty = True
                        self.state("准备就绪", "官网确认购物袋为空，已清除上次添加记录。")
                    else:
                        self.state("准备就绪", "请先在官网移除上次的商品，再检查空购物袋。")
                self.emit(events.operator_finished(self.name, empty, "reset-record"))
                return empty
            self.entry_url = AIR_TEST_URL if self.trial else PRODUCT_URL
            self.adapter = self.adapter or Adapter(self.cfg, AIR_TARGET if self.trial else DUO_TARGET)
            self.network_failure = None
            if self.cfg.checkout_mode == "account" or command == "login-prepare":
                logged_in = await self.check_login()
                if not logged_in:
                    self.prepared = True
                    self.emit(events.operator_finished(self.name, False, "login-required"))
                    return
            await self.prewarm()
            if self.network_failure:
                failure = self.network_failure
                self.state("准备就绪", f"预热时官网返回 HTTP {failure['status']}（{failure['path']}）；开始后会在同一会话重新握手。")
            else:
                self.state("准备就绪", "本路浏览器已预热" + ("，结账时优先使用官网游客入口。" if self.cfg.guest_checkout else "，将使用已登录的 Apple 账户结账。"))
            self.prepared = True
            self.emit(events.operator_finished(self.name, True, "prepared"))
            return
        if command in ("arm", "start", "now", "trial", "recover"):
            if self.active:
                return
            self.cfg = payload or self.cfg
            self.cfg.validate()
            self.network_failure = None
            if command != "recover":
                self.trial = command == "trial"
            journal = self.read_journal()
            if journal.get("fulfillment", "pickup") != self.cfg.fulfillment and journal:
                raise ValueError("订单记录属于另一种收货方式。")
            if (journal and command in ("arm", "start", "now") and not self.trial and journal.get("trial")
                    and journal.get("phase") not in SUBMITTED_PHASES):
                # 1.x wrote its trial add-attempts into the formal folder. A trial record is never a real
                # order: archive it (kept on disk) instead of blocking the formal run. Submitted phases
                # still block below, whatever their origin.
                archive = self.journal_path.with_name(
                    "purchase-state.legacy-trial-" + datetime.now(CHINA).strftime("%Y%m%d-%H%M%S") + ".json")
                self.journal_path.replace(archive)
                self.log("旧版试跑留在正式目录的加购记录（不是正式订单）已归档为 " + archive.name + "，正式任务照常开始。", "warn")
                journal = {}
            if journal and command == "trial" and self.trial:
                # Only the trial folder is touched here, never the formal journal: clear the test bag
                # left by an earlier trial (or emptied by a test order) and start fresh.
                await self.ensure_browser()
                if not await self.clear_trial_bag():
                    self.state("需要核对", "上次试跑留下的测试商品没能自动移除，请稍后再试，或在官网购物袋手动移除。")
                    self.emit(events.operator_finished(self.name, False, "journal"))
                    return
                journal = {}
            if journal and command != "recover":
                self.state("需要核对", "保留了上次添加或提交记录。请恢复购物袋；新任务前先移除商品并检查空购物袋。")
                self.emit(events.operator_finished(self.name, False, "journal"))
                return
            if journal and command == "recover" and not self.trial and journal.get("browser") != "chromium":
                self.state("需要核对", "旧版浏览器记录已保留，本版不会启动 Edge 或把旧订单转入新会话。")
                self.emit(events.operator_finished(self.name, False, "legacy-record"))
                return
            self.trial = bool(journal.get("trial")) if command == "recover" else command == "trial"
            await self.ensure_browser()
            self.adapter = Adapter(self.cfg, AIR_TARGET if self.trial else DUO_TARGET)
            self.active, self.paused = True, False
            self.attention_flag = self.blocked = self.failed = False
            self.submitted = journal.get("phase") in SUBMITTED_PHASES
            self.add_attempts = int(journal.get("attempts", 1 if journal.get("phase") == "add-attempt" else 0)) if journal else 0
            self.terms_attempts = 0
            self.submit_once = self.otp_waiting = False
            self.review_token = self.submit_token = self.otp_request = ""
            self.pending = self.last_result = self.reported_checks = None
            self.reported_store = self.fingerprint = ""
            self.cooldown_until, self.after_cooldown = 0.0, ""
            self.consecutive_blocks = self.shield_attempts = 0
            self.entry_started = self.parked = False
            self.timeline_ms = {}
            self.run_started = time.monotonic()
            self.phase_entered = self.run_started
            self.start_shot_history(command)
            self.target = self.cfg.start_time() if command in ("arm", "start") else self.site_now()
            self.deadline = max(self.target, self.site_now()) + timedelta(minutes=self.cfg.max_minutes)
            self.next_tick, self.last_progress = 0, time.monotonic()
            self.awake(True)
            self.entry_url = AIR_TEST_URL if self.trial else PRODUCT_URL
            self.emit(events.operator_run(self.name, self.trial, self.target.isoformat()))
            if command == "recover":
                self.adapter.added = self.adapter.product_verified = True
                self.set_phase("bag")
                await self.navigate(ACCOUNT_URL if self.submitted else BAG_URL)
                if self.submitted:
                    self.attention("上次已尝试提交订单，请在官网订单列表查看结果。不会再次提交。",
                                   "在官网订单列表确认结果；如未成功且购物袋为空，先“检查空购物袋”再重新开始。")
                else:
                    self.state("恢复进度", "已恢复本路购物袋，将重新核对收货方式。不会重复添加商品。", "bag")
            elif self.site_now() >= self.target - ENTRY_LEAD:
                await self.prepare_race()
                self.state("官网试跑" if self.trial else "入口竞速", ("入口已准备。官网试跑核对通过后自动提交测试订单。" if self.cfg.auto_submit
                                                                else "入口已准备。官网试跑到提交订单前停止。") if self.trial else "多个入口已就绪，开始竞速进入购买页。")
            else:
                self.state("已布防", "将按校准后的北京时间启动；提前预热会话，T-2 秒开始入口竞速。", "waiting")
            return
        if command == "pause" and self.active:
            self.paused = True
            self.submit_once = False
            await self.stop_race()
            self.state("已暂停", "已暂停操作；可通过“查看页面”检查当前页面。")
        elif command in ("resume", "submit") and self.active:
            if await self.check_dispatcher():
                return
            if self.submitted:
                self.attention("已有订单提交记录，请直接查看官网订单结果。", "在 Apple 订单列表核对；不会再次提交。")
                return
            if command == "submit":
                if not self.paused or not self.last_result or not self.last_result["valid"] or not self.review_token:
                    self.attention("请先完成当前订单核对，再提交。", "等待状态变为“待确认”且核对通过后再点提交。", review=bool(self.last_result))
                    return
                self.submit_token = self.review_token
            else:
                # Retain verified masked contacts only while resuming the same
                # reviewed order (e.g. after accepting its visible agreement).
                fresh = await self.snapshot()
                if not self.review_token or self.review_token_of(fresh) != self.review_token:
                    self.adapter.contact_verified = self.adapter.store_verified = False
                    self.adapter.address_verified = False
                    self.review_token = ""
            self.paused, self.last_progress, self.pending = False, time.monotonic(), None
            self.attention_flag = self.blocked = False
            self.cooldown_until, self.after_cooldown = 0.0, ""
            self.consecutive_blocks = 0
            self.adapter.last_clicked = ""
            self.submit_once = command == "submit"
            self.next_tick = 0
            if command == "resume" and not self.review_token and not self.adapter.added and not self.adapter.product_verified:
                await self.prepare_race()
            self.state("继续中", "重新核对当前页面。")
        elif command == "otp":
            if isinstance(payload, dict) and payload.get("request") == self.otp_request:
                if self.otp_kind == "apple-2fa":
                    await self.submit_2fa(payload.get("code"))
                else:
                    await self.submit_otp(payload.get("code"))
        elif command == "stop":
            self.active = self.paused = self.otp_waiting = False
            self.otp_request = self.submit_token = ""
            self.submit_once = False
            self.attention_flag = self.blocked = False
            await self.stop_race()
            self.awake(False)
            self.emit(events.operator_finished(self.name, False, "stopped"))
            self.state("已停止", "已停止任务，保留浏览器进度和防重复记录。", "idle")

    async def clear_trial_bag(self) -> bool:
        """Trial only: remove every item from the trial profile's bag, verify it is empty, drop the trial journal."""
        if not self.trial:
            return False
        self.state("清理试跑", "清空上次试跑留在测试购物袋里的商品。")
        try:
            await self.navigate(BAG_URL)
            for _ in range(8):
                await asyncio.sleep(1.0)
                snap = await self.snapshot()
                if bag_is_empty(snap):
                    self.journal_path.unlink(missing_ok=True)
                    self.add_attempts = 0
                    self.log("测试购物袋已清空，试跑记录已重置。")
                    return True
                remove = next((n for n in snap["nodes"] if n["kind"] in ("button", "a") and not n["disabled"]
                               and re.match(r"^(移除|删除|remove)", n["label"].strip(), re.I)), None)
                if not remove:
                    return False
                await self.page.locator(remove["selector"]).first.click(timeout=4000)
        except Exception:
            return False
        return False

    async def after_login(self):
        """Apple ID login succeeded during a run: go back to the bag and continue checkout (never re-add)."""
        if not self.active or self.submitted:
            return
        self.paused = self.attention_flag = False
        self.pending = None
        if self.adapter and self.adapter.added:
            self.adapter.chosen_store = ""
            self.adapter.contact_verified = self.adapter.store_verified = self.adapter.payment_verified = False
            self.adapter.delivery_verified = self.adapter.address_verified = False
            self.adapter.sms_requested = False
            self.adapter.last_clicked = ""
            await self.navigate(BAG_URL)
            self.state("恢复会话", "已登录 Apple 账户，回到购物袋继续结账；不会重复加购。", "bag")
        self.next_tick = 0

    async def restart(self, hard: bool, mode: str | None, reason: str):
        """Soft: new page/race. Hard: close the context and relaunch the same profile.

        Neither path bypasses the journal: after an add-attempt the bag is read back,
        after a submit-attempt nothing is ever submitted again.
        """
        self.restarts += 1
        self.pending = None
        self.network_failure = None
        self.cooldown_until, self.after_cooldown = 0.0, ""
        if mode and mode not in stealth.MODES:
            mode = None
        if mode and mode != self.browser_mode:
            hard, self.requested_mode = True, mode
        kind = "硬重启" if hard else "软重启"
        self.log(f"{kind}浏览器（{reason or '健康监控'}）；保留购物袋记录，不重复加购或提交。", "warn")
        await self.stop_race()
        if hard or not self.context:
            await self.close_browser()
            await self.ensure_browser()
        else:
            try:
                if self.page and not self.page.is_closed():
                    await self.page.close()
            except Exception:
                pass
            self.page = await self.context.new_page()
            self.hide_windows(force=True)
        self.fingerprint = ""
        self.last_progress = time.monotonic()
        if not self.active:
            if self.prepared:
                self.prewarmed = False
                await self.prewarm()
                self.state("准备就绪", f"浏览器已重启（{stealth.describe(self.browser_mode)}模式）。")
            return
        if self.blocked or (self.attention_flag and self.consecutive_blocks):
            self.paused = self.attention_flag = self.blocked = False
        self.consecutive_blocks = self.shield_attempts = 0
        self.next_tick = 0
        if self.submitted:
            await self.navigate(ACCOUNT_URL)
            self.attention("浏览器已重启；此前已尝试提交订单，请在官网订单列表核对。不会再次提交。",
                           "在 Apple 订单列表确认结果。")
        elif self.adapter.added:
            self.adapter.page_error_recovered = False
            self.adapter.chosen_store = ""
            self.adapter.contact_verified = self.adapter.store_verified = self.adapter.payment_verified = False
            self.adapter.delivery_verified = self.adapter.address_verified = False
            await self.navigate(BAG_URL)
            self.state("恢复进度", "浏览器已重启，正在读回购物袋；不会重复加购。", "bag")
        elif self.site_now() >= self.target - ENTRY_LEAD:
            await self.prepare_race()
            self.state("入口竞速", f"浏览器已重启（{stealth.describe(self.browser_mode)}模式），重新进入购买页。")
        else:
            self.prewarmed = False
            self.state("已布防", "浏览器已重启，继续等待开始时间。", "waiting")

    # ------------------------------------------------------------------ account login (§13.2 / §14.5)
    async def auth_frame(self):
        """Apple's sign-in widget lives in an iframe served by idmsa.apple.com(.cn)."""
        for frame in self.page.frames:
            if "idmsa.apple.com" in (frame.url or "") or frame.name == "aid-auth-widget-iFrame":
                return frame
        return None

    async def check_login(self) -> bool:
        await self.navigate(ACCOUNT_URL)
        await asyncio.sleep(0.8)
        url = (self.page.url or "").lower()
        self.logged_in = trusted_url(self.page.url) and "/signin" not in url and "/shop/account" in url
        if self.logged_in:
            self.state("账户已登录", "Apple 账户会话有效，可以账户身份结账。")
        else:
            self.attention("Apple 账户尚未登录（或会话已过期）。", "点击“登录账户”，在软件内输入 Apple ID 与密码；两步验证码也在软件内输入。")
            self.emit(events.event(events.INTERNAL_LOGIN_REQUIRED, self.name, checkout_mode=self.cfg.checkout_mode))
        return bool(self.logged_in)

    async def login(self, apple_id: str, password: str):
        """Type transient credentials into Apple's own sign-in page; nothing is persisted or logged."""
        self.login_in_progress = True
        try:
            if not apple_id or not password:
                self.attention("请提供 Apple ID 和密码后再登录。", "在登录对话框中填写后重试。")
                return
            await self.ensure_browser()
            self.state("登录中", "正在打开 Apple 账户登录页。")
            await self.navigate(ACCOUNT_URL)
            frame = None
            until = time.monotonic() + 15
            while time.monotonic() < until:
                if trusted_url(self.page.url) and "/signin" not in self.page.url.lower() and "/shop/account" in self.page.url.lower():
                    self.logged_in = True
                    self.state("账户已登录", "Apple 账户会话仍然有效，无需重新登录。")
                    return
                frame = await self.auth_frame()
                if frame and await frame.locator("input#account_name_text_field, input[type=text][autocomplete='username'], input[type=email]").count():
                    break
                await asyncio.sleep(0.2)
            if not frame:
                self.attention("未找到 Apple 登录表单，无法在软件内登录。", "点“查看页面”检查官网登录页，或稍后重试。")
                return
            account = frame.locator("input#account_name_text_field, input[type=text][autocomplete='username'], input[type=email]").first
            await account.fill(apple_id, timeout=5000)
            await account.press("Enter")
            password_box = frame.locator("input#password_text_field, input[type=password]").first
            try:
                await password_box.wait_for(state="visible", timeout=10000)
            except Exception:
                self.attention("官网没有显示密码框，可能 Apple ID 有误或页面已变化。", "点“查看页面”查看官网提示；不要反复尝试以免账户被锁定。")
                return
            await password_box.fill(password, timeout=5000)
            password = ""
            await password_box.press("Enter")
            outcome = await self.wait_login_outcome(frame)
            if outcome == "ok":
                self.logged_in = True
                self.state("账户已登录", "Apple 账户登录成功，会话保存在本路独立浏览器配置中。")
                await self.after_login()
            elif outcome == "2fa":
                self.paused = self.otp_waiting = self.login_waiting = True
                self.otp_kind = "apple-2fa"
                self.otp_request = uuid.uuid4().hex
                message = "Apple 账户两步验证：请输入发送到受信任设备或手机的 6 位验证码。"
                self.state("等待两步验证", message, None)
                self.emit(events.operator_otp(self.name, self.otp_request, self.cfg.phone, message, False, kind="apple-2fa"))
            elif outcome == "rejected":
                self.attention("Apple 拒绝了这次登录（账户或密码不正确）。", "请核对 Apple ID 与密码后再试一次；多次失败会导致账户锁定。")
            else:
                self.attention("登录结果不明确，请查看页面。", "点“查看页面”检查官网登录页状态。")
        except Exception:
            self.attention("登录过程中出现异常，未保存任何凭据。", "点“查看页面”检查官网状态后重试。")
        finally:
            password = apple_id = ""
            self.login_in_progress = False

    async def wait_login_outcome(self, frame, seconds: float = 20.0) -> str:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            url = (self.page.url or "").lower()
            if trusted_url(self.page.url) and "/signin" not in url and "/shop/account" in url:
                return "ok"
            try:
                frame = await self.auth_frame() or frame
                if frame is None:
                    await asyncio.sleep(0.25)
                    continue
                if await frame.locator("input.form-security-code-input, input[autocomplete='one-time-code']").count() >= 6:
                    return "2fa"
                text = await frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)")
                if re.search(r"不正确|incorrect|无法登录|已锁定|locked|无法验证|被禁用", text):
                    return "rejected"
                trust = frame.locator("button:has-text('信任'), button:has-text('Trust')")
                if await trust.count():
                    await trust.first.click(timeout=2000)
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return "unknown"

    async def submit_2fa(self, code):
        try:
            if not self.otp_waiting or self.otp_kind != "apple-2fa":
                return
            self.otp_waiting = False
            if not code or not re.fullmatch(r"\d{6}", code):
                self.attention("两步验证码应为 6 位数字。", "点“继续”重新输入。")
                return
            frame = await self.auth_frame()
            if frame is None:
                self.attention("两步验证页面已不存在，请检查登录状态。", "点“登录账户”重试。")
                return
            inputs = frame.locator("input.form-security-code-input, input[autocomplete='one-time-code']")
            count = await inputs.count()
            if count >= 6:
                await inputs.nth(0).click(timeout=2000)
                for i, digit in enumerate(code):
                    await inputs.nth(i).press(digit)
            else:
                await inputs.first.fill(code, timeout=2000)
            outcome = await self.wait_login_outcome(frame, 25)
            if outcome == "ok":
                self.logged_in = True
                self.paused = self.attention_flag = self.login_waiting = False
                self.otp_kind = "sms"
                self.state("账户已登录", "两步验证通过，Apple 账户已登录。")
                await self.after_login()
            elif outcome == "2fa":
                self.paused = self.otp_waiting = True
                self.otp_request = uuid.uuid4().hex
                message = "验证码不正确或已过期，请重新输入 6 位两步验证码。"
                self.state("等待两步验证", message)
                self.emit(events.operator_otp(self.name, self.otp_request, self.cfg.phone, message, True, kind="apple-2fa"))
            else:
                self.attention("两步验证后未进入账户页，请查看页面。", "点“查看页面”检查官网状态。")
        except Exception:
            self.attention("两步验证码填写未完成，请检查官网状态。", "点“登录账户”重试。")
        finally:
            code = None

    # ------------------------------------------------------------------ page reading
    async def snapshot(self):
        snap = await self.page.evaluate(INVENTORY)
        for info in snap.get("frames", []):
            if info["id"] == "aid-verify-widget-iFrame" and info["src"] == "https://id.apple.com/identity/verification":
                element = await self.page.query_selector(info["selector"])
                frame = await element.content_frame() if element else None
                if frame:
                    inner = await frame.evaluate(INVENTORY)
                    snap.update(phone_verification=True, verification_frame=info["selector"],
                                verification_text=inner["text"], verification_nodes=inner["nodes"])
        return snap

    @staticmethod
    def fingerprint_of(snap):
        return hashlib.sha256((snap["url"] + snap["text"] + str([(n["selected"], n["value"]) for n in snap["nodes"]])).encode()).hexdigest()

    @staticmethod
    def review_token_of(snap):
        return hashlib.sha256((snap.get("url", "") + "\n" + snap.get("summary", "")).encode()).hexdigest()

    @staticmethod
    def phase_for(url: str, kind: str, added: bool, target_path: str, current: str) -> str:
        u = (url or "").lower()
        if kind in ("complete", "payment"):
            return "done"
        if kind == "otp":
            return "verify"
        if kind == "review":
            return "review"
        if "/checkout" in u:
            stage = parse_qs(urlsplit(u).query).get("_s", [""])[0].lower()
            if stage.startswith("fulfillment"):
                return "fulfillment"
            if stage.startswith(("pickupcontact", "shipping", "contact", "address")):
                return "contact"
            if stage.startswith("billing"):
                return "payment"
            if stage.startswith("review"):
                return "review"
            return "checkout" if current in ("idle", "waiting", "entry", "configure", "add", "bag") else current
        if "/signin" in u:
            return "checkout"
        if "/bag" in u:
            return "bag"
        if target_path in u:
            return "add" if added else "configure"
        return current

    def note_confirmed(self, dec, snap):
        """Bookkeeping after a read-back confirmed a click."""
        if dec.kind == "store":
            self.adapter.chosen_store = dec.value
            self.adapter.store_verified = True
        if dec.kind in BURST_KINDS and self.adapter and self.adapter.target.path in (snap.get("url") or "").lower():
            self.last_config_change = time.monotonic()

    def add_settled(self) -> bool:
        """True once the product page had time to apply the last configuration change (ADD_SETTLE_*)."""
        now = time.monotonic()
        quiet = now - self.last_config_change >= ADD_SETTLE_S and now - self.last_shop_activity >= ADD_QUIET_S
        if quiet:
            self.add_settle_started = 0.0
            return True
        if not self.add_settle_started:
            self.add_settle_started = now
        if now - self.add_settle_started >= ADD_SETTLE_MAX_S:
            self.add_settle_started = 0.0
            return True
        return False

    @staticmethod
    def confirmed(dec, snap, before_url, before_fingerprint):
        same = next((n for n in snap["nodes"] if n["selector"] == dec.node["selector"]), None)
        if dec.kind in ("fill", "option"):
            return same is not None and same["value"] == dec.value
        if dec.kind in ("select", "store") and dec.node["kind"] in ("radio", "checkbox"):
            return same is not None and same["selected"]
        if dec.kind == "send-code":
            return snap.get("phone_verification") or not same
        return snap["url"] != before_url or Operator.fingerprint_of(snap) != before_fingerprint

    def check_pending(self, snap):
        if not self.pending:
            return False
        if missing_page(snap):
            # An error navigation is not confirmation that the click succeeded.
            self.pending = None
            return False
        dec, before_url, before_fingerprint, started = self.pending
        if self.confirmed(dec, snap, before_url, before_fingerprint):
            self.emit(events.operator_timing(self.name, dec.message, round((time.monotonic() - started) * 1000)))
            self.note_confirmed(dec, snap)
            self.pending = None
            return False
        if time.monotonic() - started >= self.confirm_limit(dec, snap):
            self.pending = None
            self.attention("官网尚未确认：" + dec.message + " 请检查页面后继续；没有重复点击。",
                           "点“查看页面”确认官网是否已完成该步骤，再点“继续”。")
        self.next_tick = time.monotonic() + 0.08
        return True

    @staticmethod
    def confirm_limit(dec, snap) -> float:
        """Seconds to wait for a click's read-back before asking the user (never a reason to click again).

        Live 10-14: confirming the delivery address overlay ("使用") took Apple ~8.7 s
        (POST /shop/checkoutx/fulfillment). Checkout clicks and busy pages get a longer window.
        """
        if snap.get("busy") or ("/checkout" in (snap.get("url") or "").lower() and dec.kind == "click"):
            return CONFIRM_SLOW_S
        return CONFIRM_S

    # ------------------------------------------------------------------ recovery matrix
    async def handle_network_failure(self):
        """Brief §8.0/§8.5: blocks are re-challenged or cooled down and re-read, never a permanent pause by default."""
        failure, self.network_failure = self.network_failure, None
        if not failure:
            return False
        self.consecutive_blocks += 1
        status, path = failure["status"], failure["path"]
        await self.stop_race()
        self.pending = None
        self.blocked = True
        if self.submitted:
            self.attention(f"提交后官网返回 HTTP {status}（{path}）。请在 Apple 订单列表核对结果；不会重复提交。",
                           "在官网订单列表确认是否已生成订单。")
            return True
        fetch_block = status == 541 and not failure.get("document")
        if fetch_block and not (self.adapter and self.adapter.added) and self.page and not self.page.is_closed():
            # Shield handshake lost the race against the first protected request: re-challenge in place.
            if self.shield_attempts >= SHIELD_MAX_ATTEMPTS:
                self.attention(f"安全盾已重新握手 {self.shield_attempts} 次仍返回 HTTP 541（{path}）。",
                               "稍等 1–2 分钟后点“继续”再试；若持续失败，在“更多”里重启浏览器或切换浏览器模式。")
                return True
            delay = jittered_backoff(self.shield_attempts + 1)
            self.after_cooldown = "rechallenge"
            self.state("安全盾重试", f"官网返回 HTTP 541（{path}），{delay:.1f} 秒后在同一会话重新加载并重新握手（第 {self.shield_attempts + 1} 次）。")
            self.cooldown_until = time.monotonic() + delay
            self.next_tick = time.monotonic() + 0.25
            return True
        delay = retry_delay(self.cfg.refresh_seconds, self.consecutive_blocks - 1, status)
        if self.consecutive_blocks >= 4:
            self.attention(f"官网连续 {self.consecutive_blocks} 次返回 HTTP {status}（{path}），本路可能已被限制。",
                           "等待一两分钟后点“继续”；仍失败可在“更多”里重启浏览器或切换浏览器模式。")
            return True
        if self.adapter and self.adapter.added:
            delay = min(delay, 20)
            self.after_cooldown = "verify-bag"
            self.state("等待冷却", f"官网返回 HTTP {status}（{path}），{delay:.0f} 秒后读回购物袋再决定；不重复加购或提交。")
        else:
            self.after_cooldown = "entry"
            self.state("入口冷却", f"官网返回 HTTP {status}（{path}），所有入口共同等待 {delay:.0f} 秒后重新进入。", "entry")
        self.cooldown_until = time.monotonic() + delay
        self.next_tick = time.monotonic() + 0.25
        return True

    async def finish_cooldown(self):
        action, self.after_cooldown = self.after_cooldown, ""
        self.cooldown_until = 0.0
        self.blocked = False
        if action == "rechallenge":
            await self.shield_rechallenge()
            self.state("安全盾重试", f"已重新加载页面（第 {self.shield_attempts} 次），等待握手后继续选配。")
        elif action == "verify-bag":
            self.adapter.page_error_recovered = False
            await self.navigate(BAG_URL)
            self.state("恢复会话", "冷却结束，读回购物袋核对商品。", "bag")
        elif action == "entry":
            await self.prepare_race()
            self.state("入口竞速", "冷却结束，重新进入购买页。")
        self.next_tick = 0

    async def handle_empty_bag(self):
        if self.submitted:
            self.attention("购物袋为空，但本路已有提交记录。请在 Apple 订单列表核对。", "在官网订单列表确认订单结果。")
        elif not self.adapter.added:
            self.state("购物袋为空", "尚未加购，返回购买页。", "entry")
            await self.prepare_race()
        elif self.add_attempts < MAX_ADD_ATTEMPTS:
            self.log(f"官网购物袋确实为空、没有本路商品；允许再加购一次（第 {self.add_attempts + 1} 次，上限 {MAX_ADD_ATTEMPTS} 次）。", "warn")
            self.adapter.added = self.adapter.product_verified = self.adapter.page_error_recovered = False
            self.adapter.last_clicked = ""
            self.state("重新加购", "购物袋为空，重新进入购买页再加购一次。", "entry")
            await self.prepare_race()
        else:
            self.attention(f"购物袋仍为空，已加购 {self.add_attempts} 次（上限 {MAX_ADD_ATTEMPTS} 次），不再自动加购。",
                           "点“查看页面”确认官网状态；若要重试，请先停止并“检查空购物袋”后重新开始。")

    # ------------------------------------------------------------------ waiting / pre-warm / keep-alive
    async def wait_phase(self, now: datetime):
        remaining = (self.target - now).total_seconds()
        prewarm_minutes = self.cfg.effective_prewarm_minutes
        if prewarm_minutes and not self.prewarmed and not self.prewarming and remaining <= prewarm_minutes * 60:
            await self.prewarm()
            if self.network_failure:
                return
        if self.prewarmed and remaining > ENTRY_PARK.total_seconds() + 15 and time.monotonic() - self.last_keepalive >= KEEPALIVE_S:
            # Gentle keep-alive: one reload per minute keeps cookies, TLS and the shield warm (§12.4).
            self.last_keepalive = time.monotonic()
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            self.mark_navigation()
        if remaining <= ENTRY_PARK.total_seconds() and not self.parked:
            self.parked = True
            if self.entry_url not in (self.page.url or ""):
                await self.navigate(self.entry_url)
            self.state("等待开始", "已停在入口页，T-2 秒开始入口竞速。", "waiting")
        else:
            self.state("等待开始", "将在 " + self.target.strftime("%m 月 %d 日 %H:%M:%S") + "（官网校准时间）启动；T-2 秒开始竞速。", "waiting")
        self.next_tick = time.monotonic() + min(0.2, max(0.02, remaining - ENTRY_LEAD.total_seconds()))

    # ------------------------------------------------------------------ main step
    async def tick(self):
        if await self.check_dispatcher():
            return
        if self.network_failure and await self.handle_network_failure():
            return
        if self.cooldown_until:
            if time.monotonic() < self.cooldown_until:
                self.next_tick = time.monotonic() + 0.25
                return
            await self.finish_cooldown()
            return
        now = self.site_now()
        if now < self.target - ENTRY_LEAD:
            await self.wait_phase(now)
            return
        if now > self.deadline:
            await self.handle("stop", None)
            self.state("已结束", "已达到最长运行时间，保留当前进度。")
            return
        if not self.entry_started and not self.race and not (self.adapter.added or self.submitted):
            await self.prepare_race()
            self.state("入口竞速", "开始竞速进入购买页。")
        if self.race:
            result = await self.race.poll()
            self.refreshes = self.race.requests
            self.hide_windows()
            if not result:
                self.next_tick = time.monotonic() + 0.05
                return
            self.page, evidence, lane = result
            self.race = None
            self.lanes = dict(self.lanes, loading=0, winner=lane)
            self.emit(events.operator_lanes(self.name, **self.lanes))
            if evidence.get("manual"):
                self.attention(evidence["manual"], "点“查看页面”确认官网内容；处理后点“继续”。")
                return
            self.consecutive_blocks = 0
            self.state("入口已打开", f"入口 {lane} 最先可用，已停止其他入口，开始配置。", "configure")
        if self.page.is_closed():
            self.attention("任务浏览器页面已关闭。", "点“更多 → 重启浏览器”，或停止后“恢复购物袋”。")
            return
        if not trusted_url(self.page.url):
            self.attention("页面已转到登录或银行付款页面，请在浏览器完成后继续。")
            return
        try:
            snap = await self.snapshot()
        except Exception:
            self.next_tick = time.monotonic() + 0.15
            if time.monotonic() - self.last_progress > 20:
                self.attention("页面持续无法读取，已保留结账进度，请检查官网。", "点“更多 → 重启浏览器”后继续；购物袋记录会保留。")
            return
        self.observe_limit(snap)
        # Tell the page logic whether Apple is still processing a checkout call (see checkout_quiet()).
        snap["checkout_busy"] = self.checkout_busy()
        if self.network_failure and await self.handle_network_failure():
            return
        if blocked_page(snap) and not self.network_failure:
            self.network_failure = {"status": 541, "path": urlsplit(snap["url"]).path, "time": time.monotonic(), "document": True}
            if await self.handle_network_failure():
                return
        if await self.check_dispatcher():
            return
        fingerprint = self.fingerprint_of(snap)
        if fingerprint != self.fingerprint:
            self.fingerprint, self.last_progress = fingerprint, time.monotonic()
        if self.check_pending(snap):
            return
        dec = self.adapter.plan(snap)
        if (dec.kind not in ("review", "complete", "payment", "manual", "otp", "wait", "unknown", "refresh",
                             "login-required", "stock", "empty-bag", "recover-bag")
                and dec.node is not None and is_final_label(dec.node.get("label", ""))):
            # Defensive: only review() may press an order-placing button, and only through the submit gate.
            dec = Decision("review", "核对订单摘要。", dec.node)
        stage = self.stage_of(snap["url"])
        if stage != self.shot_stage:
            self.shot_stage = stage
            self.archive("page-" + stage, snap, dec.message)
        elif dec.kind in ("wait", "unknown") and time.monotonic() - self.last_progress > 5 and self.slow_shot != self.fingerprint:
            self.slow_shot = self.fingerprint
            self.archive("slow-" + dec.kind, snap, dec.message)
        if "/signin" in snap["url"].lower():
            self.signin_seen_at = self.signin_seen_at or time.monotonic()
        else:
            self.signin_seen_at = 0.0
        self.set_phase(self.phase_for(snap["url"], dec.kind, self.adapter.added, self.adapter.target.path, self.phase))
        checks = self.adapter.selected_checks(snap)
        if self.adapter.target.path in snap["url"] and checks != self.reported_checks:
            self.emit(events.operator_checks(self.name, checks))
            self.reported_checks = checks
        if self.adapter.chosen_store and self.adapter.chosen_store != self.reported_store:
            self.reported_store = self.adapter.chosen_store
            self.emit(events.operator_store(self.name, self.reported_store))
        if dec.kind == "refresh":
            if self.adapter.added or self.submitted:
                self.attention("购物袋或结账页暂时维护，已保留当前位置，禁止刷新重建订单。", "稍候点“继续”重新读取页面。")
            else:
                await self.prepare_race()
                self.next_tick = time.monotonic() + self.cfg.refresh_seconds
        elif dec.kind in ("complete", "payment"):
            self.active = False
            self.awake(False)
            order = self.adapter.order_number
            self.checkpoint("complete", order=order)
            if self.coordinator and not self.trial:
                self.coordinator.outcome(self.cfg.fulfillment, "confirmed")
                self.emit(events.event(events.INTERNAL_COORDINATION, self.name, status=self.coordinator.status(), peer_blocked=False))
            message = dec.message
            if self.trial and dec.kind == "complete":
                message = (f"试跑测试订单已创建（订单号 {order}）" if order else "试跑测试订单已创建") + \
                          "，不付款 30 分钟后官网自动取消，无需处理。"
            self.state(("下单成功" if dec.kind == "complete" else "请付款"), message, "done")
            self.archive("order-result", snap, message)
            self.last_suggestion = ("试跑到此结束。" if self.trial else
                                    "请立即在手机“掌上生活” App 扫描官网页面上的二维码付款（点“查看页面”可看到二维码）。")
            self.emit(events.operator_attention(self.name, mask(message, self.cfg), self.last_suggestion, False))
            self.emit(events.operator_finished(self.name, True, dec.kind))
        elif dec.kind == "login-required":
            if self.cfg.guest_checkout and self.signin_seen_at and time.monotonic() - self.signin_seen_at < SIGNIN_GRACE_S:
                # Live 10-14: the guest block ("以游客身份继续") renders after the Apple ID widget.
                self.state("检查页面", "等待登录页加载游客结账入口。", "checkout")
                self.next_tick = time.monotonic() + 0.15
                return
            self.attention(dec.message, "点击“登录账户”在软件内登录；若官网已强制登录，指派员会切换为同账号顺序下单。")
            self.emit(events.event(events.INTERNAL_LOGIN_REQUIRED, self.name, checkout_mode=self.cfg.checkout_mode))
        elif dec.kind == "manual":
            if ("/bag" in snap["url"].lower() and not self.submitted and self.bag_entered_at
                    and time.monotonic() - self.bag_entered_at < BAG_GRACE_S):
                # Live 10-14: the bag document arrives ~0.7 s before /shop/bag/status fills in the items.
                # A mismatch inside the grace period is "not rendered yet", not "wrong bag".
                self.state("检查页面", "等待购物袋加载完成后再核对商品。", "bag")
                self.next_tick = time.monotonic() + 0.15
                return
            if trusted_url(snap["url"]) and re.search(r"需要你签署|知情同意书", snap["text"]):
                self.approval_request = (uuid.uuid4().hex, self.fingerprint_of(snap))
                if self.cfg.auto_consent or (self.trial and self.trial_auto_consent):
                    # The user asked for this (10-05 for trials, 10-15 for every run): no dialog.
                    self.state("确认同意书", "按你的设置自动同意 Apple 隐私知情同意书。")
                    await self.handle("consent", {"request": self.approval_request[0], "accept": True})
                    return
                self.attention(dec.message, "在弹出的同意书窗口阅读后选择“同意”，我会继续。")
                self.emit(events.operator_consent(self.name, self.approval_request[0], snap["url"].split("?")[0], snap["text"]))
            else:
                self.attention(dec.message)
        elif dec.kind == "recover-bag":
            if self.submitted:
                self.attention("已尝试提交订单，请查看 Apple 订单列表，不能恢复结账后重新提交。", "在官网订单列表确认结果。")
            else:
                self.adapter.chosen_store = ""
                self.adapter.contact_verified = self.adapter.store_verified = self.adapter.payment_verified = False
                self.adapter.delivery_verified = self.adapter.address_verified = False
                self.adapter.sms_requested = False
                self.pending = None
                self.state("恢复会话", dec.message, "bag")
                if dec.node:
                    await perform(self.page, dec)
                else:
                    self.adapter.page_error_recovered = True
                    await self.navigate(BAG_URL)
        elif dec.kind == "empty-bag":
            await self.handle_empty_bag()
        elif dec.kind == "otp":
            self.paused = self.otp_waiting = True
            self.otp_kind = "sms"
            self.pending = None
            error = "验证码不正确" in snap.get("verification_text", "")
            message = "官网提示验证码不正确，请重新输入。" if error else "请输入尾号 " + self.cfg.phone[-4:] + " 收到的 6 位 Apple 验证码。"
            self.state("等待验证码", message, "verify")
            self.otp_request = uuid.uuid4().hex
            self.emit(events.operator_otp(self.name, self.otp_request, self.cfg.phone, message, error, kind="sms"))
        elif dec.kind == "review":
            await self.review(snap, dec)
        elif dec.kind == "stock":
            self.state("等待库存", dec.message)
            if not self.adapter.chosen_store:
                retry = self.adapter.find(snap, r"^(重新查询|重新搜索|再次检查|刷新供货情况)$", ["button", "submit"])
                if retry:
                    await perform(self.page, Decision("click", "重新查询库存。", retry))
                elif not any(n["value"] and n["kind"] in ("tel", "email") for n in snap["nodes"]):
                    self.adapter.last_clicked = ""
                    try:
                        await self.page.reload(wait_until="domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    self.mark_navigation()
            self.next_tick = time.monotonic() + self.cfg.stock_seconds
        elif dec.kind in ("unknown", "wait"):
            if self.submitted and self.submitted_at:
                # After the submit click: watch quietly for the order page; never prompt for a slow answer.
                self.state("等待下单结果", dec.message if "订单" in dec.message else "官网正在处理订单，等待订单结果页。", "submit")
                self.next_tick = time.monotonic() + 0.3
                if not self.awaiting_order_result():
                    self.attention("提交后 3 分钟仍未看到订单结果页，请查看 Apple 订单列表或邮件；不会重复提交。",
                                   "在官网订单列表或邮件里确认是否已生成订单。")
                return
            self.state("检查页面", dec.message)
            self.next_tick = time.monotonic() + 0.08
            if self.stalled():
                self.attention("页面没有出现可确认的下一步，请检查官网后继续。", "点“查看页面”看看官网停在哪一步；必要时“重启浏览器”。")
        else:
            if not await self.shield_ready():
                self.state("等待安全盾", "等待官网安全盾握手完成后再操作，避免触发 541。")
                self.next_tick = time.monotonic() + 0.1
                return
            if "/checkout" in (snap.get("url") or "").lower() and not self.checkout_quiet():
                self.state("等待官网处理", "等待官网处理完上一步请求再操作，避免并发请求被拒（503）。")
                self.next_tick = time.monotonic() + 0.1
                return
            if dec.kind == "add":
                if self.adapter.added:
                    self.attention("已有添加记录，请核对购物袋。", "点“更多 → 恢复购物袋”核对现有商品。")
                    return
                if not self.add_settled():
                    self.state("等待配置生效", "AppleCare+ 等配置正在同步到官网购物模型，稍候再加购。")
                    self.next_tick = time.monotonic() + 0.1
                    return
                self.add_attempts += 1
                self.checkpoint("add-attempt", attempts=self.add_attempts)
                self.adapter.added = True
                self.set_phase("add")
            if dec.kind == "send-code":
                if self.trial and not self.trial_sms_allowed:
                    # Unattended trial ends here: everything up to the phone verification is proven,
                    # and nobody is waiting for the SMS.
                    self.active, self.paused = False, True
                    self.awake(False)
                    self.set_phase("verify")
                    message = "试跑已到达手机验证步骤（联系人已填写），按设置不发送短信、不继续。"
                    self.state("试跑到达验证前", message)
                    self.last_suggestion = "试跑结束。请在官网购物袋移除测试商品，然后“检查空购物袋”。"
                    self.capture_requested = True
                    self.emit(events.operator_attention(self.name, message, self.last_suggestion, False))
                    self.emit(events.operator_finished(self.name, True, "trial-contact"))
                    return
                self.adapter.sms_requested = True
            self.archive(dec.kind, snap, dec.message)
            await self.act(dec, snap)

    async def act(self, dec, snap):
        """Perform one decision, then keep going within the same tick while the next decisions are
        plain field/option/radio changes on the same page (§12.2/§12.3 batching)."""
        self.state("自动操作", dec.message)
        self.pending = (dec, snap["url"], self.fingerprint_of(snap), time.monotonic())
        try:
            await perform(self.page, dec)
            self.adapter.last_clicked = dec.node["selector"]
            self.adapter.performed(dec)
        except FinalButtonGuard:
            # The button under this selector became 立即下单: never place an order outside review().
            self.pending = None
            self.log("目标按钮已变为“立即下单”，拒绝点击；改为先核对订单摘要。", "warn")
            self.next_tick = time.monotonic() + 0.3
            return
        except Exception:
            self.log("操作返回超时，检查页面确认结果；不重复点击。")
            self.next_tick = 0
            return
        self.next_tick = 0
        if dec.kind not in BURST_KINDS:
            return
        for _ in range(BURST_LIMIT):
            fresh = await self.quick_confirm()
            if fresh is None:
                return
            following = self.adapter.plan(fresh)
            if following.kind not in BURST_KINDS or fresh["url"] != snap["url"] or following.node is None:
                return
            if following.node["kind"] == "checkbox" and re.search(r"同意|条款|协议|agree|terms", following.node["label"], re.I):
                return
            self.state("自动操作", following.message)
            self.pending = (following, fresh["url"], self.fingerprint_of(fresh), time.monotonic())
            try:
                await perform(self.page, following)
                self.adapter.last_clicked = following.node["selector"]
                self.adapter.performed(following)
            except FinalButtonGuard:
                self.pending = None
                self.log("目标按钮已变为“立即下单”，拒绝点击；改为先核对订单摘要。", "warn")
                return
            except Exception:
                self.log("操作返回超时，检查页面确认结果；不重复点击。")
                return

    async def quick_confirm(self, budget: float = 0.6):
        """Poll briefly until the pending decision is confirmed; returns the fresh snapshot or None."""
        until = time.monotonic() + budget
        while time.monotonic() < until and self.pending:
            try:
                fresh = await self.snapshot()
            except Exception:
                return None
            if missing_page(fresh) or fresh["url"] != self.pending[1]:
                return None
            dec, before_url, before_fingerprint, started = self.pending
            if self.confirmed(dec, fresh, before_url, before_fingerprint):
                self.emit(events.operator_timing(self.name, dec.message, round((time.monotonic() - started) * 1000)))
                self.note_confirmed(dec, fresh)
                self.pending = None
                self.observe_limit(fresh)
                return fresh
            await asyncio.sleep(0.03)
        return None

    async def submit_otp(self, code):
        try:
            if await self.check_dispatcher():
                return
            if not self.active or not self.otp_waiting:
                return
            self.otp_waiting = False
            if not code:
                self.attention("已暂停验证码输入，点继续可再次输入，或直接在官网验证。", "收到短信后点“继续”，我会重新弹出输入框。")
                return
            if not re.fullmatch(r"\d{6}", code):
                self.attention("验证码应为 6 位数字，请点继续重新输入。", "点“继续”重新输入 6 位数字验证码。")
                return
            if not trusted_url(self.page.url):
                self.attention("页面来源变化，未填写验证码。")
                return
            snap = await self.snapshot()
            if not snap.get("phone_verification"):
                self.paused = self.attention_flag = False
                return
            nodes = [n for n in snap["verification_nodes"] if n["kind"] == "tel" and re.search(r"位数\s*[1-6]", n["label"])]
            if len(nodes) != 6:
                self.attention("官网验证码控件已改变，请直接在官网验证。", "点“查看页面”查看官网验证框。")
                return
            frame = self.page.frame_locator(snap["verification_frame"])
            for n in nodes:
                await frame.locator(n["selector"]).fill("")
            for n, digit in zip(nodes, code):
                await frame.locator(n["selector"]).press(digit)
            self.adapter.otp_submitted_at = time.monotonic()
            # Wait for a visible outcome, not an arbitrary pause before showing
            # another prompt. The frame disappearing confirms acceptance.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                fresh = await self.snapshot()
                if not fresh.get("phone_verification") or "验证码不正确" in fresh.get("verification_text", ""):
                    break
                await asyncio.sleep(0.1)
            self.paused = self.attention_flag = False
            self.next_tick = 0
            self.state("正在验证", "验证码已发送给 Apple，检查官网结果。", "verify")
        except Exception:
            self.attention("验证码填写未完成，请检查官网状态后继续。")
        finally:
            code = None

    async def review(self, snap, dec):
        self.observe_limit(snap)
        self.set_phase("review")
        if await self.check_dispatcher():
            return
        if self.submitted:
            if self.awaiting_order_result():
                # Apple is still answering the single 立即下单 click; its button may look clickable again.
                self.state("等待下单结果", "已点击一次“立即下单”，官网正在处理，等待订单结果页；不会重复点击。", "submit")
                self.next_tick = time.monotonic() + 0.3
                return
            self.attention("已尝试提交订单，请检查官网订单列表；不会重复提交。", "在官网订单列表确认结果。")
            return
        token = self.review_token_of(snap)
        if self.submit_once and token != self.submit_token:
            self.submit_once = False
            self.last_result = None
            self.review_token = ""
            self.emit(events.operator_review(self.name, False, ["订单摘要已变化"], None, self.adapter.chosen_store, ""))
            self.attention("订单摘要已变化，请重新核对后再提交。", review=True)
            return
        valid, problems, amount = review_checks(snap.get("summary", ""), self.cfg, self.adapter.chosen_store,
                                               contact_verified=self.adapter.contact_verified,
                                               store_verified=self.adapter.store_verified,
                                               target=AIR_TARGET if self.trial else DUO_TARGET)
        destination = "上海 " + self.adapter.chosen_store if self.cfg.fulfillment == "pickup" else self.cfg.shipping_summary
        self.last_result = {"valid": valid, "problems": problems, "total": amount, "store": self.adapter.chosen_store}
        self.emit(events.operator_review(self.name, valid, problems, amount, self.adapter.chosen_store, destination))
        self.review_token = token if valid else ""
        if self.trial and not self.submit_once and not (valid and self.cfg.auto_submit):
            # Trial without automatic submission: stop on the review page. The user may create a real test
            # order (iPhone Air, unpaid, Apple cancels it after 30 min) with "提交已核对订单".
            self.capture_requested = True
            if not valid:
                self.active, self.paused = False, True
                self.awake(False)
                message = "已到核对页，但核验未通过：" + "；".join(problems)
                self.state("试跑核对失败", message)
                self.last_suggestion = "试跑结束。下次试跑会自动清空测试购物袋。"
                self.emit(events.operator_attention(self.name, mask(message, self.cfg), self.last_suggestion, False))
                self.emit(events.operator_finished(self.name, False, "trial"))
                return
            self.paused = self.attention_flag = True
            message = ("试跑核对通过：商品、收货方式、联系人、个人发票、招商银行 24 期均正确，总价 RMB "
                       f"{amount:,.2f}。可点“提交已核对订单”创建一张测试订单（不付款会被官网自动取消），或点“停止”。")
            self.state("试跑核对通过", message)
            self.last_suggestion = "要验证下单就点“提交已核对订单”；不需要就点“停止”。"
            self.emit(events.operator_attention(self.name, mask(message, self.cfg), self.last_suggestion, True))
            return
        if not valid:
            self.attention("提交前核对未通过：" + "；".join(problems), "点“查看页面”核对官网摘要；修正后点“继续”。", review=True)
            return
        terms = [n for n in snap["nodes"] if n["kind"] == "checkbox" and not n["selected"] and not n.get("disabled")
                 and re.search(r"同意|条款|协议|agree|terms", n["label"], re.I)]
        if terms:
            if self.cfg.auto_consent and self.terms_attempts < 2:
                # User decision 2026-10-15: accept Apple's terms automatically; read back on the next pass.
                self.terms_attempts += 1
                for node in terms:
                    try:
                        await perform(self.page, Decision("select", "勾选官网条款。", node))
                    except Exception:
                        self.log("条款复选框点击未返回，正在读回页面状态。", "warn")
                self.log("按你的设置自动勾选官网条款：" + "；".join(n["label"][:40] for n in terms))
                self.next_tick = time.monotonic() + 0.3
                return
            self.attention("官网条款复选框未能自动勾选，请在官网确认后点继续。", "点“查看页面”看看是哪一项条款。", review=True)
            return
        if not (self.cfg.auto_submit or self.submit_once):
            where = "上海 " + self.adapter.chosen_store if self.cfg.fulfillment == "pickup" else "送货到家，地址已核对"
            self.attention(f"订单已核对：{where}，RMB {amount:,.2f}，{self.cfg.payment}。", review=True)
            return
        # A trial (test) order never touches the shared submit gate: it must not count as the real
        # order, block the other route, or block the formal run on launch day. Its journal lives in
        # the separate trial-headless folder.
        if self.coordinator and not self.trial:
            acquired, reason = self.coordinator.claim(self.cfg.fulfillment)
            if not acquired:
                await self.check_dispatcher()
                return
            self.emit(events.event(events.INTERNAL_COORDINATION, self.name, status=self.coordinator.status(), peer_blocked=False))
        self.checkpoint("submit-attempt")
        how = "按你的点击" if self.submit_once else "自动"
        self.submit_once, self.submitted = False, True
        self.submitted_at = time.monotonic()
        self.state("正在提交", (f"试跑核对通过，{how}提交测试订单一次（不付款会被官网自动取消）。" if self.trial
                               else f"订单全部关键项目核对通过（RMB {amount:,.2f}），{how}提交一次。"), "submit")
        try:
            await perform(self.page, dec)
        except Exception:
            if self.coordinator and not self.trial:
                self.coordinator.outcome(self.cfg.fulfillment, "unknown")
                self.emit(events.event(events.INTERNAL_COORDINATION, self.name, status=self.coordinator.status(), peer_blocked=False))
            self.attention("提交结果不明确，请查看 Apple 订单列表。重启也不会自动重复提交。", "在官网订单列表确认是否已生成订单。")
        self.next_tick = time.monotonic() + 0.2

    # ------------------------------------------------------------------ screenshot history
    def start_shot_history(self, reason: str = "run") -> None:
        """One folder per run under diagnostics/history (every screenshot of the run is kept there)."""
        root = self.session_dir / "diagnostics" / "history"
        stamp = datetime.now(CHINA).strftime("%Y%m%d-%H%M%S")
        self.shot_dir = root / f"{stamp}-{'trial' if self.trial else reason}"
        self.shot_seq, self.shot_stage = 0, ""
        try:
            runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name) if root.exists() else []
            sizes = {p: sum(f.stat().st_size for f in p.iterdir() if f.is_file()) for p in runs}
            total = sum(sizes.values())
            for old in runs[:-1] if runs else []:
                if len(runs) <= SHOT_KEEP_RUNS and total <= SHOT_KEEP_MB * 1024 * 1024:
                    break
                for item in old.iterdir():
                    item.unlink(missing_ok=True)
                old.rmdir()
                runs.remove(old)
                total -= sizes[old]
        except OSError:
            pass

    def scrubbed(self, value):
        if isinstance(value, str):
            return mask(value, self.cfg)
        if isinstance(value, list):
            return [self.scrubbed(x) for x in value]
        if isinstance(value, dict):
            return {k: self.scrubbed(v) for k, v in value.items()}
        return value

    @staticmethod
    def stage_of(url: str) -> str:
        parsed = urlsplit(url or "")
        stage = parse_qs(parsed.query).get("_s", [""])[0]
        return parsed.path.rstrip("/").rsplit("/", 1)[-1] + ("-" + stage if stage else "")

    def shot_allowed(self, snap: dict | None) -> bool:
        # Never record an authentication form, a typed Apple ID or a partially entered code.
        return not (snap and (snap.get("phone_verification") or snap.get("auth"))) and not (
            self.login_waiting or self.login_in_progress)

    def shot_path(self, reason: str, suffix: str) -> Path | None:
        if self.shot_dir is None:
            self.start_shot_history("manual")
        try:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        self.shot_seq += 1
        tag = re.sub(r"[^\w.-]+", "_", reason, flags=re.ASCII)[:48]
        return self.shot_dir / f"{self.shot_seq:03d}-{datetime.now(CHINA).strftime('%H%M%S')}-{self.phase}-{tag}.{suffix}"

    def shot_record(self, snap: dict, message: str = "") -> dict:
        parsed = urlsplit(snap.get("url") or "")
        stage = parse_qs(parsed.query).get("_s", [""])[0]
        record = {k: v for k, v in snap.items() if k not in ("verification_text", "verification_nodes")}
        record["url"] = parsed.scheme + "://" + parsed.netloc + parsed.path + ("?_s=" + stage if stage else "")
        record.update(time=datetime.now(CHINA).isoformat(timespec="milliseconds"), phase=self.phase,
                      state=self.last_state, detail=self.last_message, decision=message,
                      network_failure=self.network_failure, shield=self.shield_summary(),
                      checkout_inflight=[p for _, p in self.inflight.values()],
                      checkout_trace=self.checkout_trace[-12:])
        return self.scrubbed(record)

    def archive(self, reason: str, snap: dict | None = None, message: str = "") -> None:
        """Keep a viewport screenshot + page record in the run's history without slowing the operator."""
        if not self.page or self.page.is_closed() or not self.shot_allowed(snap):
            return
        if self.shot_task is not None and not self.shot_task.done():
            return
        path = self.shot_path(reason, "jpg")
        if path is None:
            return
        record = self.shot_record(snap, message) if snap else None
        page = self.page

        async def take():
            try:
                if record is not None:
                    path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
                await page.screenshot(path=str(path), type="jpeg", quality=70, timeout=4000)
            except Exception:
                pass
        try:
            self.shot_task = asyncio.ensure_future(take())
        except RuntimeError:
            self.shot_task = None

    async def capture_page(self, show=False, reason="view"):
        """Local, read-only evidence of the actual browser, excluding OTP values."""
        if not self.page or self.page.is_closed():
            return
        snap = await self.snapshot()
        if not self.shot_allowed(snap):
            # (The guest sign-in page also hosts Apple's empty ID widget plus the consent overlay;
            # that page carries no secrets and its capture is what lets us diagnose it.)
            self.emit(events.operator_page(self.name, "", "当前是验证或登录页面，请使用本路软件对话框。", ""))
            return
        record = self.shot_record(snap, reason)
        folder = self.session_dir / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "latest-page.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        screenshot = folder / "latest-page.png"
        try:
            await self.page.screenshot(path=str(screenshot), full_page=True, timeout=5000)
            self.last_image = str(screenshot)
        except Exception:
            self.last_image = ""
        kept = self.shot_path(reason, "png")
        if kept is not None:
            try:
                kept.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
                if self.last_image:
                    kept.write_bytes(screenshot.read_bytes())
            except OSError:
                pass
        if show:
            self.emit(events.operator_page(self.name, record["url"], mask(record["url"] + "\n" + snap["text"], self.cfg), self.last_image))

    # ------------------------------------------------------------------ loop
    def interrupt(self):
        """Thread-safe: cancel whatever await the loop is stuck in (used by soft restart)."""
        task, loop = self.current, self.loop
        if task and loop and not task.done():
            loop.call_soon_threadsafe(task.cancel)
            return True
        return False

    async def _run(self, coro):
        self.current = asyncio.ensure_future(coro)
        try:
            return await self.current
        except asyncio.CancelledError:
            self.pending = None
            self.log("已中断一次卡住的浏览器操作。", "warn")
            return None
        finally:
            self.current = None

    async def main_loop(self):
        self.loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                self.heartbeat = time.monotonic()
                try:
                    command, payload = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                if command:
                    try:
                        await self._run(self.handle(command, payload))
                    except Exception as exc:
                        self.active = False
                        self.failed = True
                        await self.stop_race()
                        self.awake(False)
                        self.state("需要处理", "浏览器准备或读取记录失败，请检查软件文件、网络和软件目录。")
                        self.emit(events.operator_error(self.name, mask(f"{command}: {type(exc).__name__}: {exc}"[:200], self.cfg)))
                        self.emit(events.operator_finished(self.name, False, "error"))
                        self.capture_requested = True
                    finally:
                        payload = None
                if self.active and not self.paused and time.monotonic() >= self.next_tick:
                    try:
                        await self._run(self.tick())
                    except Exception as exc:
                        self.log(f"步骤异常 {type(exc).__name__}: {exc}"[:240], "warn")
                        self.attention("当前步骤出现异常，请检查官网进度后继续。")
                if self.capture_requested:
                    self.capture_requested = False
                    try:
                        await self.capture_page(reason="attention")
                    except Exception:
                        self.log("当前页面截图暂不可用，已保留任务进度。")
                if self.context and self.browser_mode == "hidden":
                    self.hide_windows()
                await asyncio.sleep(0.03)
        finally:
            self.awake(False)
            await self.stop_race()
            if self.context:
                try:
                    await asyncio.wait_for(self.context.close(), 10)
                except Exception:
                    pass
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass

    def run(self):
        asyncio.run(self.main_loop())


Worker = Operator

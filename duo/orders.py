"""One shared, durable submit gate for the two independent checkout operators."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .model import CHINA, FULFILLMENTS, trusted_url

POLICIES = {
    "adaptive": "争取两单；识别限购后只下一单",
    "single": "始终只下一单；两路竞速",
}
MODES = {"parallel": "两路并行", "sequential-account": "同账号顺序下单"}

OWNER = r"(?:每(?:人|位(?:顾客|客户|用户|消费者)|名(?:顾客|客户|用户)|个(?:账户|帐户|账号|帐号|apple\s*id))|同一(?:人|顾客|客户|用户|账户|帐户|账号|手机号|身份证))"
LIMIT = r"(?:仅(?:可|能|限)?|只(?:可|能)?|最多|限购|限订购|限购买|限下单|限制|限)"
PURCHASE = r"(?:(?:最多|只能|仅可|可以|可|能|限购|限订购|购买|订购|购入|下单|下|提交|创建|购|数量|为)\s*){0,4}"
QUANTITY = r"(?:1|一)(?![\d十百千万])\s*(?:笔订单|个订单|部手机|台手机|台|部|件|单)"
# "每单 / 单次 / 每笔 / 每次 / 一次(性) / 每个订单" qualify the quantity per order, not per person (§13.1).
PER_ORDER = r"(?:每(?:单|笔(?:订单)?|次|个订单|张订单|张|一单)|单次|单笔|一次(?:性)?(?:下单|购买|订购|最多|只能)?)"
PER_PERSON = re.compile(OWNER + r"\s*(?:限购数量\s*[:：]?\s*|" + LIMIT + r"\s*" + PURCHASE + r"\s*(?:数量\s*)?(?:为\s*)?[:：]?\s*)" + QUANTITY, re.I)
PER_PERSON_EN = re.compile(r"limit(?:ed)?\s*(?:of|to)?\s*1(?!\d)\s*(?:iphones?|items?|units?|orders?)?\s*per\s*(?:customer|person|account)", re.I)
PER_ORDER_CAP = re.compile(PER_ORDER + r"\s*" + LIMIT + r"?\s*" + PURCHASE + r"\s*(?:数量\s*)?(?:为\s*)?[:：]?\s*" + QUANTITY, re.I)
PER_ORDER_CAP_EN = re.compile(r"(?:limit(?:ed)?\s*(?:of|to)?\s*1(?!\d)\s*(?:iphones?|items?|units?)?\s*per\s*order|one\s+(?:item|unit|iphone)\s+per\s+order)", re.I)
CLAUSE_BREAK = re.compile(r"[。；;！!？?\n\r，,、]")


def _clause_before(text: str, index: int) -> str:
    start = 0
    for match in CLAUSE_BREAK.finditer(text, 0, index):
        start = match.end()
    return text[start:index]


def per_order_quantity_cap(snap) -> str:
    """A per-order quantity cap ("每单限购 1 台"); informational only, never switches policy."""
    if not trusted_url(snap.get("url", "")):
        return ""
    text = snap.get("text", "")
    match = PER_ORDER_CAP.search(text) or PER_ORDER_CAP_EN.search(text)
    return match[0] if match else ""


def one_per_customer(snap):
    """Positive UI evidence only: one item per *person/account*, not per cart or per order.

    An absent notice is not evidence of permission for two orders. The user's
    adaptive policy explicitly permits proceeding until a restriction appears.
    A clause whose quantity is qualified by 每单/单次/每笔/每次 is a per-order cap and
    is ignored even when it also names the customer ("每位顾客每单限购 1 台").
    """
    if not trusted_url(snap.get("url", "")):
        return ""
    text = snap.get("text", "")
    for match in PER_PERSON.finditer(text):
        clause = _clause_before(text, match.start()) + match[0]
        if re.search(PER_ORDER, clause, re.I):
            continue
        return match[0]
    match = PER_PERSON_EN.search(text)
    return match[0] if match else ""


class OrderCoordinator:
    """Shared by both QThreads; the app also enforces a single GUI instance.

    The lock covers only local state and fsync, never a network await. A durable
    reservation is made before any submit click, and is never automatically
    released, including when the click's outcome is unknown.
    """
    def __init__(self, root: Path):
        self.path = Path(root) / "order-coordination.json"
        self._lock = threading.RLock()
        self._error = ""
        self._state = {"schema": 1, "policy": "adaptive", "limit": None, "attempts": {}}
        try:
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if (state.get("schema") != 1 or state.get("policy") not in POLICIES or
                        not isinstance(state.get("attempts"), dict) or
                        any(mode not in FULFILLMENTS or not isinstance(attempt, dict) or
                            attempt.get("phase") not in ("submit-attempt", "confirmed", "unknown")
                            for mode, attempt in state["attempts"].items())):
                    raise ValueError("Invalid coordination record")
                self._state = state
            # Preserve submission evidence from releases before the shared gate.
            changed = False
            for mode, folder in (("pickup", Path(root)), ("delivery", Path(root) / "delivery")):
                journal_path = folder / "purchase-state.json"
                if not journal_path.exists():
                    continue
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if not journal.get("trial") and journal.get("phase") in ("submit-attempt", "complete") and mode not in self._state["attempts"]:
                    self._state["attempts"][mode] = {
                        "phase": "confirmed" if journal["phase"] == "complete" else "submit-attempt",
                        "time": journal.get("time", ""), "imported": True,
                    }
                    changed = True
            if changed:
                self._write(self._state)
        except (OSError, ValueError, TypeError, AttributeError):
            self._error = "全局订单记录无法读取，已禁止自动提交。请先在官网核对既有订单。"

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        self._state = state

    def status(self):
        with self._lock:
            value = copy.deepcopy(self._state)
            value["single"] = value["policy"] == "single" or bool(value.get("limit"))
            value["error"] = self._error
            value["owner"] = next(iter(value["attempts"]), None) if value["attempts"] else None
            value.setdefault("mode", "parallel")
            return value

    def set_policy(self, policy):
        if policy not in POLICIES:
            raise ValueError("未知的订单策略。")
        with self._lock:
            if self._error:
                raise ValueError(self._error)
            if self._state["attempts"]:
                raise ValueError("已有提交记录，请先核对订单；不能切换策略重新下单。")
            state = copy.deepcopy(self._state)
            state["policy"] = policy
            self._write(state)

    def set_mode(self, mode):
        """"parallel" (default) or "sequential-account" (§13.2: forced login, one Apple ID, one bag)."""
        if mode not in MODES:
            raise ValueError("未知的下单模式。")
        with self._lock:
            if self._state.get("mode", "parallel") == mode:
                return False
            state = copy.deepcopy(self._state)
            state["mode"] = mode
            try:
                self._write(state)
            except OSError:
                self._state = state
            return True

    def observe(self, snap):
        evidence = one_per_customer(snap)
        if not evidence:
            return False
        with self._lock:
            if self._error or self._state.get("limit"):
                return False
            state = copy.deepcopy(self._state)
            url = urlsplit(snap["url"])._replace(query="", fragment="").geturl()
            state["limit"] = {"maximum": 1, "evidence": evidence, "url": url,
                              "time": datetime.now(CHINA).isoformat()}
            # Even an I/O error must never turn an observed restriction off.
            self._state = state
            try:
                self._write(state)
            except OSError:
                self._error = "已识别限购，但全局记录保存失败。已禁止自动提交，请检查本机目录。"
            return True

    def blocked_reason(self, mode):
        with self._lock:
            if self._error:
                return self._error
            attempts = self._state["attempts"]
            if mode in attempts:
                return "本路已有提交记录，请在官网核对结果；不会重复提交。"
            if (self._state["policy"] == "single" or self._state.get("limit")) and attempts:
                owner = next(iter(attempts))
                return FULFILLMENTS[owner] + "已取得提交权或已提交。本路暂停，不自动补单；请先核对官网结果。"
            return ""

    def claim(self, mode):
        if mode not in FULFILLMENTS:
            raise ValueError("未知任务。")
        with self._lock:
            reason = self.blocked_reason(mode)
            if reason:
                return False, reason
            state = copy.deepcopy(self._state)
            state["attempts"][mode] = {"phase": "submit-attempt", "time": datetime.now(CHINA).isoformat()}
            # No yield between checking ownership and persisting the reservation.
            self._write(state)
            return True, ""

    def outcome(self, mode, phase):
        if phase not in ("confirmed", "unknown"):
            raise ValueError("未知提交结果。")
        with self._lock:
            state = copy.deepcopy(self._state)
            previous = state["attempts"].setdefault(mode, {"time": datetime.now(CHINA).isoformat()})
            if previous.get("phase") != "confirmed":
                previous["phase"] = phase
            self._write(state)

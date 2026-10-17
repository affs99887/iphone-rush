"""Event dictionaries exchanged between operators, the dispatcher and the UI (brief §4).

Every event carries ``type``, ``operator`` ("pickup"/"delivery"/None) and ``time``
(ISO 8601, Beijing time). Builders never include secrets; callers mask text first.
"""
from __future__ import annotations

from datetime import datetime

from .model import CHINA

# Dispatcher events
DISPATCHER_STATE = "dispatcher.state"
DISPATCHER_POLICY = "dispatcher.policy"
DISPATCHER_CLOCK = "dispatcher.clock"
DISPATCHER_HEALTH = "dispatcher.health"
DISPATCHER_BROWSER_MODE = "dispatcher.browser-mode"
# Operator events
OPERATOR_STATE = "operator.state"
OPERATOR_STEP = "operator.step"
OPERATOR_LOG = "operator.log"
OPERATOR_LANES = "operator.lanes"
OPERATOR_TIMING = "operator.timing"
OPERATOR_CHECKS = "operator.checks"
OPERATOR_STORE = "operator.store"
OPERATOR_REVIEW = "operator.review"
OPERATOR_ATTENTION = "operator.attention"
OPERATOR_OTP = "operator.otp"
OPERATOR_CONSENT = "operator.consent"
OPERATOR_PAGE = "operator.page"
OPERATOR_NETWORK = "operator.network"
OPERATOR_RUN = "operator.run"
OPERATOR_FINISHED = "operator.finished"
OPERATOR_ERROR = "operator.error"
# Internal (operator → dispatcher only; never forwarded to the UI as-is)
INTERNAL_COORDINATION = "internal.coordination"
INTERNAL_BROWSER = "internal.browser"
INTERNAL_PREWARMED = "internal.prewarmed"

DISPATCHER_STATES = ("idle", "preparing", "armed", "running", "stopping")
PHASES = ("idle", "waiting", "entry", "configure", "add", "bag", "checkout", "fulfillment",
          "contact", "verify", "payment", "review", "submit", "done")
# The UI step bar shows six cells; checkout sub-phases collapse into "checkout".
STEP_OF_PHASE = {
    "idle": "", "waiting": "", "entry": "entry", "configure": "configure", "add": "configure",
    "bag": "bag", "checkout": "checkout", "fulfillment": "checkout", "contact": "checkout",
    "verify": "checkout", "payment": "checkout", "review": "review", "submit": "submit", "done": "submit",
}
STEPS = ("entry", "configure", "bag", "checkout", "review", "submit")
STEP_TITLES = {"entry": "入口", "configure": "选配", "bag": "购物袋", "checkout": "结账", "review": "核对", "submit": "提交"}
HEALTH_ACTIONS = ("none", "soft-restart", "hard-restart")
BROWSER_MODES = ("headless", "hidden")
BROWSER_POLICIES = ("auto", "headless", "hidden")
POLICY_MODES = ("parallel", "sequential-account")
OTP_KINDS = ("sms", "apple-2fa")
# Operator → dispatcher only
INTERNAL_LOGIN_REQUIRED = "internal.login-required"
INTERNAL_SHIELD = "internal.shield"


def now_iso() -> str:
    return datetime.now(CHINA).isoformat(timespec="milliseconds")


def event(type_: str, operator: str | None = None, **fields) -> dict:
    value = {"type": type_, "operator": operator, "time": now_iso()}
    value.update(fields)
    return value


def dispatcher_state(state: str, message: str = "") -> dict:
    if state not in DISPATCHER_STATES:
        raise ValueError("未知的指派员状态：" + state)
    return event(DISPATCHER_STATE, None, state=state, message=message)


def dispatcher_policy(policy: str, single: bool, limit: dict | None, owner: str | None, message: str = "",
                      mode: str = "parallel") -> dict:
    if mode not in POLICY_MODES:
        raise ValueError("未知的下单模式：" + str(mode))
    return event(DISPATCHER_POLICY, None, policy=policy, single=bool(single), limit=limit, owner=owner,
                 message=message, mode=mode)


def dispatcher_clock(offset_ms: int, source: str) -> dict:
    return event(DISPATCHER_CLOCK, None, offset_ms=int(offset_ms), source=source)


def dispatcher_health(operator: str, healthy: bool, reason: str = "", action: str = "none") -> dict:
    if action not in HEALTH_ACTIONS:
        raise ValueError("未知的自愈动作：" + action)
    return event(DISPATCHER_HEALTH, operator, healthy=bool(healthy), reason=reason, action=action)


def dispatcher_browser_mode(operator: str, mode: str, reason: str = "") -> dict:
    if mode not in BROWSER_MODES:
        raise ValueError("未知的浏览器模式：" + mode)
    return event(DISPATCHER_BROWSER_MODE, operator, mode=mode, reason=reason)


def operator_state(operator: str, state: str, detail: str, phase: str) -> dict:
    return event(OPERATOR_STATE, operator, state=state, detail=detail, phase=phase)


def operator_step(operator: str, phase: str, done: bool) -> dict:
    return event(OPERATOR_STEP, operator, phase=phase, done=bool(done))


def operator_log(operator: str, message: str, level: str = "info") -> dict:
    if level not in ("info", "warn", "error"):
        raise ValueError("未知的日志级别：" + level)
    return event(OPERATOR_LOG, operator, message=message, level=level)


def operator_lanes(operator: str, total: int, loading: int, requests: int, cooldown_s: float, winner: int | None) -> dict:
    return event(OPERATOR_LANES, operator, total=int(total), loading=int(loading), requests=int(requests),
                 cooldown_s=round(max(0.0, float(cooldown_s)), 1), winner=winner)


def operator_timing(operator: str, action: str, ms: int, phase: str = "", phase_ms: int | None = None) -> dict:
    """Click→confirmation latency; with ``phase``/``phase_ms`` it also reports a finished phase (§12.5)."""
    return event(OPERATOR_TIMING, operator, action=action, ms=int(ms), phase=phase,
                 phase_ms=None if phase_ms is None else int(phase_ms))


def operator_checks(operator: str, checks: dict) -> dict:
    return event(OPERATOR_CHECKS, operator, checks={k: bool(checks.get(k)) for k in ("color", "capacity", "trade", "care")})


def operator_store(operator: str, store: str) -> dict:
    return event(OPERATOR_STORE, operator, store=store)


def operator_review(operator: str, valid: bool, problems: list | None = None, total: float | None = None,
                    store: str = "", destination: str = "") -> dict:
    return event(OPERATOR_REVIEW, operator, valid=bool(valid), problems=list(problems or []), total=total,
                 store=store, destination=destination)


def operator_attention(operator: str, message: str, suggestion: str, review: bool = False) -> dict:
    return event(OPERATOR_ATTENTION, operator, message=message, suggestion=suggestion, review=bool(review))


def operator_otp(operator: str, request: str, phone_tail: str, message: str, error: bool = False, kind: str = "sms") -> dict:
    if kind not in OTP_KINDS:
        raise ValueError("未知的验证码类型：" + str(kind))
    return event(OPERATOR_OTP, operator, request=request, phone_tail=phone_tail[-4:], message=message,
                 error=bool(error), kind=kind)


def operator_consent(operator: str, request: str, url: str, text: str) -> dict:
    return event(OPERATOR_CONSENT, operator, request=request, url=url, text=text)


def operator_page(operator: str, url: str, text: str, image: str = "") -> dict:
    return event(OPERATOR_PAGE, operator, url=url, text=text, image=image or "")


def operator_network(operator: str, status: int, path: str, count: int) -> dict:
    return event(OPERATOR_NETWORK, operator, status=int(status), path=path, count=int(count))


def operator_run(operator: str, trial: bool, target: str) -> dict:
    return event(OPERATOR_RUN, operator, trial=bool(trial), target=target)


def operator_finished(operator: str, success: bool, reason: str = "") -> dict:
    return event(OPERATOR_FINISHED, operator, success=bool(success), reason=reason)


def operator_error(operator: str, message: str) -> dict:
    return event(OPERATOR_ERROR, operator, message=message)


def step_of(phase: str) -> str:
    return STEP_OF_PHASE.get(phase, "")


def steps_done(phase: str) -> dict:
    """Which of the six UI steps are complete for a given phase (the current step is not done)."""
    current = step_of(phase)
    if phase == "done":
        return {step: True for step in STEPS}
    index = STEPS.index(current) if current in STEPS else -1
    return {step: i < index for i, step in enumerate(STEPS)}

"""Compatibility shim: the 1.x ``Worker`` is now :class:`duo.operator.Operator`.

Kept so older scripts and journals keep importing from ``duo.engine``; nothing else
lives here. New code should import ``duo.operator`` directly.
"""
from __future__ import annotations

from .operator import Operator, perform  # noqa: F401

Worker = Operator

__all__ = ["Operator", "Worker", "perform"]

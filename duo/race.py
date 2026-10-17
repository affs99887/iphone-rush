"""Bounded, asynchronous entry navigation. This module never clicks a page."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .browser import INVENTORY
from .model import parse_retry_after, retry_delay, trusted_url

BLOCKED = (403, 429, 541)


@dataclass
class Lane:
    page: object
    number: int
    due: float = 0
    errors: int = 0
    task: object = None


class EntryRace:
    """Several pages race to open the purchase page; the first ready page wins.

    ``emit(kind, value)`` receives ``"log"`` (str), ``"lanes"`` (dict with total/
    loading/requests/cooldown_s/winner) and ``"blocked"`` (dict with status/path/
    retry_after) so the operator can apply the dispatcher's recovery policy.
    """

    def __init__(self, pages, url, cfg, adapter_factory, emit, observe=None):
        self.lanes = [Lane(page, i + 1) for i, page in enumerate(pages)]
        self.url, self.cfg = url, cfg
        self.adapter_factory, self.emit = adapter_factory, emit
        self.observe = observe
        self.next_launch = self.cooldown = 0.0
        self.requests = 0
        self.blocks = 0
        self.last_block = None
        self.closed = False

    def status(self, winner=None) -> dict:
        now = time.monotonic()
        return {"total": len(self.lanes), "loading": sum(bool(x.task) for x in self.lanes),
                "requests": self.requests, "cooldown_s": max(0.0, self.cooldown - now), "winner": winner}

    async def probe(self, lane):
        started = time.monotonic()
        try:
            response = await lane.page.goto(self.url, wait_until="domcontentloaded", timeout=8000)
            status = response.status if response else 200
            headers = await response.all_headers() if response else {}
            retry_after = parse_retry_after(headers.get("retry-after", "0"))
            if status >= 400:
                return {"status": status, "retry_after": retry_after}
            if not trusted_url(lane.page.url):
                return {"manual": "入口跳转至官网之外，请检查浏览器。"}
            # Reading an interactive product control is enough to win. No fixed
            # loading sleep and no cart mutation takes place inside a probe.
            limit = time.monotonic() + 3
            while True:
                snap = await lane.page.evaluate(INVENTORY)
                if self.observe:
                    self.observe(snap)
                adapter = self.adapter_factory()
                dec = adapter.plan(snap)
                if dec.kind == "manual":
                    return {"manual": dec.message, "snap": snap}
                has_configuration = any(n.get("name") in ("dimensionColor", "dimensionCapacity", "tradeupinline") for n in snap["nodes"])
                if adapter.target.path in snap["url"] and (dec.kind in ("select", "add") or (dec.kind == "wait" and has_configuration)):
                    return {"ready": True, "snap": snap, "milliseconds": round((time.monotonic() - started) * 1000)}
                if dec.kind == "refresh" or time.monotonic() >= limit:
                    return {"status": status, "retry_after": retry_after}
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"status": 0, "failed": True}

    async def poll(self):
        """Return one winner, or start another eligible navigation without waiting."""
        if self.closed:
            return None
        now = time.monotonic()
        completed = []
        for lane in self.lanes:
            if lane.task and lane.task.done():
                result = lane.task.result()
                lane.task = None
                completed.append((lane, result))
                status = result.get("status", 200)
                lane.errors = lane.errors + 1 if result.get("failed") or status >= 400 else 0
                delay = retry_delay(self.cfg.refresh_seconds, lane.errors, status, result.get("retry_after", 0))
                lane.due = now + delay
                if status in BLOCKED or result.get("retry_after", 0):
                    self.cooldown = max(self.cooldown, now + delay)
                    if status in BLOCKED:
                        self.blocks += 1
                        self.last_block = {"status": status, "path": self.url, "retry_after": result.get("retry_after", 0),
                                           "cooldown_s": delay, "time": now}
                        self.emit("blocked", dict(self.last_block))
                    self.emit("log", f"入口 {lane.number} 收到 HTTP {status}（限流或拦截），所有入口共同等待 {delay:.1f} 秒。")
        # A challenge from any lane pauses all lanes, even if another is ready.
        winner = next(((lane, result) for lane, result in completed if result.get("manual")), None)
        winner = winner or next(((lane, result) for lane, result in completed if result.get("ready")), None)
        if winner:
            lane, result = winner
            await self.stop(keep=lane.page)
            self.emit("lanes", self.status(winner=lane.number))
            return lane.page, result, lane.number
        if now >= max(self.next_launch, self.cooldown):
            available = [lane for lane in self.lanes if not lane.task and now >= lane.due]
            if available:
                lane = min(available, key=lambda x: (x.due, x.number))
                lane.task = asyncio.create_task(self.probe(lane))
                self.requests += 1
                # Global request budget: at most two new navigations per second.
                self.next_launch = now + max(0.5, self.cfg.refresh_seconds / len(self.lanes))
                self.emit("lanes", self.status())
        elif completed:
            self.emit("lanes", self.status())
        return None

    async def stop(self, keep=None):
        self.closed = True
        tasks = [lane.task for lane in self.lanes if lane.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Quiesce every losing page before allowing the winner to write cart state.
        for lane in self.lanes:
            if lane.page is not keep and not lane.page.is_closed():
                try:
                    await lane.page.close()
                except Exception:
                    pass

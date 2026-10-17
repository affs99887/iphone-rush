"""Bounded diagnostics against the real Apple site using the production worker.

Never submits an order, accepts terms or calls private endpoints. This complements,
but does not replace, the packaged EXE acceptance run.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]
from PySide6.QtCore import QCoreApplication
from duo.operator import Operator as Worker
from duo.model import Config
import duo.operator as engine


async def main():
    count = int(sys.argv[1])
    folder = ROOT / "artifacts" / ("headless-add-" + sys.argv[2])
    folder.mkdir(parents=True, exist_ok=True)
    worker = Worker(folder, "delivery")
    worker.trial = True
    worker.event.connect(lambda kind, value: print(kind, value, flush=True) if kind in ("state", "log") else None)
    cfg = Config(fulfillment="delivery", entry_tabs=count)
    if worker.read_journal() and "--retry-empty" in sys.argv:
        await worker.handle("reset-record", cfg)
    await worker.handle("recover" if worker.read_journal() else "trial", cfg)
    original_perform = engine.perform
    async def inspect_add(page, decision):
        if decision.kind == "add":
            if "--settle" in sys.argv:
                await asyncio.sleep(3)
                fresh = await worker.snapshot()
                if not all(worker.adapter.selected_checks(fresh).values()):
                    print("CONFIG_CHANGED_DURING_SETTLE", flush=True)
                    worker.pending = None
                    worker.adapter.added = False
                    return
            await worker.capture_page()
            for suffix in ("json", "png"):
                source = worker.session_dir / "diagnostics" / ("latest-page." + suffix)
                (folder / ("before-add." + suffix)).write_bytes(source.read_bytes())
            details = await page.locator(decision.node["selector"]).evaluate("e => ({button:e.outerHTML, action:e.form?.getAttribute('action'), method:e.form?.method, page:location.origin+location.pathname, ready:document.readyState})")
            (folder / "add-element.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            await original_perform(page, decision)
        except Exception as exc:
            detail = {"error": str(exc), "node": decision.node}
            if decision.node:
                detail["element"] = await page.locator(decision.node["selector"]).evaluate("e=>({html:e.outerHTML,labels:[...(e.labels||[])].map(l=>l.outerHTML)})")
            (folder / "action-error.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
            raise
    engine.perform = inspect_add
    network = []
    errors = []
    worker.context.on("weberror", lambda error: errors.append(str(error.error)))
    def response(r):
        if r.request.is_navigation_request() or r.status >= 400:
            u = urlsplit(r.url)
            network.append({"status": r.status, "method": r.request.method, "url": u.scheme + "://" + u.netloc + u.path})
    worker.context.on("response", response)
    try:
        until = time.monotonic() + 75
        while worker.active and not worker.paused and time.monotonic() < until:
            await worker.tick()
            if worker.adapter.added:
                # Inspect the outcome before recovering the bag, then stop at
                # any checkout. The worker's trial guard still applies.
                if "checkout" in worker.page.url or "signIn" in worker.page.url:
                    break
            await asyncio.sleep(0.03)
        await worker.capture_page()
        (folder / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "browser-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        print("RESULT", worker.last_state, worker.last_message, flush=True)
    finally:
        (folder / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
        await worker.handle("shutdown", None)
        if worker.context:
            await worker.context.close()
        if worker.playwright:
            await worker.playwright.stop()


if __name__ == "__main__":
    app = QCoreApplication([])
    asyncio.run(main())

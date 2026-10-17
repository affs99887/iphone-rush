"""File-based remote control for attended acceptance runs (``--remote <folder>`` only).

It never moves the user's mouse or keyboard: commands drive the app's own widgets
(the same buttons and dialogs a person would click), and a status file plus
periodic screenshots let the assistant follow along. A normal double-click
launch never enables this.

Folder layout:
    commands/<n>-<name>.json   one command per file, processed in name order, then deleted
    status.json                rewritten every second (state, pending dialogs, results)
    log.jsonl                  one line per processed command / app event (masked text only)
    shots/window-*.png         screenshot of the main window (every 5 s while a run is active)
    shots/page-<op>.png        latest page capture per operator (copied from its diagnostics)

Commands (JSON object with ``cmd`` plus fields):
    trial                         click 官网试跑 (auto-confirms the app's own confirmation box)
    arm / stop_all                click 一键布防 / 停止全部
    consent  {"op", "accept"}     click 同意 / 暂不同意 in that card's consent dialog
    otp      {"op", "code"}       type the code into that card's verification dialog (auto-submits)
    login    {"op", "apple_id", "password"}   fill and send that card's login dialog
    submit   {"op"}               click 提交已核对订单 on that card (auto-confirms the app's box)
    command  {"op", "name"}       send any operator command (pause, resume, show, reset-record, …)
    screenshot                    capture the window now
    quit                          close the app (stops both operators)
Codes and passwords are never written to status.json or log.jsonl.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

CHINA = timezone(timedelta(hours=8))
SECRET_KEYS = ("code", "password")


def now() -> str:
    return datetime.now(CHINA).strftime("%H:%M:%S")


class RemoteControl(QObject):
    def __init__(self, window, dispatcher, folder: Path):
        super().__init__(window)
        self.window, self.dispatcher = window, dispatcher
        self.folder = Path(folder)
        self.commands = self.folder / "commands"
        self.shots = self.folder / "shots"
        for path in (self.commands, self.shots):
            path.mkdir(parents=True, exist_ok=True)
        for stale in self.commands.glob("*.json"):
            stale.unlink(missing_ok=True)
        self.auto_confirm = 0           # number of app confirmation boxes to accept automatically
        self.last_event = {}
        self.results: dict[str, dict] = {}
        self.ticks = 0
        dispatcher.event.connect(self.on_event)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(300)
        self.box_timer = QTimer(self)
        self.box_timer.timeout.connect(self.accept_boxes)
        self.box_timer.start(150)
        self.log({"event": "remote-started", "folder": str(self.folder)})

    # ---- logging / status ---------------------------------------------------------------
    def log(self, record: dict) -> None:
        record = {k: ("•" * 6 if k in SECRET_KEYS else v) for k, v in record.items()}
        record.setdefault("time", now())
        try:
            with (self.folder / "log.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def on_event(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        op = event.get("operator")
        if kind in ("operator.review", "operator.finished", "operator.attention", "operator.otp", "operator.consent",
                    "operator.network", "dispatcher.policy", "dispatcher.browser-mode", "operator.error"):
            slim = {k: v for k, v in event.items() if k not in ("text",)}
            self.log({"event": kind, **slim})
        if kind == "operator.review" and op:
            self.results.setdefault(op, {})["review"] = {"valid": event.get("valid"), "problems": event.get("problems"),
                                                         "total": event.get("total"), "time": now()}
        if kind == "operator.finished" and op:
            self.results.setdefault(op, {})["finished"] = {"success": event.get("success"), "reason": event.get("reason"),
                                                           "time": now()}
        if kind == "operator.page" and op and event.get("image"):
            try:
                shutil.copy(event["image"], self.shots / f"page-{op}.png")
            except OSError:
                pass

    def card_status(self, op: str, card) -> dict:
        return {
            "pill": card.pill.text(), "detail": card.detail.text()[:300], "phase": getattr(card, "phase", ""),
            "running": card.running, "pending": card.pending, "paused": card.paused, "trial": card.trial,
            "review": card.review_label.text(), "submit_enabled": card.submit_button.isEnabled(),
            "submitted": card.submitted,
            "otp_dialog": bool(self.window.notices.active(op) is not None),
            "otp_kind": getattr(self.window.notices.active(op), "kind", ""),
            "notices": [{"key": c.key, "state": c.state, "kind": c.kind} for c in self.window.notices.of_operator(op)],
            "consent_dialog": bool(card.consent_dialog is not None and card.consent_dialog.isVisible()),
            "login_dialog": bool(card.login_dialog is not None and card.login_dialog.isVisible()),
            "suggestion": card.suggest_button.text() if card.suggest_button.isVisible() else "",
            **self.results.get(op, {}),
        }

    def write_status(self) -> None:
        try:
            engine = self.dispatcher.status()
        except Exception as exc:  # noqa: BLE001
            engine = {"error": str(exc)}
        payload = {"time": now(), "dispatcher_state": self.window.panel.state,
                   "primary_button": self.window.primary_button.text(),
                   "cards": {op: self.card_status(op, card) for op, card in self.window.cards.items()},
                   "engine": {op: {k: info.get(k) for k in ("state", "detail", "phase", "running", "paused",
                                                            "submitted", "review_ok", "store", "timeline_ms", "blocks")}
                              for op, info in (engine.get("operators") or {}).items()},
                   "policy": {k: engine.get(k) for k in ("policy", "mode", "owner", "attempts", "limit")}}
        tmp = self.folder / "status.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.folder / "status.json")

    def screenshot(self, tag: str = "") -> None:
        name = f"window-{datetime.now(CHINA).strftime('%H%M%S')}{('-' + tag) if tag else ''}.png"
        self.window.grab().save(str(self.shots / name))
        self.window.grab().save(str(self.shots / "window-latest.png"))

    # ---- the app's own confirmation boxes ---------------------------------------------
    def accept_boxes(self) -> None:
        if self.auto_confirm <= 0:
            return
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.isVisible():
                accept = next((b for b in widget.buttons()
                               if widget.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole), None)
                if accept is not None:
                    self.log({"event": "auto-confirm", "title": widget.windowTitle(), "button": accept.text()})
                    self.auto_confirm -= 1
                    accept.click()
                    return

    def with_confirm(self, action) -> None:
        """Run an action that opens one of the app's modal confirmation boxes; the box timer accepts it."""
        self.auto_confirm += 1
        QTimer.singleShot(0, action)

    # ---- command processing -----------------------------------------------------------
    def tick(self) -> None:
        self.ticks += 1
        for path in sorted(self.commands.glob("*.json")):
            try:
                command = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            path.unlink(missing_ok=True)
            try:
                result = self.run(command)
            except Exception as exc:  # noqa: BLE001
                result = f"error: {exc.__class__.__name__}: {exc}"
            self.log({"event": "command", "cmd": command.get("cmd"), "op": command.get("op"), "result": result})
            command.clear()
        if self.ticks % 3 == 0:
            self.write_status()
        busy = any(c.running or c.pending for c in self.window.cards.values())
        if busy and self.ticks % 17 == 0:
            self.screenshot()

    def card(self, op: str):
        if op not in self.window.cards:
            raise ValueError("unknown operator " + str(op))
        return self.window.cards[op]

    def run(self, command: dict) -> str:
        cmd = command.get("cmd")
        if cmd == "trial":
            self.with_confirm(self.window.start_trial)
            return "clicked 官网试跑"
        if cmd == "arm":
            self.window.primary_action()
            return "clicked " + self.window.primary_button.text()
        if cmd == "stop_all":
            self.with_confirm(self.window.primary_action)
            return "clicked 停止全部"
        if cmd == "consent":
            card = self.card(command["op"])
            if card.consent_dialog is None:
                return "no consent dialog"
            (card.consent_dialog.agree if command.get("accept", True) else card.consent_dialog.decline).click()
            return "consent " + ("accepted" if command.get("accept", True) else "declined")
        if cmd == "otp":
            self.card(command["op"])
            notice = self.window.notices.active(command["op"])
            if notice is None:
                return "no pending verification notification"
            notice.enter_code(str(command.get("code", "")), "remote")
            return "code entered in the notification (" + notice.kind + ")"
        if cmd == "login":
            card = self.card(command["op"])
            dialog = card.open_login()
            dialog.apple_id.setText(str(command.get("apple_id", "")))
            dialog.password.setText(str(command.get("password", "")))
            dialog.send()
            return "login sent"
        if cmd == "submit":
            card = self.card(command["op"])
            if not card.submit_button.isEnabled():
                return "submit button is not enabled"
            self.with_confirm(card.submit_button.click)
            return "clicked 提交已核对订单"
        if cmd == "command":
            self.window.send_command(command["op"], command["name"], command.get("payload"))
            return "sent " + str(command["name"])
        if cmd == "screenshot":
            self.screenshot(str(command.get("tag", "")))
            return "screenshot saved"
        if cmd == "quit":
            self.window.closing = False
            self.with_confirm(self.window.close)
            return "closing"
        return "unknown command"

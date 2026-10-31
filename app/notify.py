"""Notification delivery: ntfy, the built-in /dash display, an optional
outbound "dash webhook" for external dashboards (Home Assistant, Node-RED,
TRMNL, MagicMirror...), and Dash LED clocks.

Every notification is recorded in the ``alerts`` table, published to
connected /dash screens, and then fanned out to whichever external channels
are configured.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import dash_clock
from .events import EventBus
from .settings_store import Settings
from .timeutil import now_db

log = logging.getLogger("lilypad.notify")

NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
USER_AGENT = "LilyPad/1.0 (+self-hosted infant care tracker)"


@dataclass
class Notification:
    title: str
    message: str
    source: str  # "reminder" | "vital" | "test"
    priority: int = 3
    tags: list[str] = field(default_factory=list)
    reminder_id: int | None = None
    stage: str | None = None
    # Extra structured data for the dash webhook / SSE payload.
    context: dict[str, Any] = field(default_factory=dict)
    # Channel switches (the reminder's own preferences).
    to_ntfy: bool = True
    to_dash: bool = True


def parse_header_lines(raw: str) -> dict[str, str]:
    headers = {}
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name, value = name.strip(), value.strip()
        if name and re.fullmatch(r"[A-Za-z0-9-]+", name):
            headers[name] = value
    return headers


class Notifier:
    def __init__(self, bus: EventBus, transport: httpx.BaseTransport | None = None, timeout: float = 10.0):
        self.bus = bus
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport, timeout=self._timeout, headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    # -- public -----------------------------------------------------------------

    def deliver(self, conn: sqlite3.Connection, settings: Settings, note: Notification) -> dict[str, Any]:
        created = now_db()
        cur = conn.execute(
            """INSERT INTO alerts (created_at, source, reminder_id, stage, title, message, priority)
               VALUES (?,?,?,?,?,?,?)""",
            (created, note.source, note.reminder_id, note.stage, note.title, note.message, note.priority),
        )
        alert_id = int(cur.lastrowid)
        alert = {
            "id": alert_id,
            "created_at": created,
            "source": note.source,
            "reminder_id": note.reminder_id,
            "stage": note.stage,
            "title": note.title,
            "message": note.message,
            "priority": note.priority,
            "tags": note.tags,
            "context": note.context,
        }

        delivery: dict[str, str] = {}
        if note.to_dash:
            self.bus.publish("alert", alert)
            delivery["dash"] = "shown"
        else:
            delivery["dash"] = "off for this reminder"

        if not note.to_ntfy:
            delivery["ntfy"] = "off for this reminder"
        elif not settings.flag("ntfy_enabled"):
            delivery["ntfy"] = "not configured"
        else:
            delivery["ntfy"] = self._attempt(lambda: self.send_ntfy(settings, note, alert_id))

        if not note.to_dash:
            delivery["webhook"] = "off for this reminder"
        elif not (settings.flag("dash_webhook_enabled") and settings.is_set("dash_webhook_url")):
            delivery["webhook"] = "not configured"
        else:
            delivery["webhook"] = self._attempt(lambda: self.send_webhook(settings, alert))

        if not note.to_dash:
            delivery["clock"] = "off for this reminder"
        elif not settings.dash_clock_ready:
            delivery["clock"] = "not configured"
        elif note.stage == "window" and settings.flag("dash_clock_skip_window"):
            delivery["clock"] = "skipped (window open)"
        else:
            delivery["clock"] = self.send_dash_clocks(settings, alert)

        conn.execute("UPDATE alerts SET delivery = ? WHERE id = ?", (json.dumps(delivery), alert_id))
        alert["delivery"] = delivery
        return alert

    # -- channels ------------------------------------------------------------------

    def send_ntfy(self, settings: Settings, note: Notification, alert_id: int | None = None) -> str:
        server = settings["ntfy_server"].strip().rstrip("/") or "https://ntfy.sh"
        topic = settings["ntfy_topic"].strip()
        if not NTFY_TOPIC_RE.match(topic):
            raise ValueError("ntfy topic is missing or invalid")
        body: dict[str, Any] = {
            "topic": topic,
            "title": note.title,
            "message": note.message,
            "priority": max(1, min(5, int(note.priority))),
            "tags": note.tags,
        }
        base = settings.base_url
        if base:
            body["click"] = base + ("/dash" if note.source == "test" else "/")
            if settings.flag("ntfy_actions"):
                actions: list[dict[str, Any]] = [{"action": "view", "label": "Open LilyPad", "url": base + "/"}]
                if note.reminder_id is not None and note.stage in ("window", "due", "overdue"):
                    actions.append({
                        "action": "http", "label": "Snooze 15 min", "method": "POST",
                        "url": f"{base}/api/reminders/{note.reminder_id}/snooze?minutes=15",
                        "clear": True,
                    })
                if alert_id is not None and note.source == "vital":
                    actions.append({
                        "action": "http", "label": "Acknowledge", "method": "POST",
                        "url": f"{base}/api/alerts/{alert_id}/ack", "clear": True,
                    })
                body["actions"] = actions
        headers = {"Content-Type": "application/json"}
        token = settings["ntfy_token"].strip()
        user, password = settings["ntfy_username"].strip(), settings["ntfy_password"]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif user:
            creds = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        with self._client() as client:
            resp = client.post(server + "/", json=body, headers=headers)
        if resp.status_code >= 300:
            raise RuntimeError(f"ntfy returned HTTP {resp.status_code}: {resp.text[:200]}")
        return "sent"

    def send_webhook(self, settings: Settings, alert: dict[str, Any]) -> str:
        url = settings["dash_webhook_url"].strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("dash webhook URL must start with http:// or https://")
        payload = {
            "event": "lilypad.alert",
            "baby": settings.baby_name if settings.notify_detail == "detailed" else None,
            "dash_url": (settings.base_url + "/dash") if settings.base_url else None,
            "alert": {k: alert[k] for k in
                      ("id", "created_at", "source", "reminder_id", "stage", "title", "message", "priority", "tags")},
            # Structured details (names, times, values) are withheld at the minimal level.
            "context": (alert.get("context") or {}) if settings.notify_detail != "minimal" else {},
        }
        headers = {"Content-Type": "application/json", **parse_header_lines(settings["dash_webhook_headers"])}
        with self._client() as client:
            resp = client.post(url, content=json.dumps(payload), headers=headers)
        if resp.status_code >= 300:
            raise RuntimeError(f"webhook returned HTTP {resp.status_code}")
        return "sent"

    def send_dash_clocks(self, settings: Settings, alert: dict[str, Any]) -> str:
        """POST the message to every configured Dash clock or server; summarise the outcome."""
        payload = dash_clock.build_payload(
            settings["dash_clock_text"], settings["dash_clock_color"].strip(), settings.dash_clock_minutes,
            baby=settings.baby_name, title=alert["title"],
        )
        headers = {"Content-Type": "application/json", **parse_header_lines(settings["dash_clock_headers"])}
        addresses = dash_clock.parse_addresses(settings["dash_clock_urls"])[: dash_clock.MAX_ADDRESSES]
        results = [(a, self._attempt(lambda a=a: self.send_dash_clock(a, payload, headers))) for a in addresses]
        failed = [f"{a}: {r}" for a, r in results if r != "sent"]
        if not failed:
            return "sent" if len(results) == 1 else f"sent to {len(results)} clocks"
        if len(results) == 1:
            return results[0][1]
        return (f"error: {len(failed)} of {len(results)} clocks failed; " + "; ".join(failed))[:300]

    def send_dash_clock(self, address: str, payload: dict[str, Any], headers: dict[str, str]) -> str:
        if not address.lower().startswith(("http://", "https://")):
            raise ValueError("Dash clock address must start with http:// or https://")
        with self._client() as client:
            resp = client.post(address + "/api/notify", content=json.dumps(payload), headers=headers)
        if resp.status_code >= 300:
            raise RuntimeError(f"Dash returned HTTP {resp.status_code}")
        return "sent"

    # -- helpers ---------------------------------------------------------------------

    @staticmethod
    def _attempt(fn, tries: int = 2) -> str:
        last_error = "error"
        for attempt in range(tries):
            try:
                return fn()
            except ValueError as exc:  # configuration problem: retrying won't help
                return f"error: {exc}"[:300]
            except Exception as exc:  # network errors, server errors...
                last_error = f"error: {exc}"[:300]
                log.warning("notification delivery failed (attempt %d): %s", attempt + 1, exc)
                if attempt + 1 < tries:
                    time.sleep(1.5)
        return last_error

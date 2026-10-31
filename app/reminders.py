"""Reminder engine.

Each reminder watches one kind of logged event (feeds, diapers, vitals or a
custom routine) and counts ``interval_minutes`` from the most recent one:

    anchor ─────────── window opens ───── due ── nag ── nag ── ...
            (ok)            (window)       (due / overdue)

A notification goes out when the window opens, when it's due, and then
every ``nag_minutes`` while overdue (up to ``max_nags``). Logging the event
moves the anchor and resets the cycle. Progress is stored in
``reminder_state`` so a restart never re-sends a stage that already went out.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import constants as C
from . import repo
from .notify import Notification, Notifier
from .settings_store import Settings
from .timeutil import fmt_ago, fmt_duration, fmt_time, from_db, minutes_between, to_db, within_daily_window

log = logging.getLogger("lilypad.reminders")

STAGE_RANK = {"ok": 0, "window": 1, "due": 2}
OVERDUE_DISPLAY_AFTER_MINUTES = 15
NTFY_TAGS = {
    "feeding": "baby_bottle",
    "diaper": "baby",
    "diaper_wet": "droplet",
    "diaper_dirty": "baby",
    "vitals": "thermometer",
    "custom": "bell",
}


@dataclass
class ReminderStatus:
    reminder: dict
    stage: str
    anchor: datetime
    anchor_from_event: bool
    due_at: datetime
    window_at: datetime
    last_event: dict | None
    in_progress: bool
    snoozed_until: datetime | None
    in_active_hours: bool
    now: datetime

    @property
    def minutes_to_due(self) -> float:
        return (self.due_at - self.now).total_seconds() / 60.0

    @property
    def progress(self) -> float:
        """Fraction of the interval elapsed (0..1+), for progress rings."""
        total = (self.due_at - self.anchor).total_seconds()
        if total <= 0:
            return 1.0
        return max(0.0, (self.now - self.anchor).total_seconds() / total)

    @property
    def needs_attention(self) -> bool:
        return self.stage in ("window", "due", "overdue")

    def to_json(self) -> dict[str, Any]:
        r = self.reminder
        return {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "stage": self.stage,
            "stage_label": C.REMINDER_STAGE_LABELS.get(self.stage, self.stage),
            "anchor": to_db(self.anchor),
            "anchor_from_event": self.anchor_from_event,
            "window_at": to_db(self.window_at),
            "due_at": to_db(self.due_at),
            "minutes_to_due": round(self.minutes_to_due, 1),
            "progress": round(self.progress, 3),
            "snoozed_until": to_db(self.snoozed_until) if self.snoozed_until else None,
            "in_active_hours": self.in_active_hours,
            "interval_minutes": r["interval_minutes"],
        }


# --- anchors ----------------------------------------------------------------------

def last_event(conn: sqlite3.Connection, reminder: dict, now: datetime) -> tuple[datetime | None, dict | None, bool]:
    """(anchor time, event row, in_progress) for a reminder."""
    kind = reminder["kind"]
    if kind == "feeding":
        row = repo.last_feeding(conn)
        if not row:
            return None, None, False
        started = from_db(row["started_at"])
        if row["ended_at"] is None:
            age = minutes_between(started, now) or 0
            if 0 <= age < C.STALE_FEED_MINUTES:
                return started, row, True
            return started, row, False
        if reminder.get("anchor") == "end":
            return from_db(row["ended_at"]), row, False
        return started, row, False
    if kind in ("diaper", "diaper_wet", "diaper_dirty"):
        kinds = {"diaper": None, "diaper_wet": C.WET_KINDS, "diaper_dirty": C.DIRTY_KINDS}[kind]
        row = repo.last_diaper(conn, kinds)
        return (from_db(row["occurred_at"]) if row else None), row, False
    if kind == "vitals":
        row = repo.last_vital_check(conn, reminder.get("vital_param_id"))
        return (from_db(row["measured_at"]) if row else None), row, False
    if kind == "custom":
        row = repo.last_task_done(conn, reminder["id"])
        return (from_db(row["done_at"]) if row else None), row, False
    return None, None, False


def _state(conn: sqlite3.Connection, reminder_id: int) -> dict:
    row = conn.execute("SELECT * FROM reminder_state WHERE reminder_id = ?", (reminder_id,)).fetchone()
    if row:
        return dict(row)
    return {"reminder_id": reminder_id, "anchor": None, "stage": "ok", "nag_count": 0,
            "last_notified_at": None, "snoozed_until": None}


def _save_state(conn: sqlite3.Connection, state: dict) -> None:
    conn.execute(
        """INSERT INTO reminder_state (reminder_id, anchor, stage, nag_count, last_notified_at, snoozed_until)
           VALUES (:reminder_id, :anchor, :stage, :nag_count, :last_notified_at, :snoozed_until)
           ON CONFLICT(reminder_id) DO UPDATE SET anchor = excluded.anchor, stage = excluded.stage,
             nag_count = excluded.nag_count, last_notified_at = excluded.last_notified_at,
             snoozed_until = excluded.snoozed_until""",
        state,
    )


def compute(conn: sqlite3.Connection, reminder: dict, settings: Settings, now: datetime) -> ReminderStatus:
    anchor, event, in_progress = last_event(conn, reminder, now)
    from_event = anchor is not None
    if anchor is None:
        anchor = from_db(reminder["updated_at"]) or now
    interval = timedelta(minutes=max(1, int(reminder["interval_minutes"])))
    due = anchor + interval
    window = min(max(0, int(reminder["window_minutes"] or 0)), int(reminder["interval_minutes"]))
    window_at = due - timedelta(minutes=window)

    state = _state(conn, reminder["id"])
    snoozed = from_db(state["snoozed_until"]) if state["anchor"] == to_db(anchor) else None
    active = within_daily_window(now, settings.tz, reminder.get("active_start"), reminder.get("active_end"))

    if in_progress:
        stage = "in_progress"
    elif now < window_at:
        stage = "ok"
    elif now < due:
        stage = "window"
    elif (now - due) < timedelta(minutes=OVERDUE_DISPLAY_AFTER_MINUTES):
        stage = "due"
    else:
        stage = "overdue"
    if stage in ("window", "due", "overdue") and snoozed and now < snoozed:
        stage = "snoozed"
    return ReminderStatus(
        reminder=reminder, stage=stage, anchor=anchor, anchor_from_event=from_event,
        due_at=due, window_at=window_at, last_event=event, in_progress=in_progress,
        snoozed_until=snoozed if snoozed and snoozed > now else None,
        in_active_hours=active, now=now,
    )


def compute_all(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[ReminderStatus]:
    return [compute(conn, r, settings, now) for r in repo.reminders(conn, enabled_only=True)]


# --- the tick ------------------------------------------------------------------------

def tick(conn: sqlite3.Connection, settings: Settings, notifier: Notifier, now: datetime) -> list[dict]:
    """Evaluate every enabled reminder once; send whatever is newly due."""
    sent = []
    for reminder in repo.reminders(conn, enabled_only=True):
        try:
            alert = _tick_one(conn, reminder, settings, notifier, now)
        except Exception:
            log.exception("reminder %s failed to evaluate", reminder["id"])
            continue
        if alert:
            sent.append(alert)
    return sent


def _tick_one(conn, reminder, settings, notifier, now) -> dict | None:
    status = compute(conn, reminder, settings, now)
    state = _state(conn, reminder["id"])
    anchor_key = to_db(status.anchor)
    if state["anchor"] != anchor_key:
        state = {**state, "anchor": anchor_key, "stage": "ok", "nag_count": 0, "snoozed_until": None}
        _save_state(conn, state)

    if status.in_progress:
        return None

    snoozed_until = from_db(state["snoozed_until"])
    if snoozed_until and now < snoozed_until:
        return None
    if not status.in_active_hours:
        # Leave the stored stage alone so the current stage is sent as soon
        # as the active window opens.
        return None

    if now < status.window_at:
        target = "ok"
    elif now < status.due_at:
        target = "window"
    else:
        target = "due"
    nag_minutes = int(reminder["nag_minutes"] or 0)
    overdue_min = max(0.0, (now - status.due_at).total_seconds() / 60.0)
    nag_index = int(math.floor(overdue_min / nag_minutes)) if nag_minutes > 0 and target == "due" else 0

    send_stage = None
    if snoozed_until and now >= snoozed_until:
        state["snoozed_until"] = None
        if target != "ok":
            send_stage = "overdue" if nag_index >= 1 else target
        state["nag_count"] = nag_index
        state["stage"] = target
    elif STAGE_RANK[target] > STAGE_RANK.get(state["stage"], 0):
        send_stage = "overdue" if nag_index >= 1 else target
        state["stage"] = target
        state["nag_count"] = nag_index
    elif target == "due" and nag_index > state["nag_count"] and nag_index <= int(reminder["max_nags"] or 0):
        send_stage = "overdue"
        state["nag_count"] = nag_index

    if send_stage is None:
        _save_state(conn, state)
        return None

    note = build_notification(status, send_stage, settings, now)
    state["last_notified_at"] = to_db(now)
    _save_state(conn, state)
    return notifier.deliver(conn, settings, note)


def snooze(conn: sqlite3.Connection, reminder: dict, settings: Settings, minutes: int, now: datetime) -> datetime:
    status = compute(conn, reminder, settings, now)
    state = _state(conn, reminder["id"])
    anchor_key = to_db(status.anchor)
    if state["anchor"] != anchor_key:
        state = {**state, "anchor": anchor_key, "stage": "ok", "nag_count": 0}
    until = now + timedelta(minutes=max(1, min(240, minutes)))
    state["snoozed_until"] = to_db(until)
    _save_state(conn, state)
    return until


# --- message text ---------------------------------------------------------------------

def _event_detail(kind: str, event: dict | None, settings: Settings) -> str:
    if not event:
        return ""
    if kind == "feeding":
        bits = [C.FEED_METHODS.get(event["method"], event["method"])]
        if event.get("side"):
            bits.append(C.FEED_SIDES.get(event["side"], event["side"]).lower())
        minutes = repo.feeding_minutes(event)
        if minutes is not None:
            bits.append(fmt_duration(minutes))
        if event.get("amount_ml"):
            bits.append(format_volume(event["amount_ml"], settings.volume_unit))
        if event.get("quality"):
            bits.append(C.FEED_QUALITY.get(event["quality"], "").lower())
        return ", ".join(b for b in bits if b)
    if kind.startswith("diaper"):
        bits = [C.DIAPER_KINDS.get(event["kind"], event["kind"])]
        if event.get("color"):
            bits.append(C.STOOL_COLORS.get(event["color"], event["color"]).lower())
        return ", ".join(bits)
    return ""


def format_volume(ml: float | None, unit: str) -> str:
    if ml is None:
        return ""
    if unit == "oz":
        return f"{ml / C.ML_PER_OZ:.1f} oz"
    return f"{ml:.0f} ml"


def build_notification(status: ReminderStatus, stage: str, settings: Settings, now: datetime) -> Notification:
    r = status.reminder
    tz = settings.tz
    level = settings.notify_detail
    name = r["name"]
    base_priority = int(r["priority"] or 3)
    priority = {"window": base_priority - 1, "due": base_priority, "overdue": base_priority + 1}[stage]
    tags = [NTFY_TAGS.get(r["kind"], "bell")]
    if stage == "overdue":
        tags.append("warning")

    overdue_for = fmt_duration(max(0, (now - status.due_at).total_seconds() / 60))
    if level == "minimal":
        title = "LilyPad reminder"
        message = {"window": "A routine's window is open.", "due": "A routine is due now.",
                   "overdue": "A routine is overdue."}[stage] + " Open LilyPad for details."
    else:
        title = {"window": f"{name} window open", "due": f"{name} due",
                 "overdue": f"{name} overdue ({overdue_for})"}[stage]
        if level == "detailed":
            title = f"{settings.baby_name}: {title[0].lower()}{title[1:]}"
        lines = []
        if stage == "window":
            lines.append(f"Due by {fmt_time(status.due_at, tz)}.")
        elif stage == "due":
            lines.append(f"Due now ({fmt_time(status.due_at, tz)}).")
        else:
            lines.append(f"Was due at {fmt_time(status.due_at, tz)}.")
        if status.anchor_from_event:
            verb = {"feeding": "Last feed started" if r.get("anchor") != "end" else "Last feed ended",
                    "custom": "Last done"}.get(r["kind"], "Last logged")
            lines.append(f"{verb} {fmt_time(status.anchor, tz)} ({fmt_ago(status.anchor, now)}).")
        else:
            lines.append("Nothing logged yet for this routine.")
        if level == "detailed":
            detail = _event_detail(r["kind"], status.last_event, settings)
            if detail:
                lines.append(detail[0].upper() + detail[1:] + ".")
        message = " ".join(lines)

    return Notification(
        title=title, message=message, source="reminder", priority=max(1, min(5, priority)),
        tags=tags, reminder_id=r["id"], stage=stage,
        context={"reminder": status.to_json()},
        to_ntfy=bool(r["notify_ntfy"]), to_dash=bool(r["notify_dash"]),
    )


# --- vital sign alerts ---------------------------------------------------------------

def vital_alerts(conn: sqlite3.Connection, settings: Settings, notifier: Notifier, check_id: int) -> list[dict]:
    """Notify immediately for readings at or beyond a parameter's alert limits."""
    check = repo.get(conn, "vital_checks", check_id)
    if not check:
        return []
    sent = []
    for reading in repo.readings_for_check(conn, check_id):
        if repo.reading_status(reading["value"], reading) != "alert":
            continue
        param = repo.vital_param(conn, reading["param_id"])
        if not param or not param["notify_on_alert"]:
            continue
        sent.append(notifier.deliver(conn, settings, vital_notification(param, reading, check, settings)))
    return sent


def vital_notification(param: dict, reading: dict, check: dict, settings: Settings) -> Notification:
    level = settings.notify_detail
    value = format_value(reading["value"], param)
    if reading["alert_high"] is not None and reading["value"] >= reading["alert_high"]:
        limit = f"at or above the alert limit of {format_value(reading['alert_high'], param)}"
    else:
        limit = f"at or below the alert limit of {format_value(reading['alert_low'], param)}"
    when = fmt_time(from_db(check["measured_at"]), settings.tz)
    if level == "minimal":
        title, message = "LilyPad alert", "A vital sign reading is outside its alert limits. Open LilyPad."
    else:
        title = f"{param['name']} alert: {value}"
        if level == "detailed":
            title = f"{settings.baby_name}: {title}"
        message = f"{param['name']} {value} is {limit}. Measured {when}."
        if level == "detailed" and check.get("notes"):
            message += f" Note: {check['notes'][:200]}"
    return Notification(
        title=title, message=message, source="vital", priority=5, tags=["rotating_light"],
        stage="alert", context={"param": param["name"], "value": reading["value"], "unit": param["unit"],
                                "check_id": check["id"]},
    )


def format_value(value: float | None, param: dict) -> str:
    if value is None:
        return ""
    decimals = max(0, min(4, int(param.get("decimals") or 0)))
    unit = param.get("unit") or ""
    return f"{value:.{decimals}f}" + (f" {unit}" if unit else "")

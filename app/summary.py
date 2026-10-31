"""Near-term snapshot shared by the home screen, /dash and /api/dash."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import constants as C
from . import repo
from .reminders import compute_all, format_value
from .settings_store import Settings
from .timeutil import fmt_age, from_db, to_db

RECENT_ALERT_HOURS = 12


def _feed_json(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "method": row["method"],
        "method_label": C.FEED_METHODS.get(row["method"], row["method"]),
        "side": row["side"],
        "side_label": C.FEED_SIDES.get(row["side"] or "", ""),
        "last_side": (last := repo.final_side(row)),
        "last_side_label": C.BREAST_SIDES.get(last or "", ""),
        "amount_ml": row["amount_ml"],
        "duration_min": round(m, 1) if (m := repo.feeding_minutes(row)) is not None else None,
        "quality": C.FEED_QUALITY.get(row["quality"]) if row["quality"] else None,
        "logged_by": row["logged_by"],
    }


def _diaper_json(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "occurred_at": row["occurred_at"],
        "kind": row["kind"],
        "kind_label": C.DIAPER_KINDS.get(row["kind"], row["kind"]),
        "color": row["color"],
        "color_label": C.STOOL_COLORS.get(row["color"] or "", ""),
        "consistency_label": C.STOOL_CONSISTENCY.get(row["consistency"] or "", ""),
        "flag": C.STOOL_COLOR_FLAGS.get(row["color"] or "") if row["kind"] in C.DIRTY_KINDS else None,
        "logged_by": row["logged_by"],
    }


def recent_alerts(conn: sqlite3.Connection, now: datetime, hours: int = RECENT_ALERT_HOURS) -> list[dict]:
    since = to_db(now - timedelta(hours=hours))
    return [dict(r) for r in conn.execute(
        "SELECT id, created_at, source, reminder_id, stage, title, message, priority, acknowledged_at "
        "FROM alerts WHERE created_at >= ? AND source != 'test' ORDER BY created_at DESC LIMIT 20",
        (since,),
    )]


def open_vital_alerts(conn: sqlite3.Connection, now: datetime, hours: int = 24) -> list[dict]:
    since = to_db(now - timedelta(hours=hours))
    return [dict(r) for r in conn.execute(
        "SELECT id, created_at, title, message, priority FROM alerts "
        "WHERE source = 'vital' AND acknowledged_at IS NULL AND created_at >= ? ORDER BY created_at DESC",
        (since,),
    )]


def latest_vitals(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for param in repo.vital_params(conn):
        reading = repo.latest_reading(conn, param["id"])
        item = {
            "param_id": param["id"],
            "name": param["name"],
            "unit": param["unit"],
            "value": None,
            "display": "",
            "measured_at": None,
            "status": None,
            "check_id": None,
        }
        if reading:
            item.update(
                value=reading["value"],
                display=format_value(reading["value"], param),
                measured_at=reading["measured_at"],
                status=repo.reading_status(reading["value"], reading),
                check_id=reading["check_id"],
            )
        out.append(item)
    return out


def snapshot(conn: sqlite3.Connection, settings: Settings, now: datetime) -> dict[str, Any]:
    since_24h = to_db(now - timedelta(hours=24))
    statuses = compute_all(conn, settings, now)
    active = repo.active_feeding(conn)
    last_feed = repo.last_feeding(conn, completed_only=True)
    today = now.astimezone(settings.tz).date()
    return {
        "now": to_db(now),
        "baby": {"name": settings.baby_name, "age": fmt_age(settings.birth_date, today)},
        "feeding": {
            "active": _feed_json(active),
            "last": _feed_json(last_feed),
            "suggested_side": repo.suggested_side(conn),
        },
        "diaper": {
            "last": _diaper_json(repo.last_diaper(conn)),
            "last_wet": _diaper_json(repo.last_diaper(conn, C.WET_KINDS)),
            "last_dirty": _diaper_json(repo.last_diaper(conn, C.DIRTY_KINDS)),
        },
        "last_24h": repo.counts_since(conn, since_24h),
        "volume_unit": settings.volume_unit,
        "reminders": [s.to_json() for s in statuses],
        "routines": [s.to_json() for s in statuses if s.reminder["kind"] == "custom"],
        "vitals": latest_vitals(conn),
        "vital_alerts": open_vital_alerts(conn, now),
        "alerts": recent_alerts(conn, now),
        "_statuses": statuses,  # stripped before JSON encoding
    }


def public_json(snap: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in snap.items() if not k.startswith("_")}


def is_stale_active(feed: dict | None, now: datetime) -> bool:
    if not feed:
        return False
    started = from_db(feed["started_at"])
    return started is not None and (now - started) > timedelta(minutes=C.STALE_FEED_MINUTES)

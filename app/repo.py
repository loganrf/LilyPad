"""Data access for logged entries.

Plain SQL over sqlite3. Deletes are soft (``deleted_at``) so a mis-tap at
3 a.m. can be undone, and the admin trash can restore anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import constants as C
from .timeutil import from_db, minutes_between, now_db, to_db


@dataclass(frozen=True)
class Entity:
    table: str
    time_col: str
    label: str
    slug: str  # URL segment for edit pages


ENTITIES: dict[str, Entity] = {
    "feedings": Entity("feedings", "started_at", "Feeding", "feed"),
    "diapers": Entity("diapers", "occurred_at", "Diaper", "diaper"),
    "notes": Entity("notes", "occurred_at", "Note", "note"),
    "vital_checks": Entity("vital_checks", "measured_at", "Vitals check", "vitals"),
    "task_logs": Entity("task_logs", "done_at", "Routine", "task"),
}

# Columns a form may write, per table.
WRITABLE = {
    "feedings": ("started_at", "ended_at", "method", "side", "last_side", "bottle_content", "amount_ml",
                 "quality", "issues", "notes", "logged_by"),
    "diapers": ("occurred_at", "kind", "color", "consistency", "amount", "notes", "logged_by"),
    "notes": ("occurred_at", "body", "tags", "for_pediatrician", "discussed", "logged_by"),
    "vital_checks": ("measured_at", "notes", "logged_by"),
    "task_logs": ("reminder_id", "task_name", "done_at", "notes", "logged_by"),
}


def entity(table: str) -> Entity:
    if table not in ENTITIES:
        raise KeyError(table)
    return ENTITIES[table]


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _row(cur: sqlite3.Cursor) -> dict[str, Any] | None:
    r = cur.fetchone()
    return dict(r) if r else None


# --- generic CRUD ------------------------------------------------------------------

def insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = [c for c in WRITABLE[table] if c in values]
    now = now_db()
    sql = "INSERT INTO {t} ({c}, created_at, updated_at) VALUES ({q}, ?, ?)".format(
        t=table, c=", ".join(cols), q=", ".join("?" for _ in cols)
    )
    cur = conn.execute(sql, [values[c] for c in cols] + [now, now])
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, entry_id: int, values: dict[str, Any]) -> None:
    cols = [c for c in WRITABLE[table] if c in values]
    if not cols:
        return
    sql = "UPDATE {t} SET {s}, updated_at = ? WHERE id = ?".format(
        t=table, s=", ".join(f"{c} = ?" for c in cols)
    )
    conn.execute(sql, [values[c] for c in cols] + [now_db(), entry_id])


def get(conn: sqlite3.Connection, table: str, entry_id: int, include_deleted: bool = False) -> dict | None:
    entity(table)
    sql = f"SELECT * FROM {table} WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return _row(conn.execute(sql, (entry_id,)))


def soft_delete(conn: sqlite3.Connection, table: str, entry_id: int) -> bool:
    entity(table)
    cur = conn.execute(
        f"UPDATE {table} SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now_db(), entry_id)
    )
    return cur.rowcount > 0


def restore(conn: sqlite3.Connection, table: str, entry_id: int) -> bool:
    entity(table)
    cur = conn.execute(
        f"UPDATE {table} SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL", (entry_id,)
    )
    return cur.rowcount > 0


def list_range(conn: sqlite3.Connection, table: str, since: str | None = None, until: str | None = None,
               order: str = "DESC", limit: int | None = None) -> list[dict]:
    ent = entity(table)
    where, args = ["deleted_at IS NULL"], []
    if since:
        where.append(f"{ent.time_col} >= ?")
        args.append(since)
    if until:
        where.append(f"{ent.time_col} < ?")
        args.append(until)
    sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY {ent.time_col} {order}, id {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _rows(conn.execute(sql, args))


def list_deleted(conn: sqlite3.Connection, since: str) -> list[dict]:
    out = []
    for ent in ENTITIES.values():
        for row in _rows(conn.execute(
            f"SELECT * FROM {ent.table} WHERE deleted_at IS NOT NULL AND deleted_at >= ? ORDER BY deleted_at DESC",
            (since,),
        )):
            row["_table"] = ent.table
            out.append(row)
    out.sort(key=lambda r: r["deleted_at"], reverse=True)
    return out


def purge_deleted(conn: sqlite3.Connection, before: str) -> int:
    total = 0
    for ent in ENTITIES.values():
        total += conn.execute(
            f"DELETE FROM {ent.table} WHERE deleted_at IS NOT NULL AND deleted_at < ?", (before,)
        ).rowcount
    return total


# --- feedings ------------------------------------------------------------------------

def active_feeding(conn: sqlite3.Connection) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM feedings WHERE deleted_at IS NULL AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1"
    ))


def last_feeding(conn: sqlite3.Connection, completed_only: bool = False) -> dict | None:
    sql = "SELECT * FROM feedings WHERE deleted_at IS NULL"
    if completed_only:
        sql += " AND ended_at IS NOT NULL"
    return _row(conn.execute(sql + " ORDER BY started_at DESC, id DESC LIMIT 1"))


def final_side(feed: dict | None) -> str | None:
    """The breast in use last during a feed (left/right), or None if unknown.

    That is the side it ended on, or the side in use now while it runs. A
    one-sided feed ended on its own side; a "both" feed records it in
    ``last_side``.
    """
    if not feed or feed.get("method") != "breast":
        return None
    for side in (feed.get("last_side"), feed.get("side")):
        if side in C.BREAST_SIDES:
            return side
    return None


def side_values(side: str | None, last_side: str | None = None) -> dict[str, str | None]:
    """Columns for a breastfeed's summary side and the side it ended on."""
    if side in C.BREAST_SIDES:
        last_side = side
    elif side != "both" or last_side not in C.BREAST_SIDES:
        last_side = None
    return {"side": side, "last_side": last_side}


def switch_side(feed: dict, side: str) -> dict[str, str | None]:
    """Values for changing breast during a running feed.

    Moving to the other breast makes the feed "both" in every summary, while
    ``last_side`` follows the breast in use so the next feed is suggested
    from the side reported last. Choosing "both" outright keeps whatever
    side is known to be in use.
    """
    current = final_side(feed)
    if side == "both":
        return {"method": "breast", **side_values("both", current)}
    if side == current:
        return {}
    used_before = feed.get("method") == "breast" and feed.get("side") in C.FEED_SIDES
    return {"method": "breast", **side_values("both" if used_before else side, side)}


def last_breast_side(conn: sqlite3.Connection) -> str | None:
    """The side the most recent breastfeed ended on (or is on now)."""
    row = _row(conn.execute(
        "SELECT method, side, last_side FROM feedings WHERE deleted_at IS NULL AND method = 'breast' "
        "AND side IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1"
    ))
    return final_side(row)


def suggested_side(conn: sqlite3.Connection) -> str | None:
    last = last_breast_side(conn)
    return {"left": "right", "right": "left"}.get(last or "")


def feeding_minutes(row: dict) -> float | None:
    return minutes_between(from_db(row.get("started_at")), from_db(row.get("ended_at")))


# --- diapers ---------------------------------------------------------------------------

def last_diaper(conn: sqlite3.Connection, kinds: Iterable[str] | None = None) -> dict | None:
    sql, args = "SELECT * FROM diapers WHERE deleted_at IS NULL", []
    if kinds:
        kinds = list(kinds)
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        args.extend(kinds)
    return _row(conn.execute(sql + " ORDER BY occurred_at DESC, id DESC LIMIT 1", args))


# --- vitals ------------------------------------------------------------------------------

def vital_params(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM vital_params"
    if active_only:
        sql += " WHERE active = 1"
    return _rows(conn.execute(sql + " ORDER BY sort_order, id"))


def vital_param(conn: sqlite3.Connection, param_id: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM vital_params WHERE id = ?", (param_id,)))


def readings_for_check(conn: sqlite3.Connection, check_id: int) -> list[dict]:
    return _rows(conn.execute(
        """SELECT r.*, p.name, p.unit, p.decimals FROM vital_readings r
           JOIN vital_params p ON p.id = r.param_id
           WHERE r.check_id = ? ORDER BY p.sort_order, p.id""",
        (check_id,),
    ))


def readings_in_range(conn: sqlite3.Connection, since: str | None, until: str | None,
                      param_id: int | None = None) -> list[dict]:
    where, args = ["c.deleted_at IS NULL"], []
    if since:
        where.append("c.measured_at >= ?")
        args.append(since)
    if until:
        where.append("c.measured_at < ?")
        args.append(until)
    if param_id is not None:
        where.append("r.param_id = ?")
        args.append(param_id)
    return _rows(conn.execute(
        f"""SELECT r.*, c.measured_at, c.notes AS check_notes, c.logged_by,
                   p.name, p.unit, p.decimals
            FROM vital_readings r
            JOIN vital_checks c ON c.id = r.check_id
            JOIN vital_params p ON p.id = r.param_id
            WHERE {' AND '.join(where)}
            ORDER BY c.measured_at ASC, p.sort_order""",
        args,
    ))


def latest_reading(conn: sqlite3.Connection, param_id: int) -> dict | None:
    return _row(conn.execute(
        """SELECT r.*, c.measured_at, c.id AS check_id FROM vital_readings r
           JOIN vital_checks c ON c.id = r.check_id
           WHERE r.param_id = ? AND c.deleted_at IS NULL
           ORDER BY c.measured_at DESC, c.id DESC LIMIT 1""",
        (param_id,),
    ))


def save_readings(conn: sqlite3.Connection, check_id: int, values: dict[int, float]) -> None:
    """Replace a check's readings, snapshotting each parameter's current limits."""
    existing = {r["param_id"]: r for r in _rows(conn.execute(
        "SELECT * FROM vital_readings WHERE check_id = ?", (check_id,)))}
    for param_id, value in values.items():
        prev = existing.pop(param_id, None)
        if prev is not None:
            conn.execute("UPDATE vital_readings SET value = ? WHERE id = ?", (value, prev["id"]))
            continue
        param = vital_param(conn, param_id)
        if param is None:
            continue
        conn.execute(
            """INSERT INTO vital_readings
               (check_id, param_id, value, expected_min, expected_max, alert_low, alert_high)
               VALUES (?,?,?,?,?,?,?)""",
            (check_id, param_id, value, param["expected_min"], param["expected_max"],
             param["alert_low"], param["alert_high"]),
        )
    for leftover in existing.values():
        conn.execute("DELETE FROM vital_readings WHERE id = ?", (leftover["id"],))


def reading_status(value: float, limits: dict) -> str:
    """'alert' (at/beyond an alert limit), 'watch' (outside expected) or 'ok'."""
    low, high = limits.get("alert_low"), limits.get("alert_high")
    if (low is not None and value <= low) or (high is not None and value >= high):
        return "alert"
    emin, emax = limits.get("expected_min"), limits.get("expected_max")
    if (emin is not None and value < emin) or (emax is not None and value > emax):
        return "watch"
    if all(limits.get(k) is None for k in ("alert_low", "alert_high", "expected_min", "expected_max")):
        return "none"
    return "ok"


def last_vital_check(conn: sqlite3.Connection, param_id: int | None = None) -> dict | None:
    if param_id is None:
        return _row(conn.execute(
            "SELECT * FROM vital_checks WHERE deleted_at IS NULL ORDER BY measured_at DESC LIMIT 1"
        ))
    return _row(conn.execute(
        """SELECT c.* FROM vital_checks c JOIN vital_readings r ON r.check_id = c.id
           WHERE c.deleted_at IS NULL AND r.param_id = ?
           ORDER BY c.measured_at DESC LIMIT 1""",
        (param_id,),
    ))


# --- notes ----------------------------------------------------------------------------------

def all_tags(conn: sqlite3.Connection) -> list[str]:
    tags: dict[str, int] = {}
    for row in conn.execute("SELECT tags FROM notes WHERE deleted_at IS NULL AND tags != ''"):
        for tag in split_tags(row["tags"]):
            tags[tag] = tags.get(tag, 0) + 1
    return sorted(tags, key=lambda t: (-tags[t], t))


def split_tags(raw: str | None) -> list[str]:
    seen, out = set(), []
    for tag in (raw or "").replace(";", ",").split(","):
        tag = tag.strip().lower()[:40]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def pediatrician_notes(conn: sqlite3.Connection, include_discussed: bool = False) -> list[dict]:
    sql = "SELECT * FROM notes WHERE deleted_at IS NULL AND for_pediatrician = 1"
    if not include_discussed:
        sql += " AND discussed = 0"
    return _rows(conn.execute(sql + " ORDER BY occurred_at DESC"))


# --- reminders ------------------------------------------------------------------------------

def reminders(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM reminders"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return _rows(conn.execute(sql + " ORDER BY sort_order, id"))


def reminder(conn: sqlite3.Connection, reminder_id: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)))


def last_task_done(conn: sqlite3.Connection, reminder_id: int) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM task_logs WHERE deleted_at IS NULL AND reminder_id = ? "
        "ORDER BY done_at DESC LIMIT 1",
        (reminder_id,),
    ))


# --- counts for summaries -----------------------------------------------------------------

def counts_since(conn: sqlite3.Connection, since: str, until: str | None = None) -> dict[str, Any]:
    until = until or "9999"
    feeds = _rows(conn.execute(
        "SELECT * FROM feedings WHERE deleted_at IS NULL AND started_at >= ? AND started_at < ?",
        (since, until),
    ))
    diapers = _rows(conn.execute(
        "SELECT kind FROM diapers WHERE deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?",
        (since, until),
    ))
    minutes = [m for m in (feeding_minutes(f) for f in feeds) if m is not None]
    return {
        "feeds": len(feeds),
        "feed_minutes": sum(minutes),
        "bottle_ml": sum(f["amount_ml"] or 0 for f in feeds if f["method"] == "bottle"),
        "wet": sum(1 for d in diapers if d["kind"] in C.WET_KINDS),
        "dirty": sum(1 for d in diapers if d["kind"] in C.DIRTY_KINDS),
        "diapers": len(diapers),
    }


def window_start(now: datetime, hours: int) -> str:
    return to_db(now - timedelta(hours=hours))

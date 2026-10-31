"""Bulk exports (admin only): CSV per data type, a JSON backup and a raw
SQLite snapshot."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from . import constants as C
from . import repo
from .settings_store import SECRET_KEYS, Settings, load_raw
from .timeutil import from_db, now_db

CSV_EXPORTS = ("feedings", "diapers", "vitals", "notes", "routines")


def _safe_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection.

    Notes are typed by anyone with access to the open pages; a cell starting
    with = + - @ (or tab/CR) would be evaluated by Excel/LibreOffice.
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _local(value: str | None, settings: Settings) -> str:
    dt = from_db(value)
    return dt.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M") if dt else ""


def _csv(header: list[str], rows: Iterable[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe_cell(v) for v in row])
    return buf.getvalue()


def export_csv(conn: sqlite3.Connection, settings: Settings, name: str, since: str | None, until: str | None) -> str:
    tzname = settings.timezone_name
    if name == "feedings":
        rows = repo.list_range(conn, "feedings", since, until, order="ASC")
        return _csv(
            ["id", f"started ({tzname})", f"ended ({tzname})", "started_utc", "duration_min", "method", "side",
             "last_side", "bottle_content", "amount_ml", "quality", "issues", "notes", "logged_by"],
            ([r["id"], _local(r["started_at"], settings), _local(r["ended_at"], settings), r["started_at"],
              round(m, 1) if (m := repo.feeding_minutes(r)) is not None else "",
              r["method"], r["side"] or "", repo.final_side(r) or "", r["bottle_content"] or "",
              r["amount_ml"] if r["amount_ml"] is not None else "",
              C.FEED_QUALITY.get(r["quality"], "") if r["quality"] else "",
              r["issues"], r["notes"], r["logged_by"]] for r in rows),
        )
    if name == "diapers":
        rows = repo.list_range(conn, "diapers", since, until, order="ASC")
        return _csv(
            ["id", f"time ({tzname})", "time_utc", "kind", "color", "consistency", "amount", "notes", "logged_by"],
            ([r["id"], _local(r["occurred_at"], settings), r["occurred_at"], r["kind"], r["color"] or "",
              r["consistency"] or "", r["amount"] or "", r["notes"], r["logged_by"]] for r in rows),
        )
    if name == "vitals":
        rows = repo.readings_in_range(conn, since, until)
        return _csv(
            ["check_id", f"time ({tzname})", "time_utc", "parameter", "value", "unit", "status",
             "expected_min", "expected_max", "alert_low", "alert_high", "notes", "logged_by"],
            ([r["check_id"], _local(r["measured_at"], settings), r["measured_at"], r["name"], r["value"],
              r["unit"], repo.reading_status(r["value"], r),
              *("" if r[k] is None else r[k] for k in ("expected_min", "expected_max", "alert_low", "alert_high")),
              r["check_notes"], r["logged_by"]] for r in rows),
        )
    if name == "notes":
        rows = repo.list_range(conn, "notes", since, until, order="ASC")
        return _csv(
            ["id", f"time ({tzname})", "time_utc", "for_pediatrician", "discussed", "tags", "body", "logged_by"],
            ([r["id"], _local(r["occurred_at"], settings), r["occurred_at"], r["for_pediatrician"],
              r["discussed"], r["tags"], r["body"], r["logged_by"]] for r in rows),
        )
    if name == "routines":
        rows = repo.list_range(conn, "task_logs", since, until, order="ASC")
        return _csv(
            ["id", f"time ({tzname})", "time_utc", "routine", "notes", "logged_by"],
            ([r["id"], _local(r["done_at"], settings), r["done_at"], r["task_name"], r["notes"], r["logged_by"]]
             for r in rows),
        )
    raise KeyError(name)


def export_json(conn: sqlite3.Connection) -> str:
    """Everything except secrets (password hash, signing key, ntfy/webhook credentials)."""
    tables = ["feedings", "diapers", "notes", "vital_params", "vital_checks", "vital_readings",
              "reminders", "task_logs", "alerts"]
    data: dict[str, Any] = {
        "format": "lilypad-backup",
        "version": 1,
        "app_version": __version__,
        "exported_at": now_db(),
        "settings": {k: v for k, v in load_raw(conn).items() if k not in SECRET_KEYS},
    }
    for table in tables:
        data[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    return json.dumps(data, indent=2, ensure_ascii=False)


def sqlite_snapshot(conn: sqlite3.Connection) -> Path:
    """A consistent copy of the live database (safe while the app is running)."""
    tmp = tempfile.NamedTemporaryFile(prefix="lilypad-", suffix=".sqlite3", delete=False)
    tmp.close()
    dest = sqlite3.connect(tmp.name)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return Path(tmp.name)

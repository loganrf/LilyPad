"""One-line descriptions of logged entries for timelines, history and trash."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import constants as C
from . import repo
from .reminders import format_value, format_volume
from .settings_store import Settings
from .timeutil import fmt_duration


def describe(conn: sqlite3.Connection, table: str, row: dict, settings: Settings) -> dict[str, Any]:
    ent = repo.entity(table)
    item: dict[str, Any] = {
        "table": table,
        "id": row["id"],
        "time": row[ent.time_col],
        "kind": table,
        "icon": "",
        "title": ent.label,
        "detail": "",
        "flag": None,
        "status": None,
        "logged_by": row.get("logged_by") or "",
        "edit_url": f"/{ent.slug}/{row['id']}/edit",
        "deleted_at": row.get("deleted_at"),
    }
    if table == "feedings":
        item["icon"] = "feed"
        method = C.FEED_METHODS.get(row["method"], row["method"])
        side = C.FEED_SIDES.get(row["side"] or "")
        item["title"] = f"{method} · {side}" if side and row["method"] == "breast" else method
        bits = []
        minutes = repo.feeding_minutes(row)
        if row["ended_at"] is None:
            bits.append("in progress")
            item["status"] = "active"
        elif minutes is not None and minutes > 0:
            bits.append(fmt_duration(minutes))
        if row["amount_ml"]:
            content = C.BOTTLE_CONTENTS.get(row["bottle_content"] or "", "")
            bits.append(format_volume(row["amount_ml"], settings.volume_unit) + (f" {content.lower()}" if content else ""))
        if row["quality"]:
            bits.append(C.FEED_QUALITY.get(row["quality"], ""))
        issues = [C.FEED_ISSUES.get(i, i) for i in (row["issues"] or "").split(",") if i]
        if issues:
            bits.append(", ".join(issues).lower())
        if row["notes"]:
            bits.append(f"“{_clip(row['notes'])}”")
        item["detail"] = " · ".join(b for b in bits if b)
    elif table == "diapers":
        item["icon"] = "diaper-" + row["kind"]
        item["title"] = C.DIAPER_KINDS.get(row["kind"], row["kind"]) + " diaper"
        bits = []
        if row["kind"] in C.DIRTY_KINDS:
            bits += [C.STOOL_COLORS.get(row["color"] or "", ""), C.STOOL_CONSISTENCY.get(row["consistency"] or "", "")]
            item["flag"] = C.STOOL_COLOR_FLAGS.get(row["color"] or "")
        if row["amount"]:
            bits.append(C.AMOUNTS.get(row["amount"], row["amount"]))
        if row["notes"]:
            bits.append(f"“{_clip(row['notes'])}”")
        item["detail"] = " · ".join(b for b in bits if b)
        item["status"] = "alert" if item["flag"] else None
    elif table == "notes":
        item["icon"] = "note"
        item["title"] = "Note for pediatrician" if row["for_pediatrician"] else "Note"
        item["detail"] = _clip(row["body"], 160)
        tags = repo.split_tags(row["tags"])
        if tags:
            item["detail"] += "  " + " ".join(f"#{t}" for t in tags)
        if row["for_pediatrician"]:
            item["status"] = "done" if row["discussed"] else "ped"
    elif table == "vital_checks":
        item["icon"] = "vitals"
        item["title"] = "Vitals"
        readings = repo.readings_for_check(conn, row["id"])
        parts, worst = [], "ok"
        rank = {"none": 0, "ok": 0, "watch": 1, "alert": 2}
        for r in readings:
            status = repo.reading_status(r["value"], r)
            if rank[status] > rank[worst]:
                worst = status
            mark = {"alert": " ‼", "watch": " !"}.get(status, "")
            parts.append(f"{r['name']} {format_value(r['value'], r)}{mark}")
        if row["notes"]:
            parts.append(f"“{_clip(row['notes'])}”")
        item["detail"] = " · ".join(parts)
        item["status"] = worst if worst != "ok" else None
    elif table == "task_logs":
        item["icon"] = "task"
        item["title"] = row["task_name"]
        item["detail"] = f"“{_clip(row['notes'])}”" if row["notes"] else "Done"
    return item


def _clip(text: str | None, length: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


def timeline(conn: sqlite3.Connection, settings: Settings, since: str | None, until: str | None = None,
             tables: list[str] | None = None, limit: int | None = None) -> list[dict]:
    items = []
    for table in tables or list(repo.ENTITIES):
        for row in repo.list_range(conn, table, since, until):
            items.append(describe(conn, table, row, settings))
    items.sort(key=lambda i: (i["time"], i["id"]), reverse=True)
    return items[:limit] if limit else items

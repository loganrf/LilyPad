"""Multi-day reporting (admin only): daily summaries, feeding intervals,
stool patterns, vitals trends and notes for the pediatrician.

Charts are rendered server-side as inline SVG so the report needs no
third-party JavaScript and prints cleanly.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any

from markupsafe import Markup, escape

from . import constants as C
from . import repo
from .reminders import format_value, format_volume
from .settings_store import Settings
from .timeutil import fmt_datetime, fmt_duration, fmt_time, from_db, local_day_bounds, to_db

MAX_REPORT_DAYS = 120


@dataclass
class DayStats:
    day: date
    feeds: int = 0
    feed_minutes: float = 0.0
    bottle_ml: float = 0.0
    qualities: list[int] = field(default_factory=list)
    wet: int = 0
    dirty: int = 0
    notes: int = 0
    vital_alerts: int = 0
    feed_spans: list[tuple[float, float, str, str]] = field(default_factory=list)  # start_min, end_min, method, tip
    diaper_marks: list[tuple[float, str, str]] = field(default_factory=list)  # minute, kind, tip

    @property
    def quality_avg(self) -> float | None:
        return mean(self.qualities) if self.qualities else None


def clamp_range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    end = end or today
    start = start or (end - timedelta(days=6))
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_REPORT_DAYS:
        start = end - timedelta(days=MAX_REPORT_DAYS - 1)
    return start, end


def build(conn: sqlite3.Connection, settings: Settings, start: date, end: date, now: datetime) -> dict[str, Any]:
    tz = settings.tz
    since = to_db(local_day_bounds(start, tz)[0])
    until = to_db(local_day_bounds(end, tz)[1])
    unit = settings.volume_unit

    days: dict[date, DayStats] = {}
    d = start
    while d <= end:
        days[d] = DayStats(d)
        d += timedelta(days=1)

    def local_minute(dt: datetime) -> tuple[date, float]:
        local = dt.astimezone(tz)
        return local.date(), local.hour * 60 + local.minute + local.second / 60

    feeds = repo.list_range(conn, "feedings", since, until, order="ASC")
    issues: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    for f in feeds:
        started = from_db(f["started_at"])
        day, start_min = local_minute(started)
        stats = days.get(day)
        if stats is None:
            continue
        minutes = repo.feeding_minutes(f)
        stats.feeds += 1
        stats.feed_minutes += minutes or 0
        if f["method"] == "bottle" and f["amount_ml"]:
            stats.bottle_ml += f["amount_ml"]
        if f["quality"]:
            stats.qualities.append(f["quality"])
        for issue in (f["issues"] or "").split(","):
            if issue:
                issues[issue] += 1
        methods[f["method"]] += 1
        tip = f"{fmt_time(started, tz)} · {C.FEED_METHODS.get(f['method'], f['method'])}"
        if f["side"]:
            tip += f" ({C.FEED_SIDES.get(f['side'], f['side']).lower()})"
        if minutes:
            tip += f" · {fmt_duration(minutes)}"
        if f["amount_ml"]:
            tip += f" · {format_volume(f['amount_ml'], unit)}"
        # Draw at least a 6-minute sliver so instant entries stay visible; split across midnight.
        span = max(minutes or 0, 6)
        end_min = start_min + span
        stats.feed_spans.append((start_min, min(end_min, 1440), f["method"], tip))
        if end_min > 1440 and (nxt := days.get(day + timedelta(days=1))):
            nxt.feed_spans.append((0, min(end_min - 1440, 1440), f["method"], tip + " (cont.)"))

    # Intervals between consecutive feed starts.
    starts = [from_db(f["started_at"]) for f in feeds]
    intervals = [(b - a).total_seconds() / 60 for a, b in zip(starts, starts[1:])]
    longest = None
    if intervals:
        idx = max(range(len(intervals)), key=lambda i: intervals[i])
        longest = {"minutes": intervals[idx], "from": feeds[idx]["started_at"], "to": feeds[idx + 1]["started_at"]}

    diapers = repo.list_range(conn, "diapers", since, until, order="ASC")
    colors: Counter[str] = Counter()
    consistencies: Counter[str] = Counter()
    flagged_diapers = []
    for dp in diapers:
        occurred = from_db(dp["occurred_at"])
        day, minute = local_minute(occurred)
        stats = days.get(day)
        if stats is None:
            continue
        if dp["kind"] in C.WET_KINDS:
            stats.wet += 1
        if dp["kind"] in C.DIRTY_KINDS:
            stats.dirty += 1
            if dp["color"]:
                colors[dp["color"]] += 1
            if dp["consistency"]:
                consistencies[dp["consistency"]] += 1
            if dp["color"] in C.STOOL_COLOR_FLAGS:
                flagged_diapers.append(dp)
        tip = f"{fmt_time(occurred, tz)} · {C.DIAPER_KINDS.get(dp['kind'], dp['kind'])}"
        if dp["color"] and dp["kind"] in C.DIRTY_KINDS:
            tip += f" · {C.STOOL_COLORS.get(dp['color'], dp['color']).lower()}"
        stats.diaper_marks.append((minute, dp["kind"], tip))

    notes = repo.list_range(conn, "notes", since, until, order="ASC")
    for n in notes:
        day, _ = local_minute(from_db(n["occurred_at"]))
        if day in days:
            days[day].notes += 1

    # Vitals, grouped per parameter.
    readings = repo.readings_in_range(conn, since, until)
    vitals: dict[int, dict[str, Any]] = {}
    for r in readings:
        entry = vitals.setdefault(r["param_id"], {
            "param": repo.vital_param(conn, r["param_id"]), "readings": [], "counts": Counter(),
        })
        status = repo.reading_status(r["value"], r)
        entry["readings"].append({**r, "status": status})
        entry["counts"][status] += 1
        if status == "alert":
            day, _ = local_minute(from_db(r["measured_at"]))
            if day in days:
                days[day].vital_alerts += 1
    vital_list = []
    for entry in sorted(vitals.values(), key=lambda e: (e["param"]["sort_order"], e["param"]["id"])):
        values = [r["value"] for r in entry["readings"]]
        entry["min"], entry["max"], entry["avg"] = min(values), max(values), mean(values)
        entry["chart"] = vitals_chart(entry["param"], entry["readings"], since, until, tz)
        vital_list.append(entry)

    tasks = repo.list_range(conn, "task_logs", since, until, order="ASC")
    task_counts = Counter(t["task_name"] for t in tasks)

    day_list = list(days.values())
    n_days = len(day_list)
    total_feeds = sum(s.feeds for s in day_list)
    # Average over days since tracking began, so a range that starts before
    # the first entry doesn't dilute the per-day figures.
    first = first_entry_day(conn, tz)
    tracked_start = max(start, first) if first else start
    tracked_days = max(1, (end - tracked_start).days + 1) if first and first <= end else n_days
    return {
        "start": start,
        "end": end,
        "n_days": n_days,
        "tracked_days": tracked_days,
        "tracked_start": tracked_start,
        "days": day_list,
        "totals": {
            "feeds": total_feeds,
            "feeds_per_day": total_feeds / tracked_days,
            "feed_minutes_per_day": sum(s.feed_minutes for s in day_list) / tracked_days,
            "avg_feed_minutes": (sum(s.feed_minutes for s in day_list) / total_feeds) if total_feeds else None,
            "bottle_ml_per_day": sum(s.bottle_ml for s in day_list) / tracked_days,
            "wet_per_day": sum(s.wet for s in day_list) / tracked_days,
            "dirty_per_day": sum(s.dirty for s in day_list) / tracked_days,
            "avg_interval": mean(intervals) if intervals else None,
            "min_interval": min(intervals) if intervals else None,
            "longest": longest,
        },
        "methods": methods,
        "issues": issues,
        "colors": colors,
        "consistencies": consistencies,
        "flagged_diapers": flagged_diapers,
        "vitals": vital_list,
        "notes": notes,
        "ped_notes": [n for n in notes if n["for_pediatrician"]],
        "open_ped_notes_outside": [n for n in repo.pediatrician_notes(conn)
                                   if not (since <= n["occurred_at"] < until)],
        "tasks": task_counts,
        "strip_chart": strip_chart(day_list),
        "generated": fmt_datetime(now, tz, now),
    }


def first_entry_day(conn: sqlite3.Connection, tz) -> date | None:
    firsts = []
    for table in repo.ENTITIES.values():
        row = conn.execute(f"SELECT MIN({table.time_col}) FROM {table.table} WHERE deleted_at IS NULL").fetchone()
        if row and row[0]:
            firsts.append(row[0])
    return from_db(min(firsts)).astimezone(tz).date() if firsts else None


# --- SVG charts ----------------------------------------------------------------------

def _tip(text: str) -> str:
    return f"<title>{escape(text)}</title>"


def strip_chart(days: list[DayStats]) -> Markup:
    """One row per day, 24 hours across: feed spans as bars, diapers as dots."""
    if not days:
        return Markup("")
    left, right, top = 92, 12, 26
    width = 760
    row_h = 36
    plot_w = width - left - right
    height = top + row_h * len(days) + 8
    x = lambda minute: left + plot_w * (minute / 1440)
    out = [f'<svg class="chart strip" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="24-hour activity by day: feeds as bars, diapers as dots">']
    for hour in range(0, 25, 3):
        xx = x(hour * 60)
        out.append(f'<line class="grid" x1="{xx:.1f}" y1="{top - 4}" x2="{xx:.1f}" y2="{height - 8}"/>')
        label = {0: "12a", 12: "12p", 24: "12a"}.get(hour, f"{hour % 12}{'a' if hour < 12 else 'p'}")
        out.append(f'<text class="axis" x="{xx:.1f}" y="{top - 10}" text-anchor="middle">{label}</text>')
    for i, stats in enumerate(days):
        y0 = top + i * row_h
        out.append(f'<text class="axis day" x="{left - 8}" y="{y0 + row_h / 2 + 4:.1f}" text-anchor="end">'
                   f'{stats.day.strftime("%a %b")} {stats.day.day}</text>')
        out.append(f'<line class="row" x1="{left}" y1="{y0 + row_h:.1f}" x2="{width - right}" y2="{y0 + row_h:.1f}"/>')
        for start_min, end_min, method, tip in stats.feed_spans:
            x1, x2 = x(start_min), x(end_min)
            w = max(3.0, x2 - x1)
            out.append(f'<rect class="mark feed feed-{escape(method)}" x="{x1:.1f}" y="{y0 + 4}" width="{w:.1f}" '
                       f'height="12" rx="2" tabindex="0" data-tip="{escape(tip)}">{_tip(tip)}</rect>')
        for minute, kind, tip in stats.diaper_marks:
            cx = x(minute)
            if kind in C.WET_KINDS:
                out.append(f'<circle class="mark wet" cx="{cx:.1f}" cy="{y0 + 22}" r="4" tabindex="0" '
                           f'data-tip="{escape(tip)}">{_tip(tip)}</circle>')
            if kind in C.DIRTY_KINDS:
                out.append(f'<rect class="mark dirty" x="{cx - 4:.1f}" y="{y0 + 27}" width="8" height="8" '
                           f'rx="1.5" tabindex="0" data-tip="{escape(tip)}">{_tip(tip)}</rect>')
            if kind == "dry":
                out.append(f'<circle class="mark dry" cx="{cx:.1f}" cy="{y0 + 22}" r="3.5" tabindex="0" '
                           f'data-tip="{escape(tip)}">{_tip(tip)}</circle>')
    out.append("</svg>")
    return Markup("".join(out))


def _nice_bounds(lo: float, hi: float) -> tuple[float, float, list[float]]:
    """Round axis bounds and ~5 evenly spaced ticks. Never loops unboundedly."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, 1.0, [0.0, 1.0]
    if hi - lo <= max(abs(lo), abs(hi)) * 1e-9:
        pad = max(1.0, abs(hi) * 0.05)
        lo, hi = lo - pad, hi + pad
    raw_step = (hi - lo) / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw_step)
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    count = int(round((end - start) / step))
    if not 1 <= count <= 20:
        return lo, hi, [lo, hi]
    return start, end, [round(start + i * step, 6) for i in range(count + 1)]


def vitals_chart(param: dict, readings: list[dict], since: str, until: str, tz) -> Markup:
    if not readings:
        return Markup("")
    width, height = 760, 220
    left, right, top, bottom = 56, 16, 14, 30
    plot_w, plot_h = width - left - right, height - top - bottom
    t0, t1 = from_db(since), from_db(until)
    total = (t1 - t0).total_seconds() or 1

    values = [r["value"] for r in readings]
    refs = [v for v in (param.get("expected_min"), param.get("expected_max"),
                        param.get("alert_low"), param.get("alert_high")) if v is not None]
    lo, hi, ticks = _nice_bounds(min(values + refs), max(values + refs))
    xs = lambda dt: left + plot_w * ((dt - t0).total_seconds() / total)
    ys = lambda v: top + plot_h * (1 - (v - lo) / ((hi - lo) or 1))

    name = escape(param["name"])
    out = [f'<svg class="chart vitals" viewBox="0 0 {width} {height}" role="img" aria-label="{name} over time">']
    emin, emax = param.get("expected_min"), param.get("expected_max")
    if emin is not None or emax is not None:
        band_lo = emin if emin is not None else lo
        band_hi = emax if emax is not None else hi
        y_hi, y_lo = ys(band_hi), ys(band_lo)
        out.append(f'<rect class="band" x="{left}" y="{y_hi:.1f}" width="{plot_w}" height="{max(0, y_lo - y_hi):.1f}">'
                   f'{_tip("Expected range")}</rect>')
    for tick in ticks:
        y = ys(tick)
        out.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{tick:g}</text>')
    for key, label in (("alert_low", "alert ≤"), ("alert_high", "alert ≥")):
        v = param.get(key)
        if v is not None and lo <= v <= hi:
            y = ys(v)
            out.append(f'<line class="limit" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}"/>')
            out.append(f'<text class="limit-label" x="{left + 6}" y="{y - 4:.1f}" text-anchor="start">'
                       f'{label} {escape(format_value(v, param))}</text>')
    # Day ticks along the x axis (at most ~8 labels).
    days = max(1, round(total / 86400))
    every = max(1, round(days / 8))
    d = t0.astimezone(tz).date()
    i = 0
    while True:
        start_utc, _ = local_day_bounds(d, tz)
        if start_utc >= t1:
            break
        if i % every == 0:
            xx = xs(start_utc)
            out.append(f'<text class="axis" x="{xx:.1f}" y="{height - 10}" text-anchor="start">'
                       f'{d.strftime("%b")} {d.day}</text>')
        d += timedelta(days=1)
        i += 1
    pts = [(xs(from_db(r["measured_at"])), ys(r["value"]), r) for r in readings]
    if len(pts) > 1:
        path = " ".join(f"{'M' if j == 0 else 'L'}{px:.1f},{py:.1f}" for j, (px, py, _) in enumerate(pts))
        out.append(f'<path class="line" d="{path}"/>')
    for px, py, r in pts:
        tip = f"{fmt_datetime(from_db(r['measured_at']), tz)} · {format_value(r['value'], param)}"
        status = r.get("status") or "ok"
        if status in ("alert", "watch"):
            tip += " · " + ("outside alert limits" if status == "alert" else "outside expected range")
        out.append(f'<circle class="hit" cx="{px:.1f}" cy="{py:.1f}" r="12" tabindex="0" data-tip="{escape(tip)}">'
                   f'{_tip(tip)}</circle>')
        out.append(f'<circle class="dot status-{escape(status)}" cx="{px:.1f}" cy="{py:.1f}" r="4.5"/>')
    out.append("</svg>")
    return Markup("".join(out))

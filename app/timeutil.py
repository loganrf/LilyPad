"""Time helpers.

All timestamps are stored as UTC strings in a fixed format
(``YYYY-MM-DDTHH:MM:SSZ``) so SQLite can compare them lexically. Everything
shown to people is converted to the household time zone configured in
settings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to to_db")
    return dt.astimezone(UTC).strftime(FMT)


def from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, FMT).replace(tzinfo=UTC)


def now_db() -> str:
    return to_db(utcnow())


def get_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_local_input(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse a ``<input type="datetime-local">`` value in the household zone."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    return naive.replace(tzinfo=tz).astimezone(UTC).replace(microsecond=0)


def to_local_input(value: str | datetime | None, tz: ZoneInfo) -> str:
    dt = from_db(value) if isinstance(value, str) else value
    if dt is None:
        return ""
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC start/end of a calendar day in the household zone."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def fmt_time(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(tz)
    return local.strftime("%I:%M %p").lstrip("0")


def fmt_datetime(dt: datetime | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or utcnow()
    local = dt.astimezone(tz)
    today = now.astimezone(tz).date()
    time_part = fmt_time(dt, tz)
    if local.date() == today:
        return f"Today {time_part}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {time_part}"
    return f"{local.strftime('%a %b')} {local.day}, {time_part}"


def fmt_duration(minutes: float | int | None) -> str:
    if minutes is None:
        return ""
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {mins:02d}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def fmt_ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    now = now or utcnow()
    mins = minutes_between(dt, now) or 0
    if mins < 0:
        return "in " + fmt_duration(-mins)
    if mins < 1:
        return "just now"
    return fmt_duration(mins) + " ago"


def fmt_age(birth: date | None, today: date) -> str:
    """Human-friendly baby age: '5 days', '3 weeks 2 days', '4 months 1 week'."""
    if birth is None or birth > today:
        return ""
    days = (today - birth).days
    if days == 0:
        return "born today"
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 90:
        weeks, rem = divmod(days, 7)
        out = f"{weeks} weeks"
        return out + (f" {rem} day{'s' if rem != 1 else ''}" if rem else "")
    months = (today.year - birth.year) * 12 + today.month - birth.month
    if today.day < birth.day:
        months -= 1
    anchor_month = birth.month + months
    anchor = date(birth.year + (anchor_month - 1) // 12, (anchor_month - 1) % 12 + 1, 1)
    try:
        anchor = anchor.replace(day=birth.day)
    except ValueError:
        # Born on the 31st, anchor month is shorter: clamp to the 1st of next.
        anchor = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
    weeks = max(0, (today - anchor).days) // 7
    if months < 24:
        out = f"{months} month{'s' if months != 1 else ''}"
        return out + (f" {weeks} week{'s' if weeks != 1 else ''}" if weeks else "")
    years, months = divmod(months, 12)
    return f"{years} years" + (f" {months} months" if months else "")


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        hh, mm = value.split(":")[:2]
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h, m
    return None


def within_daily_window(now: datetime, tz: ZoneInfo, start: str | None, end: str | None) -> bool:
    """True if ``now`` falls inside a daily HH:MM window (wrapping midnight)."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s is None or e is None or s == e:
        return True
    local = now.astimezone(tz)
    cur = local.hour * 60 + local.minute
    s_min, e_min = s[0] * 60 + s[1], e[0] * 60 + e[1]
    if s_min < e_min:
        return s_min <= cur < e_min
    return cur >= s_min or cur < e_min

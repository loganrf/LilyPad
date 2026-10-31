from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.timeutil import fmt_age, fmt_duration, parse_local_input, within_daily_window


def test_fmt_age():
    assert fmt_age(date(2026, 10, 28), date(2026, 10, 28)) == "born today"
    assert fmt_age(date(2026, 10, 20), date(2026, 10, 28)) == "8 days"
    assert fmt_age(date(2026, 10, 1), date(2026, 10, 28)) == "3 weeks 6 days"
    assert fmt_age(date(2026, 6, 15), date(2026, 10, 28)) == "4 months 1 week"
    assert fmt_age(date(2026, 1, 31), date(2026, 5, 2)) == "3 months"
    assert fmt_age(date(2024, 1, 1), date(2026, 3, 1)) == "2 years 2 months"
    assert fmt_age(None, date(2026, 1, 1)) == ""


def test_fmt_duration():
    assert fmt_duration(5) == "5m"
    assert fmt_duration(65) == "1h 05m"
    assert fmt_duration(120) == "2h"
    assert fmt_duration(60 * 50) == "2d 2h"


def test_parse_local_input_uses_household_zone():
    dt = parse_local_input("2026-10-28T09:30", ZoneInfo("America/New_York"))
    assert dt == datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)
    assert parse_local_input("nonsense", ZoneInfo("UTC")) is None


def test_daily_window_wraps_midnight():
    tz = ZoneInfo("UTC")
    late = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert within_daily_window(late, tz, "22:00", "06:00")
    assert not within_daily_window(noon, tz, "22:00", "06:00")
    assert within_daily_window(noon, tz, "07:00", "22:00")
    assert within_daily_window(noon, tz, None, None)

"""Household settings stored in the database and edited from /admin.

Values are stored as strings; ``Settings`` exposes typed accessors.
Keys listed in SECRET_KEYS are never rendered back into HTML and are left
out of JSON exports.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from .timeutil import get_zone, parse_date

DEFAULTS: dict[str, str] = {
    "baby_name": "Lily",
    "birth_date": "",
    "timezone": "",  # empty -> deployment default (TZ env var)
    "volume_unit": "ml",
    "caregivers": "",
    "open_window_hours": "48",
    "base_url": "",
    # ntfy
    "ntfy_enabled": "0",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "ntfy_token": "",
    "ntfy_username": "",
    "ntfy_password": "",
    "notify_detail": "standard",  # minimal | standard | detailed
    "ntfy_actions": "1",
    # dash
    "dash_webhook_enabled": "0",
    "dash_webhook_url": "",
    "dash_webhook_headers": "",
    "dash_sound": "1",
    # Dash LED clocks (github.com/loganrf/dash)
    "dash_clock_enabled": "0",
    "dash_clock_urls": "",
    "dash_clock_headers": "",
    "dash_clock_text": "Check {baby}",
    "dash_clock_color": "#FF4080",
    "dash_clock_minutes": "10",
    "dash_clock_skip_window": "1",
    # internal
    "admin_password_hash": "",
    "secret_key": "",
}

SECRET_KEYS = frozenset(
    {"ntfy_token", "ntfy_password", "dash_webhook_headers", "dash_clock_headers",
     "admin_password_hash", "secret_key"}
)

OPEN_WINDOW_MIN_HOURS = 24
OPEN_WINDOW_MAX_HOURS = 24 * 30


def load_raw(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def save(conn: sqlite3.Connection, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def _flag(value: str) -> bool:
    return value in ("1", "true", "on", "yes")


@dataclass
class Settings:
    raw: dict[str, str]
    default_timezone: str = "UTC"

    def __getitem__(self, key: str) -> str:
        return self.raw.get(key, DEFAULTS.get(key, ""))

    def flag(self, key: str) -> bool:
        return _flag(self[key])

    @property
    def baby_name(self) -> str:
        return self["baby_name"].strip() or "Baby"

    @property
    def birth_date(self) -> date | None:
        return parse_date(self["birth_date"])

    @property
    def timezone_name(self) -> str:
        return self["timezone"].strip() or self.default_timezone

    @property
    def tz(self) -> ZoneInfo:
        return get_zone(self.timezone_name)

    @property
    def volume_unit(self) -> str:
        return "oz" if self["volume_unit"] == "oz" else "ml"

    @property
    def caregivers(self) -> list[str]:
        return [c.strip() for c in self["caregivers"].split(",") if c.strip()]

    @property
    def open_window_hours(self) -> int:
        try:
            hours = int(self["open_window_hours"])
        except ValueError:
            hours = 48
        return max(OPEN_WINDOW_MIN_HOURS, min(OPEN_WINDOW_MAX_HOURS, hours))

    @property
    def notify_detail(self) -> str:
        value = self["notify_detail"]
        return value if value in ("minimal", "standard", "detailed") else "standard"

    @property
    def base_url(self) -> str:
        return self["base_url"].strip().rstrip("/")

    @property
    def dash_clock_minutes(self) -> int:
        try:
            minutes = int(self["dash_clock_minutes"])
        except ValueError:
            minutes = 10
        return max(0, min(30, minutes))

    @property
    def dash_clock_ready(self) -> bool:
        return self.flag("dash_clock_enabled") and self.is_set("dash_clock_urls")

    def is_set(self, key: str) -> bool:
        return bool(self[key].strip())


def load(conn: sqlite3.Connection, default_timezone: str = "UTC") -> Settings:
    return Settings(load_raw(conn), default_timezone=default_timezone)

"""Small, forgiving form parsing helpers.

Forms are filled one-handed at 3 a.m., so parsing is lenient (blank means
"not recorded") and validation messages are short and specific.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .timeutil import parse_local_input


class FormError(ValueError):
    pass


class Form:
    def __init__(self, data: Mapping, multi: Mapping[str, list[str]] | None = None):
        self.data = data
        self._multi = multi or {}
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise FormError(" ".join(self.errors))

    def text(self, name: str, max_len: int = 2000) -> str:
        value = self.data.get(name) or ""
        if not isinstance(value, str):
            return ""
        value = value.replace("\r\n", "\n").strip()
        return value[:max_len]

    def choice(self, name: str, choices: Iterable, label: str = "", required: bool = False):
        raw = self.text(name, 100)
        options = {str(c): c for c in choices}
        if not raw:
            if required:
                self.error(f"Choose {label or name}.")
            return None
        if raw not in options:
            self.error(f"Unknown {label or name}.")
            return None
        return options[raw]

    def multi(self, name: str, choices: Iterable[str]) -> list[str]:
        allowed = set(choices)
        values = self._multi.get(name, [])
        return [v for v in dict.fromkeys(values) if v in allowed]

    def number(self, name: str, label: str = "", minimum: float | None = None,
               maximum: float | None = None, required: bool = False) -> float | None:
        raw = self.text(name, 50).replace(",", ".")
        if not raw:
            if required:
                self.error(f"Enter {label or name}.")
            return None
        try:
            value = float(raw)
        except ValueError:
            self.error(f"{label or name} must be a number.")
            return None
        if value != value or value in (float("inf"), float("-inf")):
            self.error(f"{label or name} must be a number.")
            return None
        if minimum is not None and value < minimum:
            self.error(f"{label or name} must be at least {minimum:g}.")
            return None
        if maximum is not None and value > maximum:
            self.error(f"{label or name} must be at most {maximum:g}.")
            return None
        return value

    def integer(self, name: str, label: str = "", minimum: int | None = None,
                maximum: int | None = None, required: bool = False) -> int | None:
        value = self.number(name, label, minimum, maximum, required)
        if value is None:
            return None
        if value != int(value):
            self.error(f"{label or name} must be a whole number.")
            return None
        return int(value)

    def flag(self, name: str) -> bool:
        return self.text(name, 10).lower() in ("1", "on", "true", "yes")

    def when(self, prefix: str, tz: ZoneInfo, now: datetime, label: str = "time",
             required: bool = True) -> datetime | None:
        """Read a when-picker: ``<prefix>_ago`` chips or ``<prefix>_at`` datetime."""
        ago = self.text(f"{prefix}_ago", 10)
        at = self.text(f"{prefix}_at", 30)
        if at and ago in ("", "custom"):
            value = parse_local_input(at, tz)
            if value is None:
                self.error(f"Couldn't read the {label}.")
            elif value > now + timedelta(minutes=5):
                self.error(f"The {label} is in the future.")
                return None
            return value
        if ago and ago != "custom":
            try:
                minutes = max(0, min(24 * 60, int(ago)))
            except ValueError:
                self.error(f"Couldn't read the {label}.")
                return None
            return now - timedelta(minutes=minutes)
        if required:
            self.error(f"Enter the {label}.")
        return None

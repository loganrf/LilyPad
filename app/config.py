"""Deployment-level configuration, read once from environment variables.

Anything a parent would reasonably change day to day lives in the database
(see settings_store.py) and is edited from the password-protected admin
area. Environment variables are for things that belong to the deployment:
where data lives, the bootstrap admin password, and network hardening.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # If set, this password always unlocks the admin area and the in-app
    # "change password" form is disabled.
    admin_password: str = ""
    # Key used to sign admin session cookies. Generated and stored in the
    # database when not provided.
    secret_key: str = ""
    session_minutes: int = 30
    # Host header allowlist (DNS-rebinding protection). "*" disables the check.
    allowed_hosts: list[str] = field(default_factory=lambda: ["*"])
    # Extra origins allowed to submit forms (besides the app's own origin).
    allowed_origins: list[str] = field(default_factory=list)
    # "auto" marks cookies Secure when the request arrived over HTTPS.
    cookie_secure: str = "auto"
    # Who may embed /dash in an iframe (CSP frame-ancestors value).
    dash_frame_ancestors: str = "*"
    reminder_tick_seconds: int = 30
    default_timezone: str = "UTC"
    # Set to 0 to disable the background reminder loop (used by tests).
    scheduler_enabled: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lilypad.sqlite3"

    @property
    def password_file(self) -> Path:
        """Where a generated first-run admin password is written."""
        return self.data_dir / "INITIAL_ADMIN_PASSWORD.txt"


def load_config() -> Config:
    data_dir = Path(_env("LILYPAD_DATA_DIR", "./data")).resolve()
    cookie_secure = _env("LILYPAD_COOKIE_SECURE", "auto").lower()
    if cookie_secure not in ("auto", "true", "false"):
        raise SystemExit("LILYPAD_COOKIE_SECURE must be auto, true or false")
    return Config(
        data_dir=data_dir,
        admin_password=os.environ.get("LILYPAD_ADMIN_PASSWORD", ""),
        secret_key=_env("LILYPAD_SECRET_KEY"),
        session_minutes=max(5, _env_int("LILYPAD_ADMIN_SESSION_MINUTES", 30)),
        allowed_hosts=_env_list("LILYPAD_ALLOWED_HOSTS", "*") or ["*"],
        allowed_origins=_env_list("LILYPAD_ALLOWED_ORIGINS"),
        cookie_secure=cookie_secure,
        dash_frame_ancestors=_env("LILYPAD_DASH_FRAME_ANCESTORS", "*") or "*",
        reminder_tick_seconds=max(5, _env_int("LILYPAD_REMINDER_TICK_SECONDS", 30)),
        default_timezone=_env("TZ", "UTC") or "UTC",
        scheduler_enabled=_env("LILYPAD_SCHEDULER", "1") not in ("0", "false", "no"),
    )

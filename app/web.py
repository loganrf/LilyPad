"""Shared plumbing for route modules: app state, DB dependency, templates,
admin-session checks and redirect/toast helpers."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path as FilePath
from typing import Annotated, Any, Iterator
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Path, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import __version__
from . import constants as C
from . import db, repo
from . import settings_store
from .config import Config
from .events import EventBus
from .notify import Notifier
from .reminders import format_value, format_volume
from .security import AdminSession, LoginThrottle, SessionSigner, password_fingerprint
from .settings_store import Settings
from .timeutil import (
    fmt_age, fmt_ago, fmt_datetime, fmt_duration, fmt_time, from_db, to_db, to_local_input, utcnow,
)

ADMIN_COOKIE = "lilypad_admin"
WHO_COOKIE = "lilypad_who"
TEMPLATE_DIR = FilePath(__file__).parent / "templates"
STATIC_DIR = FilePath(__file__).parent / "static"

TOASTS = {
    "feed-started": ("Feed timer started.", "ok"),
    "feed-stopped": ("Feed stopped and saved. Add details below if you like.", "ok"),
    "feed-switched": ("Switched sides. This feed will be logged as both.", "ok"),
    "feed-saved": ("Feed saved.", "ok"),
    "feed-active": ("A feed is already running. Stop it first.", "warn"),
    "diaper-saved": ("Diaper logged.", "ok"),
    "vitals-saved": ("Vitals saved.", "ok"),
    "vitals-alert": ("Vitals saved. A reading is outside its alert limits.", "alert"),
    "vitals-watch": ("Vitals saved. A reading is outside its expected range.", "warn"),
    "note-saved": ("Note saved.", "ok"),
    "task-done": ("Marked done.", "ok"),
    "deleted": ("Entry deleted.", "ok"),
    "restored": ("Entry restored.", "ok"),
    "undone": ("Undone.", "ok"),
    "snoozed": ("Reminder snoozed.", "ok"),
    "acknowledged": ("Alert acknowledged.", "ok"),
    "settings-saved": ("Settings saved.", "ok"),
    "password-changed": ("Password changed. Other admin sessions were signed out.", "ok"),
    "logged-out": ("Admin area locked.", "ok"),
    "param-saved": ("Vital parameter saved.", "ok"),
    "param-deleted": ("Vital parameter deleted.", "ok"),
    "param-archived": ("Parameter has readings, so it was archived instead of deleted.", "warn"),
    "reminder-saved": ("Reminder saved.", "ok"),
    "reminder-deleted": ("Reminder deleted.", "ok"),
    "test-sent": ("Test notification sent. Check the delivery log below.", "ok"),
    "purged": ("Trash emptied.", "ok"),
    "who-saved": ("Saved for this device.", "ok"),
}


@dataclass
class AppState:
    config: Config
    bus: EventBus
    notifier: Notifier
    signer: SessionSigner
    throttle: LoginThrottle
    env_password_digest: bytes | None  # sha256 of LILYPAD_ADMIN_PASSWORD, if set


def state(request: Request) -> AppState:
    return request.app.state.lilypad


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(state(request).config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def load_settings(request: Request, conn: sqlite3.Connection) -> Settings:
    cached = getattr(request.state, "settings", None)
    if cached is None:
        cached = settings_store.load(conn, state(request).config.default_timezone)
        request.state.settings = cached
    return cached


# --- admin sessions ---------------------------------------------------------------

def current_fingerprint(request: Request, settings: Settings) -> str:
    st = state(request)
    if st.env_password_digest is not None:
        return password_fingerprint("env:" + st.env_password_digest.hex())
    return password_fingerprint(settings["admin_password_hash"])


def check_admin_password(request: Request, settings: Settings, password: str) -> bool:
    from .security import verify_password

    st = state(request)
    if st.env_password_digest is not None:
        return hmac.compare_digest(hashlib.sha256(password.encode()).digest(), st.env_password_digest)
    stored = settings["admin_password_hash"]
    return bool(stored) and verify_password(password, stored)


def admin_session(request: Request, conn: sqlite3.Connection) -> AdminSession | None:
    if hasattr(request.state, "admin"):
        return request.state.admin
    token = request.cookies.get(ADMIN_COOKIE)
    session = None
    if token:
        settings = load_settings(request, conn)
        session = state(request).signer.verify(token, current_fingerprint(request, settings))
    request.state.admin = session
    return session


class AdminRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def require_admin(request: Request, conn: sqlite3.Connection) -> AdminSession:
    session = admin_session(request, conn)
    if session is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise AdminRequired(target if request.method == "GET" else "/admin")
    return session


async def require_admin_post(request: Request, conn: sqlite3.Connection) -> tuple[AdminSession, Any]:
    """Admin check + CSRF validation for form posts. Returns (session, form)."""
    session = require_admin(request, conn)
    form = await request.form()
    if not state(request).signer.check_csrf(session, form.get("csrf")):
        raise HTTPException(403, "The form expired. Go back, reload the page and try again.")
    return session, form


def cookie_secure(request: Request) -> bool:
    mode = state(request).config.cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    return request.url.scheme == "https"


def _issue_admin_token(request: Request, settings: Settings) -> str:
    st = state(request)
    token = st.signer.issue(current_fingerprint(request, settings), st.config.session_minutes * 60)
    # Make the rest of this request (e.g. CSRF tokens in the page) use the new session.
    request.state.admin = st.signer.verify(token, current_fingerprint(request, settings))
    return token


def _write_admin_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=state(request).config.session_minutes * 60, httponly=True,
        samesite="strict", secure=cookie_secure(request), path="/",
    )


def set_admin_cookie(request: Request, response: Response, settings: Settings) -> None:
    _write_admin_cookie(request, response, _issue_admin_token(request, settings))


def refresh_admin_session(request: Request, conn: sqlite3.Connection) -> str | None:
    """Sliding expiry: once half the lifetime has passed, issue a new session.

    Returns the new cookie value (to be set on the response), or None.
    """
    session = admin_session(request, conn)
    if session is None:
        return None
    if session.remaining_seconds() >= state(request).config.session_minutes * 60 / 2:
        return None
    return _issue_admin_token(request, load_settings(request, conn))


# --- visibility window -----------------------------------------------------------

def window_start(settings: Settings, now: datetime) -> str:
    return repo.window_start(now, settings.open_window_hours)


def require_admin_csrf(request: Request, conn: sqlite3.Connection, csrf: str | None) -> AdminSession:
    """Admin check for an open-area POST that only an admin may perform."""
    session = require_admin(request, conn)
    if request.method == "POST" and not state(request).signer.check_csrf(session, csrf):
        raise HTTPException(403, "The form expired. Go back, reload the page and try again.")
    return session


def ensure_visible(request: Request, conn: sqlite3.Connection, entry_time: str, now: datetime,
                   csrf: str | None = None) -> None:
    """Entries older than the open window are admin-only (with CSRF for writes)."""
    settings = load_settings(request, conn)
    if entry_time < window_start(settings, now):
        require_admin_csrf(request, conn, csrf)


def earliest_allowed(request: Request, conn: sqlite3.Connection, now: datetime) -> datetime | None:
    """Oldest time an entry may be given from the open pages (None for admins)."""
    if admin_session(request, conn) is not None:
        return None
    return now - timedelta(hours=load_settings(request, conn).open_window_hours)


MAX_ID = 2**63 - 1
# Path ids outside SQLite's integer range would otherwise crash the query.
EntryId = Annotated[int, Path(ge=1, le=MAX_ID)]


def parse_id(value: str) -> int | None:
    """Strictly parse a numeric path segment (rejects '²', huge numbers...)."""
    if not (value.isascii() and value.isdigit()) or len(value) > 18:
        return None
    number = int(value)
    return number if 0 < number <= MAX_ID else None


# --- templates ----------------------------------------------------------------------

def static_fingerprint(directory: FilePath = STATIC_DIR) -> str:
    """Short hash of every static file: the ?v= cache-buster in page links and
    the service worker's cache name. Any changed file gets a new URL, so an
    installed app never keeps serving stale CSS or JS."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = static_fingerprint()

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
env = templates.env
env.globals.update(C=C, version=__version__, asset_version=ASSET_VERSION, timedelta=timedelta)
env.globals["safe_next"] = lambda url, default="/": safe_next(url, default)
env.filters["dt"] = lambda v, tz, now=None: fmt_datetime(from_db(v) if isinstance(v, str) else v, tz, now)
env.filters["time"] = lambda v, tz: fmt_time(from_db(v) if isinstance(v, str) else v, tz)
env.filters["ago"] = lambda v, now=None: fmt_ago(from_db(v) if isinstance(v, str) else v, now)
env.filters["duration"] = fmt_duration
env.filters["local_input"] = lambda v, tz: to_local_input(v, tz)
env.filters["volume"] = lambda ml, unit: format_volume(ml, unit)
env.filters["vital"] = lambda v, param: format_value(v, param)
env.filters["feed_minutes"] = repo.feeding_minutes
env.filters["final_side"] = repo.final_side
env.filters["urlq"] = lambda v: quote(str(v), safe="")


def _until(v: str | datetime | None, now_: datetime | None = None) -> str:
    dt = from_db(v) if isinstance(v, str) else v
    if dt is None:
        return ""
    mins = (dt - (now_ or utcnow())).total_seconds() / 60
    if mins >= 1:
        return "in " + fmt_duration(mins)
    if mins > -1:
        return "now"
    return fmt_duration(-mins) + " overdue"


def _volume_input(ml: float | None, unit: str) -> str:
    if ml is None or ml == "":
        return ""
    ml = float(ml)
    return f"{ml / C.ML_PER_OZ:.1f}" if unit == "oz" else f"{ml:.0f}"


env.filters["until"] = _until
env.filters["volume_input"] = _volume_input
env.filters["iso"] = lambda v: to_db(v) if isinstance(v, datetime) else (v or "")


def local_day(v: str | datetime, tz) -> str:
    dt = from_db(v) if isinstance(v, str) else v
    local = dt.astimezone(tz)
    return f"{local.strftime('%A, %B')} {local.day}"


env.filters["day"] = local_day


def base_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    settings = load_settings(request, conn)
    now = getattr(request.state, "now", None) or utcnow()
    request.state.now = now
    session = admin_session(request, conn)
    toast_code = request.query_params.get("toast", "")
    toast = TOASTS.get(toast_code)
    undo = None
    undo_raw = request.query_params.get("undo", "")
    if undo_raw and ":" in undo_raw:
        table, _, entry_id = undo_raw.partition(":")
        action = request.query_params.get("undo_action", "delete")
        if table in repo.ENTITIES and entry_id.isdigit() and action in ("delete", "restore"):
            undo = {"table": table, "id": int(entry_id), "action": action}
    who = request.cookies.get(WHO_COOKIE, "")
    if who not in settings.caregivers:
        who = ""
    today = now.astimezone(settings.tz).date()
    return {
        "request": request,
        "settings": settings,
        "tz": settings.tz,
        "now": now,
        "baby_name": settings.baby_name,
        "baby_age": fmt_age(settings.birth_date, today),
        "is_admin": session is not None,
        "admin_remaining": session.remaining_seconds() if session else 0,
        "csrf": state(request).signer.csrf_token(session) if session else "",
        "who": who,
        "caregivers": settings.caregivers,
        "toast": toast,
        "undo": undo,
        "volume_unit": settings.volume_unit,
        "path": request.url.path,
    }


def render(request: Request, conn: sqlite3.Connection, template: str, status_code: int = 200,
           **context: Any) -> Response:
    new_token = refresh_admin_session(request, conn)  # before the CSRF token is generated
    ctx = base_context(request, conn)
    ctx.update(context)
    response = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if new_token:
        _write_admin_cookie(request, response, new_token)
    return response


def redirect(url: str, toast: str | None = None, undo: tuple[str, int, str] | None = None) -> RedirectResponse:
    params = {}
    if toast:
        params["toast"] = toast
    if undo:
        params["undo"] = f"{undo[0]}:{undo[1]}"
        params["undo_action"] = undo[2]
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative redirects."""
    if not isinstance(url, str) or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    if any(ord(c) < 33 or ord(c) == 127 for c in url):  # browsers drop tabs/newlines: "/\t/x" -> "//x"
        return default
    return url


def who_logged(request: Request, conn: sqlite3.Connection, form_value: str | None = None) -> str:
    settings = load_settings(request, conn)
    for candidate in (form_value, request.cookies.get(WHO_COOKIE)):
        if candidate and candidate in settings.caregivers:
            return candidate
    return ""


def now(request: Request) -> datetime:
    value = getattr(request.state, "now", None)
    if value is None:
        value = utcnow()
        request.state.now = value
    return value


"""Application factory: middleware, startup (migrations, admin password
bootstrap) and the background reminder loop."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db, repo, settings_store
from .config import Config, load_config
from .events import EventBus
from .notify import Notifier
from .reminders import tick
from .security import LoginThrottle, SessionSigner, generate_password, hash_password, host_allowed, origin_allowed
from .timeutil import to_db, utcnow
from .web import AdminRequired, AppState, templates

log = logging.getLogger("lilypad")
STATIC_DIR = Path(__file__).parent / "static"
TRASH_KEEP_DAYS = 30
ALERT_KEEP_DAYS = 365


# --- request guard ------------------------------------------------------------------

class GuardMiddleware:
    """Host allowlist, same-origin check for writes, and security headers.

    Logging is unauthenticated, so the main defence against a malicious web
    page silently submitting forms to LilyPad from a parent's browser is the
    Origin check below: browsers always attach Origin to cross-site POSTs.
    """

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

    def __init__(self, app, config: Config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        path = scope.get("path", "")

        if path != "/healthz" and not host_allowed(host, self.config.allowed_hosts):
            return await self._plain(send, 421, "Unrecognized host. Add it to LILYPAD_ALLOWED_HOSTS.")

        if scope["method"] not in self.SAFE_METHODS:
            origin = headers.get("origin")
            if origin is None and headers.get("referer"):
                parts = urlsplit(headers["referer"])
                origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else "null"
            if origin is not None:
                hosts = [host]
                forwarded = headers.get("x-forwarded-host")
                if forwarded:
                    hosts.append(forwarded.split(",")[0].strip())
                if not origin_allowed(origin, hosts, self.config.allowed_origins):
                    log.warning("blocked cross-origin %s %s from origin %r (host %r)",
                                scope["method"], path, origin, host)
                    return await self._plain(
                        send, 403,
                        "Blocked: this form was submitted from a different site than LilyPad "
                        f"({origin}). If you use a reverse proxy, forward the original Host "
                        "header or set LILYPAD_ALLOWED_ORIGINS.",
                    )

        is_dash = path == "/dash"
        is_static = path.startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                ancestors = self.config.dash_frame_ancestors if is_dash else "'none'"
                h.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                    "connect-src 'self'; font-src 'self'; form-action 'self'; base-uri 'none'; "
                    f"object-src 'none'; frame-ancestors {ancestors}",
                )
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
                h.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
                if not is_dash:
                    h.setdefault("X-Frame-Options", "DENY")
                if is_static:
                    h.setdefault("Cache-Control", "public, max-age=86400")
                else:
                    h.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _plain(send, status: int, text: str):
        body = text.encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


# --- startup ----------------------------------------------------------------------------

def bootstrap(config: Config) -> tuple[str, bytes | None]:
    """Run migrations, ensure a secret key and an admin password exist."""
    db.init_db(config.db_path)
    conn = db.connect(config.db_path)
    try:
        settings = settings_store.load(conn, config.default_timezone)
        secret = config.secret_key or settings["secret_key"]
        if not secret:
            secret = secrets.token_urlsafe(32)
            settings_store.save(conn, {"secret_key": secret})

        env_digest = None
        password_file = config.password_file
        if config.admin_password:
            env_digest = hashlib.sha256(config.admin_password.encode()).digest()
            log.info("Admin password is set by LILYPAD_ADMIN_PASSWORD.")
        elif not settings["admin_password_hash"]:
            password = generate_password()
            settings_store.save(conn, {"admin_password_hash": hash_password(password)})
            try:
                password_file.write_text(
                    "LilyPad generated this admin password on first start:\n\n"
                    f"    {password}\n\n"
                    "Change it in Admin > Settings; this file is deleted when you do.\n"
                )
                os.chmod(password_file, 0o600)
            except OSError:
                pass
            banner = "=" * 64
            log.warning("\n%s\n  LilyPad admin password (generated): %s\n"
                        "  Also saved to %s\n  Change it in Admin > Settings.\n%s",
                        banner, password, password_file, banner)

        # Housekeeping: empty old trash and trim the notification log.
        now = utcnow()
        repo.purge_deleted(conn, to_db(now - timedelta(days=TRASH_KEEP_DAYS)))
        conn.execute("DELETE FROM alerts WHERE created_at < ?", (to_db(now - timedelta(days=ALERT_KEEP_DAYS)),))
    finally:
        conn.close()
    return secret, env_digest


async def reminder_loop(app: FastAPI) -> None:
    st: AppState = app.state.lilypad

    def run_once():
        conn = db.connect(st.config.db_path)
        try:
            settings = settings_store.load(conn, st.config.default_timezone)
            return tick(conn, settings, st.notifier, utcnow())
        finally:
            conn.close()

    while True:
        try:
            sent = await asyncio.to_thread(run_once)
            for alert in sent:
                log.info("reminder alert: %s — %s", alert["title"], alert["delivery"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder loop iteration failed")
        await asyncio.sleep(st.config.reminder_tick_seconds)


def create_app(config: Config | None = None, notifier: Notifier | None = None) -> FastAPI:
    config = config or load_config()
    logging.basicConfig(level=os.environ.get("LILYPAD_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret, env_digest = bootstrap(config)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        task = None
        if config.scheduler_enabled:
            task = asyncio.create_task(reminder_loop(app))
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="LilyPad", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.lilypad = AppState(
        config=config, bus=bus, notifier=notifier or Notifier(bus),
        signer=SessionSigner(secret), throttle=LoginThrottle(), env_password_digest=env_digest,
    )

    @app.exception_handler(AdminRequired)
    async def _admin_required(request: Request, exc: AdminRequired):
        if request.url.path.startswith("/api/"):
            return PlainTextResponse("Admin password required.", status_code=401)
        from urllib.parse import urlencode
        return RedirectResponse("/admin/login?" + urlencode({"next": exc.next_url}), status_code=303)

    @app.exception_handler(RequestValidationError)
    async def _bad_path(request: Request, exc: RequestValidationError):
        # Only path parameters are validated by FastAPI here (e.g. ids out of range).
        return await _http_error(request, StarletteHTTPException(404, "Not Found"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return templates.TemplateResponse(
            request, "error.html",
            {"request": request, "status": exc.status_code, "detail": exc.detail,
             "baby_name": "", "is_admin": False, "path": request.url.path},
            status_code=exc.status_code,
        )

    from .routes import admin, api, open as open_routes

    app.include_router(open_routes.router)
    app.include_router(api.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/icons/icon.svg", status_code=301)

    app.add_middleware(GuardMiddleware, config=config)
    return app


def _app_from_env() -> FastAPI:  # used by uvicorn --factory
    return create_app()

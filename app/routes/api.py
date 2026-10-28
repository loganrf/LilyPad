"""JSON + Server-Sent Events endpoints for /dash, external dashboards and
ntfy action buttons. Open (no password), like the home screen."""

from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import repo
from ..reminders import snooze as snooze_reminder
from ..summary import public_json, snapshot
from ..timeutil import now_db, to_db
from ..web import get_conn, load_settings, now, redirect, safe_next, state

router = APIRouter()

SSE_KEEPALIVE_SECONDS = 20


@router.get("/healthz")
def healthz(conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return PlainTextResponse("ok")


@router.get("/api/dash")
def api_dash(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    data = public_json(snapshot(conn, settings, now(request)))
    data["dash_sound"] = settings.flag("dash_sound")
    return JSONResponse(data)


@router.get("/api/events")
async def api_events(request: Request):
    bus = state(request).bus
    queue = bus.subscribe()

    async def stream():
        try:
            yield "retry: 5000\n\n"
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    yield event.encode()
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _next_param(request: Request) -> str | None:
    value = request.query_params.get("next")
    if value is None and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get("next")
    return value if isinstance(value, str) else None


@router.post("/api/reminders/{reminder_id}/snooze")
async def api_snooze(reminder_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    reminder = repo.reminder(conn, reminder_id)
    if not reminder:
        raise HTTPException(404, "No such reminder.")
    try:
        minutes = int(request.query_params.get("minutes", "15"))
    except ValueError:
        minutes = 15
    until = snooze_reminder(conn, reminder, load_settings(request, conn), minutes, now(request))
    state(request).bus.publish("changed", {"what": "reminders"})
    next_url = await _next_param(request)
    if next_url is not None:
        return redirect(safe_next(next_url), "snoozed")
    return JSONResponse({"ok": True, "snoozed_until": to_db(until)})


@router.post("/api/alerts/{alert_id}/ack")
async def api_ack(alert_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    cur = conn.execute("UPDATE alerts SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
                       (now_db(), alert_id))
    if cur.rowcount == 0 and not conn.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone():
        raise HTTPException(404, "No such alert.")
    state(request).bus.publish("changed", {"what": "alerts"})
    next_url = await _next_param(request)
    if next_url is not None:
        return redirect(safe_next(next_url), "acknowledged")
    return JSONResponse({"ok": True})

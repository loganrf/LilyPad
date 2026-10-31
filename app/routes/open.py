"""Open (no password) pages: the home screen, logging forms, timeline and dash.

Anyone who can reach LilyPad can use these. They only ever show the recent
"open window" of data (48 hours by default); anything older needs the admin
password. See docs/PRIVACY_AND_SECURITY.md.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import constants as C
from .. import repo
from ..db import transaction
from ..describe import timeline as build_timeline
from ..forms import Form, FormError
from ..reminders import vital_alerts
from ..summary import is_stale_active, snapshot
from ..timeutil import from_db, to_db
from ..web import (
    WHO_COOKIE, EntryId, cookie_secure, earliest_allowed, ensure_visible, get_conn, load_settings, now,
    redirect, render, require_admin_csrf, safe_next, state, who_logged, window_start,
)

router = APIRouter()

UNDO_RESTORE_MINUTES = 30
MAX_FEED_MINUTES = 300
MAX_AMOUNT_ML = 1000


async def read_form(request: Request) -> Form:
    data = await request.form()
    multi = {key: data.getlist(key) for key in data.keys()}
    return Form({k: data.get(k) for k in data.keys()}, multi)


def changed(request: Request, what: str) -> None:
    """Tell live screens (other parent's phone, the dash) to refresh."""
    state(request).bus.publish("changed", {"what": what})


# --- home --------------------------------------------------------------------------

@router.get("/")
def home(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    current = now(request)
    snap = snapshot(conn, settings, current)
    statuses = snap["_statuses"]
    recent = build_timeline(conn, settings, window_start(settings, current), limit=8)
    return render(
        request, conn, "home.html",
        snap=snap,
        statuses=statuses,
        feed_status=next((s for s in statuses if s.reminder["kind"] == "feeding"), None),
        diaper_statuses=[s for s in statuses if s.reminder["kind"].startswith("diaper")],
        vitals_statuses=[s for s in statuses if s.reminder["kind"] == "vitals"],
        routine_statuses=[s for s in statuses if s.reminder["kind"] == "custom"],
        attention=[s for s in statuses if s.needs_attention and s.in_active_hours],
        active_feed=repo.active_feeding(conn),
        last_feed=repo.last_feeding(conn, completed_only=True),
        stale_active=is_stale_active(repo.active_feeding(conn), current),
        last_diaper=repo.last_diaper(conn),
        last_wet=repo.last_diaper(conn, C.WET_KINDS),
        last_dirty=repo.last_diaper(conn, C.DIRTY_KINDS),
        recent=recent,
        open_ped_notes=len(repo.pediatrician_notes(conn)),
        live=True,
    )


# --- feedings --------------------------------------------------------------------------

@router.post("/feed/start")
async def feed_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    method = form.choice("method", C.FEED_METHODS) or "breast"
    side = form.choice("side", C.FEED_SIDES) if method == "breast" else None
    if repo.active_feeding(conn) and not is_stale_active(repo.active_feeding(conn), now(request)):
        return redirect("/", "feed-active")
    feed_id = repo.insert(conn, "feedings", {
        "started_at": to_db(now(request)), "ended_at": None, "method": method, **repo.side_values(side),
        "logged_by": who_logged(request, conn),
    })
    changed(request, "feedings")
    return redirect("/", "feed-started", ("feedings", feed_id, "delete"))


@router.post("/feed/{feed_id}/stop")
async def feed_stop(feed_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    feed = repo.get(conn, "feedings", feed_id)
    if not feed:
        raise HTTPException(404, "That feed no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, feed["started_at"], now(request), form.text("csrf", 200))
    if feed["ended_at"] is None:
        repo.update(conn, "feedings", feed_id, {"ended_at": to_db(now(request))})
        changed(request, "feedings")
    return redirect(f"/feed/{feed_id}/edit", "feed-stopped")


@router.post("/feed/{feed_id}/side")
async def feed_side(feed_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    feed = repo.get(conn, "feedings", feed_id)
    if not feed:
        raise HTTPException(404, "That feed no longer exists.")
    ensure_visible(request, conn, feed["started_at"], now(request), form.text("csrf", 200))
    side = form.choice("side", C.FEED_SIDES)
    if side and feed["ended_at"] is None and (values := repo.switch_side(feed, side)):
        repo.update(conn, "feedings", feed_id, values)
        changed(request, "feedings")
        if values.get("side") == "both":
            return redirect("/", "feed-switched")
    return redirect("/")


def _feed_values(form: Form, request: Request, conn, existing: dict | None) -> dict:
    settings = load_settings(request, conn)
    current = now(request)
    method = form.choice("method", C.FEED_METHODS, "a feeding method", required=True)
    started = form.when("start", settings.tz, current, "start time",
                        earliest=earliest_allowed(request, conn, current))
    duration = form.number("duration", "Duration", 0, MAX_FEED_MINUTES)
    amount = form.number("amount", "Amount", 0, MAX_AMOUNT_ML if settings.volume_unit == "ml" else 35)
    if amount is not None and settings.volume_unit == "oz":
        amount = round(amount * C.ML_PER_OZ, 1)
    form.raise_if_errors()
    ended = None
    if duration is not None:
        ended = started + timedelta(minutes=duration)
    elif existing is None or existing.get("ended_at") is not None:
        ended = started  # past feed with no duration recorded
    values = {
        "started_at": to_db(started),
        "ended_at": to_db(ended) if ended else None,
        "method": method,
        **repo.side_values(form.choice("side", C.FEED_SIDES) if method == "breast" else None,
                           form.choice("last_side", C.BREAST_SIDES, "which side it ended on")),
        "bottle_content": form.choice("bottle_content", C.BOTTLE_CONTENTS) if method == "bottle" else None,
        "amount_ml": amount if method != "breast" else None,
        "quality": form.choice("quality", C.FEED_QUALITY),
        "issues": ",".join(form.multi("issues", C.FEED_ISSUES)),
        "notes": form.text("notes", 2000),
        "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
    }
    form.raise_if_errors()
    return values


def _feed_form(request, conn, feed: dict | None, error: str | None = None, status_code: int = 200):
    duration = None
    if feed and feed.get("ended_at"):
        duration = round(repo.feeding_minutes(feed) or 0)
    elif feed and feed.get("started_at"):
        duration = None
    return render(request, conn, "feed_form.html", status_code=status_code, feed=feed, error=error,
                  duration=duration, suggested_side=repo.suggested_side(conn))


@router.get("/feed/new")
def feed_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    method = request.query_params.get("method", "breast")
    side = request.query_params.get("side") or repo.suggested_side(conn)
    return _feed_form(request, conn, {"method": method if method in C.FEED_METHODS else "breast", "side": side})


@router.post("/feed/new")
async def feed_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    try:
        values = _feed_values(form, request, conn, None)
    except FormError as exc:
        return _feed_form(request, conn, dict(form.data), str(exc), 400)
    feed_id = repo.insert(conn, "feedings", values)
    changed(request, "feedings")
    return redirect("/", "feed-saved", ("feedings", feed_id, "delete"))


@router.get("/feed/{feed_id}/edit")
def feed_edit(feed_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    feed = repo.get(conn, "feedings", feed_id)
    if not feed:
        raise HTTPException(404, "That feed no longer exists.")
    ensure_visible(request, conn, feed["started_at"], now(request))
    return _feed_form(request, conn, feed)


@router.post("/feed/{feed_id}/edit")
async def feed_update(feed_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    feed = repo.get(conn, "feedings", feed_id)
    if not feed:
        raise HTTPException(404, "That feed no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, feed["started_at"], now(request), form.text("csrf", 200))
    try:
        values = _feed_values(form, request, conn, feed)
    except FormError as exc:
        return _feed_form(request, conn, {**feed, **dict(form.data)}, str(exc), 400)
    repo.update(conn, "feedings", feed_id, values)
    changed(request, "feedings")
    return redirect(safe_next(form.text("next", 300), "/"), "feed-saved")


# --- diapers ---------------------------------------------------------------------------

@router.post("/diaper/quick")
async def diaper_quick(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    kind = form.choice("kind", C.DIAPER_KINDS) or "wet"
    diaper_id = repo.insert(conn, "diapers", {
        "occurred_at": to_db(now(request)), "kind": kind, "logged_by": who_logged(request, conn),
    })
    changed(request, "diapers")
    return redirect("/", "diaper-saved", ("diapers", diaper_id, "delete"))


def _diaper_values(form: Form, request, conn) -> dict:
    settings = load_settings(request, conn)
    kind = form.choice("kind", C.DIAPER_KINDS, "what was in the diaper", required=True)
    when = form.when("when", settings.tz, now(request), "time",
                     earliest=earliest_allowed(request, conn, now(request)))
    dirty = kind in C.DIRTY_KINDS
    values = {
        "occurred_at": to_db(when) if when else None,
        "kind": kind,
        "color": form.choice("color", C.STOOL_COLORS) if dirty else None,
        "consistency": form.choice("consistency", C.STOOL_CONSISTENCY) if dirty else None,
        "amount": form.choice("amount", C.AMOUNTS),
        "notes": form.text("notes", 2000),
        "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
    }
    form.raise_if_errors()
    return values


@router.get("/diaper/new")
def diaper_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    kind = request.query_params.get("kind", "dirty")
    return render(request, conn, "diaper_form.html", diaper={"kind": kind if kind in C.DIAPER_KINDS else "dirty"})


@router.post("/diaper/new")
async def diaper_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    try:
        values = _diaper_values(form, request, conn)
    except FormError as exc:
        return render(request, conn, "diaper_form.html", 400, diaper=dict(form.data), error=str(exc))
    diaper_id = repo.insert(conn, "diapers", values)
    changed(request, "diapers")
    return redirect("/", "diaper-saved", ("diapers", diaper_id, "delete"))


@router.get("/diaper/{diaper_id}/edit")
def diaper_edit(diaper_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    diaper = repo.get(conn, "diapers", diaper_id)
    if not diaper:
        raise HTTPException(404, "That diaper entry no longer exists.")
    ensure_visible(request, conn, diaper["occurred_at"], now(request))
    return render(request, conn, "diaper_form.html", diaper=diaper)


@router.post("/diaper/{diaper_id}/edit")
async def diaper_update(diaper_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    diaper = repo.get(conn, "diapers", diaper_id)
    if not diaper:
        raise HTTPException(404, "That diaper entry no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, diaper["occurred_at"], now(request), form.text("csrf", 200))
    try:
        values = _diaper_values(form, request, conn)
    except FormError as exc:
        return render(request, conn, "diaper_form.html", 400, diaper={**diaper, **dict(form.data)}, error=str(exc))
    repo.update(conn, "diapers", diaper_id, values)
    changed(request, "diapers")
    return redirect(safe_next(form.text("next", 300), "/"), "diaper-saved")


# --- vitals ------------------------------------------------------------------------------

def _vital_values(form: Form, params: list[dict]) -> dict[int, float]:
    values = {}
    for p in params:
        value = form.number(f"p_{p['id']}", p["name"])
        if value is not None:
            values[p["id"]] = value
    return values


def _vitals_toast(conn, check_id: int) -> str:
    statuses = {repo.reading_status(r["value"], r) for r in repo.readings_for_check(conn, check_id)}
    if "alert" in statuses:
        return "vitals-alert"
    if "watch" in statuses:
        return "vitals-watch"
    return "vitals-saved"


def _vitals_form(request, conn, check: dict | None, readings: dict, error=None, status_code=200):
    params = repo.vital_params(conn)
    if check and check.get("id"):
        # Include archived parameters that this check already has values for.
        have = {p["id"] for p in params}
        for pid in readings:
            if pid not in have and (p := repo.vital_param(conn, pid)):
                params.append(p)
    return render(request, conn, "vitals_form.html", status_code=status_code, check=check,
                  params=params, readings=readings, error=error)


@router.get("/vitals/new")
def vitals_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _vitals_form(request, conn, None, {})


@router.post("/vitals/new")
async def vitals_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    form = await read_form(request)
    params = repo.vital_params(conn)
    values = _vital_values(form, params)
    when = form.when("when", settings.tz, now(request), "time",
                     earliest=earliest_allowed(request, conn, now(request)))
    if not values and not form.errors:
        form.error("Enter at least one reading.")
    if form.errors:
        raw = {p["id"]: form.text(f"p_{p['id']}") for p in params}
        return _vitals_form(request, conn, dict(form.data), raw, " ".join(form.errors), 400)
    with transaction(conn):
        check_id = repo.insert(conn, "vital_checks", {
            "measured_at": to_db(when), "notes": form.text("notes", 2000),
            "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
        })
        repo.save_readings(conn, check_id, values)
    st = state(request)
    vital_alerts(conn, settings, st.notifier, check_id)
    changed(request, "vitals")
    return redirect("/", _vitals_toast(conn, check_id), ("vital_checks", check_id, "delete"))


@router.get("/vitals/{check_id}/edit")
def vitals_edit(check_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    check = repo.get(conn, "vital_checks", check_id)
    if not check:
        raise HTTPException(404, "That vitals check no longer exists.")
    ensure_visible(request, conn, check["measured_at"], now(request))
    readings = {r["param_id"]: r["value"] for r in repo.readings_for_check(conn, check_id)}
    return _vitals_form(request, conn, check, readings)


@router.post("/vitals/{check_id}/edit")
async def vitals_update(check_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    check = repo.get(conn, "vital_checks", check_id)
    if not check:
        raise HTTPException(404, "That vitals check no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, check["measured_at"], now(request), form.text("csrf", 200))
    existing = {r["param_id"] for r in repo.readings_for_check(conn, check_id)}
    params = [p for p in repo.vital_params(conn, active_only=False) if p["active"] or p["id"] in existing]
    values = _vital_values(form, params)
    when = form.when("when", settings.tz, now(request), "time",
                     earliest=earliest_allowed(request, conn, now(request)))
    if not values and not form.errors:
        form.error("Enter at least one reading (or delete the check).")
    if form.errors:
        raw = {p["id"]: form.text(f"p_{p['id']}") for p in params}
        return _vitals_form(request, conn, {**check, **dict(form.data)}, raw, " ".join(form.errors), 400)
    before = {r["param_id"]: r["value"] for r in repo.readings_for_check(conn, check_id)}
    with transaction(conn):
        repo.update(conn, "vital_checks", check_id, {
            "measured_at": to_db(when), "notes": form.text("notes", 2000),
            "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
        })
        repo.save_readings(conn, check_id, values)
    if values != before:
        vital_alerts(conn, settings, state(request).notifier, check_id)
    changed(request, "vitals")
    return redirect(safe_next(form.text("next", 300), "/"), _vitals_toast(conn, check_id))


# --- notes ---------------------------------------------------------------------------------

def _note_values(form: Form, request, conn) -> dict:
    settings = load_settings(request, conn)
    body = form.text("body", 10000)
    if not body:
        form.error("Write something in the note.")
    when = form.when("when", settings.tz, now(request), "time",
                     earliest=earliest_allowed(request, conn, now(request)))
    form.raise_if_errors()
    return {
        "occurred_at": to_db(when),
        "body": body,
        "tags": ", ".join(repo.split_tags(form.text("tags", 500))),
        "for_pediatrician": 1 if form.flag("for_pediatrician") else 0,
        "discussed": 1 if form.flag("discussed") else 0,
        "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
    }


@router.get("/note/new")
def note_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ped = request.query_params.get("ped") == "1"
    return render(request, conn, "note_form.html", note={"for_pediatrician": 1 if ped else 0},
                  tags=repo.all_tags(conn)[:20])


@router.post("/note/new")
async def note_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    try:
        values = _note_values(form, request, conn)
    except FormError as exc:
        return render(request, conn, "note_form.html", 400, note=dict(form.data), error=str(exc),
                      tags=repo.all_tags(conn)[:20])
    note_id = repo.insert(conn, "notes", values)
    changed(request, "notes")
    return redirect("/", "note-saved", ("notes", note_id, "delete"))


@router.get("/note/{note_id}/edit")
def note_edit(note_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    note = repo.get(conn, "notes", note_id)
    if not note:
        raise HTTPException(404, "That note no longer exists.")
    ensure_visible(request, conn, note["occurred_at"], now(request))
    return render(request, conn, "note_form.html", note=note, tags=repo.all_tags(conn)[:20])


@router.post("/note/{note_id}/edit")
async def note_update(note_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    note = repo.get(conn, "notes", note_id)
    if not note:
        raise HTTPException(404, "That note no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, note["occurred_at"], now(request), form.text("csrf", 200))
    try:
        values = _note_values(form, request, conn)
    except FormError as exc:
        return render(request, conn, "note_form.html", 400, note={**note, **dict(form.data)}, error=str(exc),
                      tags=repo.all_tags(conn)[:20])
    repo.update(conn, "notes", note_id, values)
    changed(request, "notes")
    return redirect(safe_next(form.text("next", 300), "/"), "note-saved")


# --- custom routines -------------------------------------------------------------------------

@router.post("/task/{reminder_id}/done")
def task_done(reminder_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    reminder = repo.reminder(conn, reminder_id)
    if not reminder or reminder["kind"] != "custom":
        raise HTTPException(404, "That routine no longer exists.")
    log_id = repo.insert(conn, "task_logs", {
        "reminder_id": reminder_id, "task_name": reminder["name"], "done_at": to_db(now(request)),
        "logged_by": who_logged(request, conn),
    })
    changed(request, "tasks")
    return redirect("/", "task-done", ("task_logs", log_id, "delete"))


@router.get("/task/{log_id}/edit")
def task_edit(log_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    task = repo.get(conn, "task_logs", log_id)
    if not task:
        raise HTTPException(404, "That entry no longer exists.")
    ensure_visible(request, conn, task["done_at"], now(request))
    return render(request, conn, "task_form.html", task=task)


@router.post("/task/{log_id}/edit")
async def task_update(log_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    task = repo.get(conn, "task_logs", log_id)
    if not task:
        raise HTTPException(404, "That entry no longer exists.")
    form = await read_form(request)
    ensure_visible(request, conn, task["done_at"], now(request), form.text("csrf", 200))
    when = form.when("when", settings.tz, now(request), "time",
                     earliest=earliest_allowed(request, conn, now(request)))
    if form.errors:
        return render(request, conn, "task_form.html", 400, task={**task, **dict(form.data)},
                      error=" ".join(form.errors))
    repo.update(conn, "task_logs", log_id, {
        "done_at": to_db(when), "notes": form.text("notes", 2000),
        "logged_by": who_logged(request, conn, form.text("logged_by", 60)),
    })
    changed(request, "tasks")
    return redirect(safe_next(form.text("next", 300), "/"), "task-done")


# --- delete / restore -------------------------------------------------------------------------

@router.post("/entry/{table}/{entry_id}/delete")
async def entry_delete(table: str, entry_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if table not in repo.ENTITIES:
        raise HTTPException(404)
    row = repo.get(conn, table, entry_id)
    form = await read_form(request)
    back = safe_next(form.text("next", 300), "/")
    if not row:
        return redirect(back)
    ensure_visible(request, conn, row[repo.entity(table).time_col], now(request), form.text("csrf", 200))
    repo.soft_delete(conn, table, entry_id)
    changed(request, table)
    toast = "undone" if form.flag("undo") else "deleted"
    return redirect(back, toast, None if form.flag("undo") else (table, entry_id, "restore"))


@router.post("/entry/{table}/{entry_id}/restore")
async def entry_restore(table: str, entry_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if table not in repo.ENTITIES:
        raise HTTPException(404)
    row = repo.get(conn, table, entry_id, include_deleted=True)
    form = await read_form(request)
    back = safe_next(form.text("next", 300), "/")
    if not row or row["deleted_at"] is None:
        return redirect(back)
    deleted = from_db(row["deleted_at"])
    if deleted is None or now(request) - deleted > timedelta(minutes=UNDO_RESTORE_MINUTES):
        require_admin_csrf(request, conn, form.text("csrf", 200))
    ensure_visible(request, conn, row[repo.entity(table).time_col], now(request), form.text("csrf", 200))
    repo.restore(conn, table, entry_id)
    changed(request, table)
    return redirect(back, "restored")


# --- timeline -----------------------------------------------------------------------------------

TIMELINE_FILTERS = {
    "all": None,
    "feedings": ["feedings"],
    "diapers": ["diapers"],
    "vitals": ["vital_checks"],
    "notes": ["notes"],
    "routines": ["task_logs"],
}


@router.get("/timeline")
def timeline_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    current = now(request)
    kind = request.query_params.get("type", "all")
    if kind not in TIMELINE_FILTERS:
        kind = "all"
    items = build_timeline(conn, settings, window_start(settings, current), tables=TIMELINE_FILTERS[kind])
    days: list[tuple[str, list[dict]]] = []
    for item in items:
        local = from_db(item["time"]).astimezone(settings.tz).date()
        label = local.isoformat()
        if not days or days[-1][0] != label:
            days.append((label, []))
        days[-1][1].append(item)
    return render(request, conn, "timeline.html", days=days, kind=kind, filters=list(TIMELINE_FILTERS),
                  window_hours=settings.open_window_hours, live=True)


# --- misc ------------------------------------------------------------------------------------------

@router.post("/who")
async def set_who(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await read_form(request)
    settings = load_settings(request, conn)
    who = form.text("who", 60)
    response = redirect(safe_next(form.text("next", 300), "/"), "who-saved")
    if who in settings.caregivers:
        response.set_cookie(WHO_COOKIE, who, max_age=400 * 24 * 3600, samesite="lax",
                            httponly=True, secure=cookie_secure(request))
    else:
        response.delete_cookie(WHO_COOKIE)
    return response


@router.get("/privacy")
def privacy(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    settings = load_settings(request, conn)
    return render(request, conn, "privacy.html", window_hours=settings.open_window_hours)


@router.get("/dash")
def dash(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    settings = load_settings(request, conn)
    return render(request, conn, "dash.html", dash_sound=settings.flag("dash_sound"))

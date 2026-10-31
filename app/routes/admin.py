"""Password-protected admin area: settings, vitals parameters, reminders,
full history, reports, exports, trash and the notification log."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import timedelta
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from .. import constants as C
from .. import exports, repo, reports
from .. import settings_store
from ..describe import describe, timeline as build_timeline
from ..forms import Form
from ..main import TRASH_KEEP_DAYS
from ..notify import NTFY_TOPIC_RE, Notification
from ..security import hash_password
from ..timeutil import from_db, local_day_bounds, parse_date, parse_hhmm, to_db, utcnow
from ..web import (
    ADMIN_COOKIE, EntryId, admin_session, check_admin_password, get_conn, load_settings, now, parse_id,
    redirect, render, require_admin, require_admin_post, safe_next, set_admin_cookie, state,
)

router = APIRouter(prefix="/admin")

MIN_PASSWORD_LENGTH = 8


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _form(form) -> Form:
    return Form({k: form.get(k) for k in form.keys()}, {k: form.getlist(k) for k in form.keys()})


# --- login / logout -------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    next_url = safe_next(request.query_params.get("next"), "/admin")
    if admin_session(request, conn):
        return redirect(next_url)
    return render(request, conn, "admin/login.html", next_url=next_url,
                  env_password=state(request).env_password_digest is not None)


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    st = state(request)
    form = await request.form()
    next_url = safe_next(form.get("next"), "/admin")
    key = _client_key(request)
    locked = st.throttle.attempt(key)  # counts this attempt before the password is checked
    env_password = st.env_password_digest is not None
    if locked:
        return render(request, conn, "admin/login.html", 429, next_url=next_url, env_password=env_password,
                      error=f"Too many attempts. Try again in {locked} seconds.")
    password = form.get("password") or ""
    settings = load_settings(request, conn)
    ok = isinstance(password, str) and await asyncio.to_thread(check_admin_password, request, settings, password)
    if not ok:
        await asyncio.sleep(0.4)
        return render(request, conn, "admin/login.html", 401, next_url=next_url, env_password=env_password,
                      error="That password didn't work.")
    st.throttle.success(key)
    response = redirect(next_url)
    set_admin_cookie(request, response, settings)
    return response


@router.post("/logout")
def logout(request: Request):
    response = redirect("/", "logged-out")
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


# --- hub ---------------------------------------------------------------------------------

@router.get("")
def hub(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    settings = load_settings(request, conn)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE deleted_at IS NULL").fetchone()[0]
              for t in repo.ENTITIES}
    password_file = state(request).config.password_file.exists()
    return render(request, conn, "admin/index.html", counts=counts,
                  ped_notes=repo.pediatrician_notes(conn),
                  ntfy_ready=settings.flag("ntfy_enabled") and settings.is_set("ntfy_topic"),
                  webhook_ready=settings.flag("dash_webhook_enabled") and settings.is_set("dash_webhook_url"),
                  generated_password_pending=password_file)


# --- settings ------------------------------------------------------------------------------

def _settings_page(request, conn, errors=None, status_code=200):
    st = state(request)
    return render(request, conn, "admin/settings.html", status_code, errors=errors or [],
                  timezones=sorted(available_timezones()),
                  env_password=st.env_password_digest is not None,
                  secrets_set={k: load_settings(request, conn).is_set(k) for k in settings_store.SECRET_KEYS},
                  db_path=str(st.config.db_path))


@router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    return _settings_page(request, conn)


@router.post("/settings")
async def settings_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _, raw = await require_admin_post(request, conn)
    form = _form(raw)
    updates: dict[str, str] = {}
    errors: list[str] = []

    updates["baby_name"] = form.text("baby_name", 60) or "Baby"
    birth = form.text("birth_date", 20)
    if birth and parse_date(birth) is None:
        errors.append("Birth date must look like 2026-10-01.")
    updates["birth_date"] = birth
    tz = form.text("timezone", 64)
    if tz and tz not in available_timezones():
        errors.append(f"Unknown time zone {tz!r}.")
    updates["timezone"] = tz
    updates["volume_unit"] = "oz" if form.text("volume_unit") == "oz" else "ml"
    caregivers = [c.strip()[:30] for c in form.text("caregivers", 300).split(",") if c.strip()]
    updates["caregivers"] = ", ".join(dict.fromkeys(caregivers))
    hours = form.integer("open_window_hours", "Open window", settings_store.OPEN_WINDOW_MIN_HOURS,
                         settings_store.OPEN_WINDOW_MAX_HOURS)
    updates["open_window_hours"] = str(hours if hours is not None else 48)
    base_url = form.text("base_url", 300).rstrip("/")
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        errors.append("Base URL must start with http:// or https://.")
    updates["base_url"] = base_url

    updates["ntfy_enabled"] = "1" if form.flag("ntfy_enabled") else "0"
    updates["ntfy_server"] = form.text("ntfy_server", 300).rstrip("/") or "https://ntfy.sh"
    if not updates["ntfy_server"].lower().startswith(("http://", "https://")):
        errors.append("ntfy server must start with http:// or https://.")
    topic = form.text("ntfy_topic", 64)
    if topic and not NTFY_TOPIC_RE.match(topic):
        errors.append("ntfy topic may only contain letters, numbers, - and _ (max 64).")
    if updates["ntfy_enabled"] == "1" and not topic:
        errors.append("Set an ntfy topic to enable ntfy notifications.")
    updates["ntfy_topic"] = topic
    updates["ntfy_username"] = form.text("ntfy_username", 100)
    detail = form.text("notify_detail", 20)
    updates["notify_detail"] = detail if detail in ("minimal", "standard", "detailed") else "standard"
    updates["ntfy_actions"] = "1" if form.flag("ntfy_actions") else "0"

    updates["dash_webhook_enabled"] = "1" if form.flag("dash_webhook_enabled") else "0"
    hook = form.text("dash_webhook_url", 500)
    if hook and not hook.lower().startswith(("http://", "https://")):
        errors.append("Dash webhook URL must start with http:// or https://.")
    if updates["dash_webhook_enabled"] == "1" and not hook:
        errors.append("Set a webhook URL to enable the dash webhook.")
    updates["dash_webhook_url"] = hook
    updates["dash_sound"] = "1" if form.flag("dash_sound") else "0"

    # Secrets: blank keeps the stored value; the "clear" box removes it.
    for key, limit in (("ntfy_token", 200), ("ntfy_password", 200), ("dash_webhook_headers", 2000)):
        if form.flag(f"clear_{key}"):
            updates[key] = ""
        elif value := form.text(key, limit):
            updates[key] = value

    errors.extend(form.errors)
    if errors:
        # Re-render with what they typed (without echoing secrets).
        request.state.settings = settings_store.Settings(
            {**load_settings(request, conn).raw, **{k: v for k, v in updates.items()
                                                     if k not in settings_store.SECRET_KEYS}},
            default_timezone=state(request).config.default_timezone,
        )
        return _settings_page(request, conn, errors, 400)
    settings_store.save(conn, updates)
    state(request).bus.publish("changed", {"what": "settings"})
    return redirect("/admin/settings", "settings-saved")


@router.post("/password")
async def change_password(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _, raw = await require_admin_post(request, conn)
    st = state(request)
    if st.env_password_digest is not None:
        raise HTTPException(400, "The admin password is set by LILYPAD_ADMIN_PASSWORD and can't be changed here.")
    settings = load_settings(request, conn)
    current = raw.get("current") or ""
    new, confirm = raw.get("new") or "", raw.get("confirm") or ""
    errors = []
    if not await asyncio.to_thread(check_admin_password, request, settings, current):
        errors.append("Current password is wrong.")
    if len(new) < MIN_PASSWORD_LENGTH:
        errors.append(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if new != confirm:
        errors.append("New passwords don't match.")
    if errors:
        return _settings_page(request, conn, errors, 400)
    settings_store.save(conn, {"admin_password_hash": await asyncio.to_thread(hash_password, new)})
    try:
        os.remove(st.config.password_file)
    except OSError:
        pass
    request.state.settings = None
    fresh = load_settings(request, conn)
    response = redirect("/admin/settings", "password-changed")
    set_admin_cookie(request, response, fresh)
    return response


@router.post("/test-notification")
async def test_notification(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    await require_admin_post(request, conn)
    settings = load_settings(request, conn)
    note = Notification(
        title="LilyPad test", source="test", priority=3, tags=["white_check_mark"],
        message="Test notification from LilyPad. If you can read this, delivery works.",
    )
    await asyncio.to_thread(state(request).notifier.deliver, conn, settings, note)
    return redirect("/admin/alerts", "test-sent")


# --- vitals parameters --------------------------------------------------------------------

@router.get("/vitals")
def vitals_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    params = repo.vital_params(conn, active_only=False)
    usage = {r["param_id"]: r["n"] for r in conn.execute(
        "SELECT param_id, COUNT(*) AS n FROM vital_readings GROUP BY param_id")}
    return render(request, conn, "admin/vitals.html", params=params, usage=usage)


@router.get("/vitals/{param_id}")
def vital_param_form(param_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    if param_id == "new":
        param = {"decimals": 1, "notify_on_alert": 1, "active": 1}
    else:
        param = repo.vital_param(conn, pid) if (pid := parse_id(param_id)) else None
        if not param:
            raise HTTPException(404, "No such parameter.")
    return render(request, conn, "admin/vital_form.html", param=param)


@router.post("/vitals/{param_id}")
async def vital_param_save(param_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _, raw = await require_admin_post(request, conn)
    form = _form(raw)
    existing = None
    if param_id != "new":
        existing = repo.vital_param(conn, pid) if (pid := parse_id(param_id)) else None
        if not existing:
            raise HTTPException(404, "No such parameter.")
    name = form.text("name", 60)
    if not name:
        form.error("Give the parameter a name.")
    values = {
        "name": name,
        "unit": form.text("unit", 20),
        "decimals": form.integer("decimals", "Decimals", 0, 4) or 0,
        "expected_min": form.number("expected_min", "Expected minimum"),
        "expected_max": form.number("expected_max", "Expected maximum"),
        "alert_low": form.number("alert_low", "Alert low"),
        "alert_high": form.number("alert_high", "Alert high"),
        "notify_on_alert": 1 if form.flag("notify_on_alert") else 0,
        "help_text": form.text("help_text", 500),
        "active": 1 if form.flag("active") else 0,
        "sort_order": form.integer("sort_order", "Order", -1000, 1000) or 0,
    }
    emin, emax, alo, ahi = values["expected_min"], values["expected_max"], values["alert_low"], values["alert_high"]
    if emin is not None and emax is not None and emin > emax:
        form.error("Expected minimum is above expected maximum.")
    if alo is not None and ahi is not None and alo >= ahi:
        form.error("Alert low must be below alert high.")
    if alo is not None and emin is not None and alo > emin:
        form.error("Alert low should be at or below the expected minimum.")
    if ahi is not None and emax is not None and ahi < emax:
        form.error("Alert high should be at or above the expected maximum.")
    if form.errors:
        return render(request, conn, "admin/vital_form.html", 400, param={**(existing or {}), **values},
                      error=" ".join(form.errors))
    ts = to_db(utcnow())
    cols = list(values)
    if existing:
        conn.execute(f"UPDATE vital_params SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                     [values[c] for c in cols] + [ts, existing["id"]])
    else:
        conn.execute(f"INSERT INTO vital_params ({', '.join(cols)}, created_at, updated_at) "
                     f"VALUES ({', '.join('?' for _ in cols)}, ?, ?)", [values[c] for c in cols] + [ts, ts])
    return redirect("/admin/vitals", "param-saved")


@router.post("/vitals/{param_id}/delete")
async def vital_param_delete(param_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    await require_admin_post(request, conn)
    used = conn.execute("SELECT COUNT(*) FROM vital_readings WHERE param_id = ?", (param_id,)).fetchone()[0]
    if used:
        conn.execute("UPDATE vital_params SET active = 0, updated_at = ? WHERE id = ?", (to_db(utcnow()), param_id))
        return redirect("/admin/vitals", "param-archived")
    conn.execute("DELETE FROM vital_params WHERE id = ?", (param_id,))
    return redirect("/admin/vitals", "param-deleted")


# --- reminders ------------------------------------------------------------------------------

@router.get("/reminders")
def reminders_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    from ..reminders import compute

    settings = load_settings(request, conn)
    current = now(request)
    items = [(r, compute(conn, r, settings, current) if r["enabled"] else None) for r in repo.reminders(conn)]
    params = {p["id"]: p for p in repo.vital_params(conn, active_only=False)}
    return render(request, conn, "admin/reminders.html", items=items, params=params)


@router.get("/reminders/{reminder_id}")
def reminder_form(reminder_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    if reminder_id == "new":
        kind = request.query_params.get("kind", "feeding")
        reminder = {"kind": kind if kind in C.REMINDER_KINDS else "feeding", "interval_minutes": 180,
                    "window_minutes": 30, "nag_minutes": 15, "max_nags": 3, "priority": 3,
                    "notify_ntfy": 1, "notify_dash": 1, "enabled": 1, "anchor": "start"}
    else:
        reminder = repo.reminder(conn, rid) if (rid := parse_id(reminder_id)) else None
        if not reminder:
            raise HTTPException(404, "No such reminder.")
    return render(request, conn, "admin/reminder_form.html", reminder=reminder,
                  params=repo.vital_params(conn, active_only=False))


def _minutes(form: Form, name: str, label: str, minimum: int, maximum: int) -> int | None:
    """Accept '2h30m', '2:30', '150' (minutes) or hours+minutes fields."""
    hours = form.number(f"{name}_h", f"{label} hours", 0, maximum / 60)
    mins = form.number(f"{name}_m", f"{label} minutes", 0, maximum)
    if hours is None and mins is None:
        return None
    total = int(round((hours or 0) * 60 + (mins or 0)))
    if total < minimum or total > maximum:
        form.error(f"{label} must be between {minimum} and {maximum} minutes.")
        return None
    return total


@router.post("/reminders/{reminder_id}")
async def reminder_save(reminder_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _, raw = await require_admin_post(request, conn)
    form = _form(raw)
    existing = None
    if reminder_id != "new":
        existing = repo.reminder(conn, rid) if (rid := parse_id(reminder_id)) else None
        if not existing:
            raise HTTPException(404, "No such reminder.")
    kind = form.choice("kind", C.REMINDER_KINDS, "a reminder type", required=True)
    name = form.text("name", 60) or (C.REMINDER_KINDS.get(kind or "", "Reminder"))
    interval = _minutes(form, "interval", "Interval", 5, 14 * 24 * 60)
    if interval is None and not form.errors:
        form.error("Set how often this should happen.")
    window = _minutes(form, "window", "Heads-up window", 0, 24 * 60) or 0
    if interval is not None and window >= interval:
        form.error("The heads-up window must be shorter than the interval.")
    nag = _minutes(form, "nag", "Repeat", 0, 24 * 60) or 0
    active_start, active_end = form.text("active_start", 5), form.text("active_end", 5)
    for label, value in (("Active from", active_start), ("Active until", active_end)):
        if value and parse_hhmm(value) is None:
            form.error(f"{label} must be a time like 07:00.")
    vital_param_id = form.integer("vital_param_id", "Vital parameter") if kind == "vitals" else None
    values = {
        "name": name,
        "kind": kind,
        "vital_param_id": vital_param_id or None,
        "interval_minutes": interval or 0,
        "window_minutes": window,
        "nag_minutes": nag,
        "max_nags": form.integer("max_nags", "Max repeats", 0, 50) or 0,
        "anchor": "end" if form.text("anchor") == "end" else "start",
        "active_start": active_start or None,
        "active_end": active_end or None,
        "priority": form.choice("priority", C.NTFY_PRIORITIES) or 3,
        "notify_ntfy": 1 if form.flag("notify_ntfy") else 0,
        "notify_dash": 1 if form.flag("notify_dash") else 0,
        "enabled": 1 if form.flag("enabled") else 0,
        "sort_order": form.integer("sort_order", "Order", -1000, 1000) or 0,
    }
    if form.errors:
        return render(request, conn, "admin/reminder_form.html", 400, reminder={**(existing or {}), **values},
                      params=repo.vital_params(conn, active_only=False), error=" ".join(form.errors))
    ts = to_db(utcnow())
    cols = list(values)
    if existing:
        conn.execute(f"UPDATE reminders SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ? WHERE id = ?",
                     [values[c] for c in cols] + [ts, existing["id"]])
        # The schedule may have changed: start the notification cycle afresh.
        conn.execute("DELETE FROM reminder_state WHERE reminder_id = ?", (existing["id"],))
        if existing["kind"] == "custom" and values["name"] != existing["name"]:
            conn.execute("UPDATE task_logs SET task_name = ? WHERE reminder_id = ?", (values["name"], existing["id"]))
    else:
        conn.execute(f"INSERT INTO reminders ({', '.join(cols)}, created_at, updated_at) "
                     f"VALUES ({', '.join('?' for _ in cols)}, ?, ?)", [values[c] for c in cols] + [ts, ts])
    state(request).bus.publish("changed", {"what": "reminders"})
    return redirect("/admin/reminders", "reminder-saved")


@router.post("/reminders/{reminder_id}/delete")
async def reminder_delete(reminder_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    await require_admin_post(request, conn)
    conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    state(request).bus.publish("changed", {"what": "reminders"})
    return redirect("/admin/reminders", "reminder-deleted")


# --- history -----------------------------------------------------------------------------------

HISTORY_TYPES = {
    "all": None, "feedings": ["feedings"], "diapers": ["diapers"], "vitals": ["vital_checks"],
    "notes": ["notes"], "pediatrician": ["notes"], "routines": ["task_logs"],
}


def _range(request: Request, settings, default_days: int = 7):
    today = now(request).astimezone(settings.tz).date()
    start = parse_date(request.query_params.get("start"))
    end = parse_date(request.query_params.get("end"))
    if start is None and end is None:
        start = today - timedelta(days=default_days - 1)
    return reports.clamp_range(start, end, today)


@router.get("/history")
def history(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    settings = load_settings(request, conn)
    kind = request.query_params.get("type", "all")
    if kind not in HISTORY_TYPES:
        kind = "all"
    q = (request.query_params.get("q") or "").strip().lower()[:100]
    start, end = _range(request, settings, 14)
    since, until = to_db(local_day_bounds(start, settings.tz)[0]), to_db(local_day_bounds(end, settings.tz)[1])
    items = build_timeline(conn, settings, since, until, tables=HISTORY_TYPES[kind])
    if kind == "pediatrician":
        items = [i for i in items if i["status"] in ("ped", "done")]
    if q:
        items = [i for i in items if q in (i["title"] + " " + i["detail"] + " " + i["logged_by"]).lower()]
    days: list[tuple[str, list[dict]]] = []
    for item in items:
        label = from_db(item["time"]).astimezone(settings.tz).date().isoformat()
        if not days or days[-1][0] != label:
            days.append((label, []))
        days[-1][1].append(item)
    return render(request, conn, "admin/history.html", days=days, kind=kind, kinds=list(HISTORY_TYPES),
                  q=q, start=start, end=end, count=len(items))


# --- report ------------------------------------------------------------------------------------

@router.get("/report")
def report(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    settings = load_settings(request, conn)
    start, end = _range(request, settings, 7)
    data = reports.build(conn, settings, start, end, now(request))
    return render(request, conn, "admin/report.html", r=data, start=start, end=end)


@router.post("/notes/{note_id}/discussed")
async def note_discussed(note_id: EntryId, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _, raw = await require_admin_post(request, conn)
    value = 1 if raw.get("discussed") == "1" else 0
    conn.execute("UPDATE notes SET discussed = ?, updated_at = ? WHERE id = ?", (value, to_db(utcnow()), note_id))
    return redirect(safe_next(raw.get("next"), "/admin/report"))


# --- export ---------------------------------------------------------------------------------------

@router.get("/export")
def export_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    settings = load_settings(request, conn)
    today = now(request).astimezone(settings.tz).date()
    return render(request, conn, "admin/export.html", csv_names=exports.CSV_EXPORTS, today=today)


@router.get("/export/{name}.csv")
def export_csv(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    if name not in exports.CSV_EXPORTS:
        raise HTTPException(404)
    settings = load_settings(request, conn)
    start = parse_date(request.query_params.get("start"))
    end = parse_date(request.query_params.get("end"))
    since = to_db(local_day_bounds(start, settings.tz)[0]) if start else None
    until = to_db(local_day_bounds(end, settings.tz)[1]) if end else None
    body = exports.export_csv(conn, settings, name, since, until)
    stamp = now(request).astimezone(settings.tz).strftime("%Y%m%d")
    return Response("﻿" + body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="lilypad-{name}-{stamp}.csv"'})


@router.get("/export/backup.json")
def export_json(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    stamp = now(request).strftime("%Y%m%d-%H%M")
    return Response(exports.export_json(conn), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="lilypad-backup-{stamp}.json"'})


@router.get("/export/lilypad.sqlite3")
def export_sqlite(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    path = exports.sqlite_snapshot(conn)
    stamp = now(request).strftime("%Y%m%d-%H%M")
    return FileResponse(path, media_type="application/vnd.sqlite3",
                        filename=f"lilypad-{stamp}.sqlite3",
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


# --- trash ----------------------------------------------------------------------------------------

@router.get("/trash")
def trash(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    settings = load_settings(request, conn)
    since = to_db(now(request) - timedelta(days=TRASH_KEEP_DAYS))
    items = [describe(conn, row["_table"], row, settings) for row in repo.list_deleted(conn, since)]
    return render(request, conn, "admin/trash.html", items=items, keep_days=TRASH_KEEP_DAYS)


@router.post("/trash/purge")
async def trash_purge(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    await require_admin_post(request, conn)
    repo.purge_deleted(conn, to_db(now(request) + timedelta(seconds=1)))
    return redirect("/admin/trash", "purged")


# --- notification log -------------------------------------------------------------------------------

@router.get("/alerts")
def alerts_log(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request, conn)
    rows = []
    for r in conn.execute("SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT 200"):
        row = dict(r)
        try:
            row["delivery"] = json.loads(row["delivery"] or "{}")
        except ValueError:
            row["delivery"] = {}
        rows.append(row)
    return render(request, conn, "admin/alerts.html", alerts=rows)

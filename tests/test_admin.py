"""The password boundary: what needs the admin password and what doesn't."""

import csv
import io
import json
from datetime import timedelta

import pytest

from app import repo
from app.timeutil import to_db, utcnow
from tests.conftest import PASSWORD, csrf_of

PROTECTED_GETS = [
    "/admin", "/admin/settings", "/admin/vitals", "/admin/vitals/new", "/admin/reminders",
    "/admin/reminders/new", "/admin/history", "/admin/report", "/admin/export",
    "/admin/export/feedings.csv", "/admin/export/backup.json", "/admin/export/lilypad.sqlite3",
    "/admin/trash", "/admin/alerts",
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_admin_pages_require_password(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login?next=")


@pytest.mark.parametrize("path", ["/admin/settings", "/admin/password", "/admin/test-notification",
                                  "/admin/vitals/new", "/admin/reminders/new", "/admin/trash/purge"])
def test_admin_posts_require_password(client, path):
    r = client.post(path, data={"baby_name": "Hacked"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/login")


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        r = client.post("/admin/login", data={"password": "nope"})
        assert r.status_code == 401
    r = client.post("/admin/login", data={"password": PASSWORD})
    assert r.status_code == 429  # locked out even with the right password
    assert "Too many attempts" in r.text


def test_login_redirects_to_next_safely(client):
    r = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin/report"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/report"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    client.cookies.clear()
    r = client.post("/admin/login", data={"password": PASSWORD, "next": "//evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"


def test_admin_pages_render(admin):
    for path in PROTECTED_GETS[:10] + ["/admin/trash", "/admin/alerts"]:
        r = admin.get(path)
        assert r.status_code == 200, path


def test_logout(admin):
    admin.post("/admin/logout")
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_tampered_cookie_rejected(admin):
    token = admin.cookies.get("lilypad_admin")
    admin.cookies.set("lilypad_admin", token[:-3] + "AAA")
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_csrf_required(admin):
    r = admin.post("/admin/settings", data={"baby_name": "X"})
    assert r.status_code == 403


def test_settings_save_and_secret_handling(admin, conn):
    token = csrf_of(admin)
    r = admin.post("/admin/settings", data={
        "csrf": token, "baby_name": "Lily Rose", "birth_date": "2026-10-01", "timezone": "America/Chicago",
        "volume_unit": "oz", "caregivers": "Sam, Alex, Sam", "open_window_hours": "72",
        "ntfy_enabled": "1", "ntfy_server": "https://ntfy.sh", "ntfy_topic": "lilypad-abc123",
        "ntfy_token": "tk_secret", "notify_detail": "minimal", "ntfy_actions": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = admin.get("/admin/settings").text
    assert "tk_secret" not in page  # secrets are never echoed back
    assert "Saved — leave blank to keep" in page
    from app import settings_store
    s = settings_store.load(conn)
    assert s.caregivers == ["Sam", "Alex"]
    assert s.open_window_hours == 72 and s["ntfy_token"] == "tk_secret"
    # Saving again with a blank token keeps it.
    admin.post("/admin/settings", data={"csrf": token, "baby_name": "Lily", "ntfy_topic": "lilypad-abc123",
                                        "ntfy_enabled": "1"})
    assert settings_store.load(conn)["ntfy_token"] == "tk_secret"
    # And the clear box removes it.
    admin.post("/admin/settings", data={"csrf": token, "baby_name": "Lily", "clear_ntfy_token": "1"})
    assert settings_store.load(conn)["ntfy_token"] == ""


def test_settings_validation(admin):
    token = csrf_of(admin)
    r = admin.post("/admin/settings", data={"csrf": token, "baby_name": "Lily", "timezone": "Mars/Base",
                                            "ntfy_enabled": "1", "ntfy_topic": "bad topic!"})
    assert r.status_code == 400
    assert "Unknown time zone" in r.text and "ntfy topic" in r.text


def test_env_password_cannot_be_changed_in_ui(admin):
    token = csrf_of(admin)
    r = admin.post("/admin/password", data={"csrf": token, "current": PASSWORD, "new": "x" * 10, "confirm": "x" * 10})
    assert r.status_code == 400


def test_generated_password_flow(make_app, tmp_path):
    from fastapi.testclient import TestClient

    app = make_app(admin_password="")
    password_file = tmp_path / "INITIAL_ADMIN_PASSWORD.txt"
    generated = password_file.read_text().split("\n\n")[1].strip()
    with TestClient(app, base_url="http://lilypad.test") as c:
        assert c.post("/admin/login", data={"password": generated}, follow_redirects=False).status_code == 303
        token = csrf_of(c)
        r = c.post("/admin/password", data={"csrf": token, "current": generated, "new": "newpass123",
                                            "confirm": "newpass123"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        assert not password_file.exists()
        # Still logged in on this device (fresh cookie issued)...
        assert c.get("/admin", follow_redirects=False).status_code == 200
        # ...but the old password no longer works.
        c.cookies.clear()
        assert c.post("/admin/login", data={"password": generated}).status_code == 401
        assert c.post("/admin/login", data={"password": "newpass123"}, follow_redirects=False).status_code == 303


def test_password_change_invalidates_other_sessions(make_app):
    from fastapi.testclient import TestClient

    app = make_app(admin_password="")
    from app import db, settings_store
    from app.security import hash_password

    conn = db.connect(app.state.lilypad.config.db_path)
    settings_store.save(conn, {"admin_password_hash": hash_password("firstpass1")})
    with TestClient(app, base_url="http://lilypad.test") as a, TestClient(app, base_url="http://lilypad.test") as b:
        a.post("/admin/login", data={"password": "firstpass1"})
        b.post("/admin/login", data={"password": "firstpass1"})
        token = csrf_of(a)
        a.post("/admin/password", data={"csrf": token, "current": "firstpass1", "new": "secondpass2",
                                        "confirm": "secondpass2"})
        assert a.get("/admin", follow_redirects=False).status_code == 200
        assert b.get("/admin", follow_redirects=False).status_code == 303
    conn.close()


def test_old_entries_need_admin(client, conn):
    old_id = repo.insert(conn, "notes", {"occurred_at": to_db(utcnow() - timedelta(days=4)), "body": "old"})
    r = client.get(f"/note/{old_id}/edit", follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    r = client.post(f"/entry/notes/{old_id}/delete", follow_redirects=False)
    assert "/admin/login" in r.headers["location"]
    assert repo.get(conn, "notes", old_id) is not None
    client.post("/admin/login", data={"password": PASSWORD})
    assert client.get(f"/note/{old_id}/edit").status_code == 200


def test_open_window_setting_widens_access(client, conn):
    from tests.conftest import save_settings

    old_id = repo.insert(conn, "notes", {"occurred_at": to_db(utcnow() - timedelta(hours=60)), "body": "60h"})
    assert client.get(f"/note/{old_id}/edit", follow_redirects=False).status_code == 303
    save_settings(conn, open_window_hours=72)
    assert client.get(f"/note/{old_id}/edit", follow_redirects=False).status_code == 200


def test_history_shows_everything(admin, conn):
    repo.insert(conn, "notes", {"occurred_at": to_db(utcnow() - timedelta(days=10)), "body": "ten days ago"})
    page = admin.get("/admin/history?start=" + (utcnow() - timedelta(days=12)).date().isoformat()).text
    assert "ten days ago" in page
    page = admin.get("/admin/history?q=nomatch&start=" + (utcnow() - timedelta(days=12)).date().isoformat()).text
    assert "ten days ago" not in page


def test_csv_export_neutralizes_formulas(admin):
    admin.post("/note/new", data={"body": '=HYPERLINK("http://evil","click")', "when_ago": "0"})
    r = admin.get("/admin/export/notes.csv")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    body_col = rows[0].index("body")
    assert rows[1][body_col].startswith("'=")


def test_all_csv_exports(admin):
    admin.post("/feed/new", data={"method": "breast", "side": "left", "start_ago": "30", "duration": "10"})
    admin.post("/diaper/quick", data={"kind": "wet"})
    for name in ("feedings", "diapers", "vitals", "notes", "routines"):
        r = admin.get(f"/admin/export/{name}.csv?start=2020-01-01")
        assert r.status_code == 200, name


def test_json_backup_excludes_secrets(admin, conn):
    from tests.conftest import save_settings

    save_settings(conn, ntfy_token="tk_topsecret")
    data = json.loads(admin.get("/admin/export/backup.json").text)
    assert data["format"] == "lilypad-backup"
    assert "ntfy_token" not in data["settings"] and "admin_password_hash" not in data["settings"]
    assert "tk_topsecret" not in json.dumps(data)
    assert "vital_params" in data


def test_sqlite_snapshot(admin, tmp_path):
    import sqlite3

    r = admin.get("/admin/export/lilypad.sqlite3")
    assert r.status_code == 200
    path = tmp_path / "copy.sqlite3"
    path.write_bytes(r.content)
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM vital_params").fetchone()[0] >= 1
    c.close()


def test_report_renders_with_data(admin, conn):
    temp = next(p for p in repo.vital_params(conn) if p["name"] == "Temperature")
    for minutes in (30, 200, 400):
        admin.post("/feed/new", data={"method": "breast", "side": "left", "start_ago": "custom",
                                      "start_at": (utcnow() - timedelta(minutes=minutes)).astimezone().strftime("%Y-%m-%dT%H:%M"),
                                      "duration": "15", "quality": "3", "issues": "spit_up"})
    admin.post("/diaper/new", data={"kind": "dirty", "color": "red", "when_ago": "15"})
    admin.post("/vitals/new", data={f"p_{temp['id']}": "99.1", "when_ago": "60"})
    admin.post("/vitals/new", data={f"p_{temp['id']}": "100.6", "when_ago": "5"})
    admin.post("/note/new", data={"body": "Ask about reflux", "for_pediatrician": "1", "when_ago": "0"})
    r = admin.get("/admin/report")
    assert r.status_code == 200
    assert "<svg" in r.text and "Ask about reflux" in r.text
    assert "Red / bloody" in r.text
    assert "Spit-up" in r.text
    note = repo.pediatrician_notes(conn)[0]
    token = csrf_of(admin, "/admin/report")
    admin.post(f"/admin/notes/{note['id']}/discussed", data={"csrf": token, "discussed": "1", "next": "/admin/report"})
    assert repo.pediatrician_notes(conn) == []


def test_vital_param_crud(admin, conn):
    token = csrf_of(admin, "/admin/vitals/new")
    r = admin.post("/admin/vitals/new", data={
        "csrf": token, "name": "SpO2", "unit": "%", "decimals": "0", "expected_min": "95",
        "alert_low": "90", "notify_on_alert": "1", "active": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    p = next(p for p in repo.vital_params(conn) if p["name"] == "SpO2")
    assert p["alert_low"] == 90 and p["alert_high"] is None
    # Inconsistent limits are rejected.
    r = admin.post(f"/admin/vitals/{p['id']}", data={"csrf": token, "name": "SpO2", "expected_min": "95",
                                                     "alert_low": "97"})
    assert r.status_code == 400
    # Delete without readings removes it; with readings archives it.
    admin.post("/vitals/new", data={f"p_{p['id']}": "98", "when_ago": "0"})
    r = admin.post(f"/admin/vitals/{p['id']}/delete", data={"csrf": token}, follow_redirects=False)
    assert "param-archived" in r.headers["location"]
    assert repo.vital_param(conn, p["id"])["active"] == 0


def test_reminder_crud(admin, conn):
    token = csrf_of(admin, "/admin/reminders/new")
    r = admin.post("/admin/reminders/new", data={
        "csrf": token, "name": "Night feeds", "kind": "feeding", "interval_h": "2", "interval_m": "30",
        "window_h": "0", "window_m": "20", "nag_h": "0", "nag_m": "10", "max_nags": "2", "anchor": "start",
        "active_start": "22:00", "active_end": "06:00", "priority": "4", "notify_ntfy": "1", "enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    rem = next(r for r in repo.reminders(conn) if r["name"] == "Night feeds")
    assert rem["interval_minutes"] == 150 and rem["window_minutes"] == 20 and rem["notify_dash"] == 0
    r = admin.post(f"/admin/reminders/{rem['id']}", data={
        "csrf": token, "kind": "feeding", "interval_h": "1", "interval_m": "0", "window_h": "2", "window_m": "0",
    })
    assert r.status_code == 400 and "shorter than the interval" in r.text
    admin.post(f"/admin/reminders/{rem['id']}/delete", data={"csrf": token})
    assert repo.reminder(conn, rem["id"]) is None


def test_trash_restore_and_purge(admin, conn):
    admin.post("/diaper/quick", data={"kind": "wet"})
    d = repo.last_diaper(conn)
    admin.post(f"/entry/diapers/{d['id']}/delete")
    page = admin.get("/admin/trash").text
    assert "Wet diaper" in page
    token = csrf_of(admin, "/admin/trash")
    admin.post("/admin/trash/purge", data={"csrf": token})
    assert repo.get(conn, "diapers", d["id"], include_deleted=True) is None


def test_test_notification_logs_delivery(admin, conn, outbox):
    from tests.conftest import save_settings

    save_settings(conn, ntfy_enabled=1, ntfy_topic="lilypad-t", ntfy_token="tk_x",
                  dash_webhook_enabled=1, dash_webhook_url="http://hooks.local/x",
                  dash_webhook_headers="X-Key: abc")
    token = csrf_of(admin, "/admin/alerts")
    r = admin.post("/admin/test-notification", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert len(outbox.requests) == 2
    ntfy, hook = outbox.requests
    assert ntfy.headers["authorization"] == "Bearer tk_x"
    assert hook.headers["x-key"] == "abc"
    assert json.loads(hook.content)["event"] == "lilypad.alert"
    log = admin.get("/admin/alerts").text
    assert "LilyPad test" in log and "sent" in log

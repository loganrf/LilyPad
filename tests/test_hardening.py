"""Regression tests for issues found in review."""

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app import repo, settings_store
from app.reminders import tick
from app.reports import _nice_bounds
from app.security import LoginThrottle, SessionSigner
from app.timeutil import to_db, utcnow
from tests.conftest import PASSWORD, csrf_of, save_settings


def test_ancient_dates_rejected_and_home_survives(client, conn):
    for value in ("0999-01-01T00:00", "0001-01-01T00:00", "1970-01-01T00:00"):
        r = client.post("/diaper/new", data={"kind": "wet", "when_ago": "custom", "when_at": value})
        assert r.status_code == 400, value
    save_settings(conn, timezone="Asia/Tokyo")
    r = client.post("/diaper/new", data={"kind": "wet", "when_ago": "custom", "when_at": "0001-01-01T00:00"})
    assert r.status_code == 400
    assert client.get("/").status_code == 200


def test_to_db_zero_pads_years():
    from datetime import datetime, timezone
    assert to_db(datetime(999, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "0999-01-02T03:04:05Z"


def test_absurd_vitals_rejected(client, conn):
    weight = next(p for p in repo.vital_params(conn) if p["name"] == "Weight")
    r = client.post("/vitals/new", data={f"p_{weight['id']}": "1e17", "when_ago": "0"})
    assert r.status_code == 400 and "too large" in r.text


def test_axis_ticks_always_terminate():
    started = time.time()
    for lo, hi in ((1e17, 100000000000000016), (5, 5), (0, 0), (-1e6, 1e6), (float("inf"), 1)):
        _, _, ticks = _nice_bounds(lo, hi)
        assert 2 <= len(ticks) <= 21
    assert time.time() - started < 1


def test_cannot_backdate_into_protected_history(client, conn):
    old = (utcnow() - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/note/new", data={"body": "planted", "when_ago": "custom", "when_at": old})
    assert r.status_code == 400 and "older than the open window" in r.text
    client.post("/diaper/quick", data={"kind": "wet"})
    d = repo.last_diaper(conn)
    r = client.post(f"/diaper/{d['id']}/edit", data={"kind": "wet", "when_at": old})
    assert r.status_code == 400
    # An admin may.
    client.post("/admin/login", data={"password": PASSWORD})
    token = csrf_of(client, "/note/new")
    r = client.post("/note/new", data={"body": "late entry", "when_ago": "custom", "when_at": old, "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303


def test_csrf_token_matches_refreshed_session(make_app):
    from app.security import password_fingerprint

    app = make_app()
    st = app.state.lilypad
    fingerprint = password_fingerprint("env:" + st.env_password_digest.hex())
    with TestClient(app, base_url="http://lilypad.test") as c:
        # A session 20 of 30 minutes old: past the half-life, so the page re-issues it.
        old = st.signer.issue(fingerprint, st.config.session_minutes * 60, now=time.time() - 20 * 60)
        c.cookies.set("lilypad_admin", old)
        page = c.get("/admin/settings")
        assert "lilypad_admin" in page.headers.get("set-cookie", "")
        token = page.text.split('name="csrf" value="')[1].split('"')[0]
        r = c.post("/admin/settings", data={"csrf": token, "baby_name": "Lily"}, follow_redirects=False)
        assert r.status_code == 303


def test_cancel_links_are_never_offsite(client):
    for path in ("/diaper/new", "/feed/new", "/note/new", "/vitals/new"):
        for evil in ("https://evil.example/login", "//evil.example", "/\t/evil.example", "javascript:alert(1)"):
            html = client.get(path, params={"next": evil}).text
            assert "evil.example" not in html and "javascript:" not in html, (path, evil)


def test_parallel_logins_cannot_bypass_lockout():
    t = LoginThrottle()
    allowed = sum(1 for _ in range(200) if t.attempt("ip", now=1000) == 0)
    assert allowed == LoginThrottle.FREE_ATTEMPTS


def test_editing_reminder_restarts_cycle(admin, conn, app):
    save_settings(conn, ntfy_enabled=0)
    rid = conn.execute("SELECT id FROM reminders WHERE kind = 'feeding'").fetchone()[0]
    t0 = utcnow() - timedelta(minutes=200)
    repo.insert(conn, "feedings", {"started_at": to_db(t0), "ended_at": to_db(t0), "method": "bottle"})
    notifier = app.state.lilypad.notifier
    assert tick(conn, settings_store.load(conn), notifier, utcnow())  # due/overdue sent
    token = csrf_of(admin, f"/admin/reminders/{rid}")
    admin.post(f"/admin/reminders/{rid}", data={
        "csrf": token, "name": "Feeding", "kind": "feeding", "interval_h": "5", "interval_m": "0",
        "window_h": "0", "window_m": "30", "nag_h": "0", "nag_m": "15", "max_nags": "3", "enabled": "1",
        "notify_dash": "1", "priority": "3",
    })
    later = t0 + timedelta(minutes=300 - 20)  # inside the new heads-up window
    sent = tick(conn, settings_store.load(conn), notifier, later)
    assert [a["stage"] for a in sent] == ["window"]


def test_admin_only_writes_on_open_routes_need_csrf(client, conn):
    old_id = repo.insert(conn, "notes", {"occurred_at": to_db(utcnow() - timedelta(days=5)), "body": "old"})
    client.post("/admin/login", data={"password": PASSWORD})
    # Logged in, but no CSRF token: refused.
    assert client.post(f"/entry/notes/{old_id}/delete").status_code == 403
    assert repo.get(conn, "notes", old_id) is not None
    page = client.get(f"/note/{old_id}/edit")
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    r = client.post(f"/entry/notes/{old_id}/delete", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and repo.get(conn, "notes", old_id) is None


def test_ipv6_origin_ports_compared():
    from app.security import host_allowed, origin_allowed
    assert not origin_allowed("http://[fd00::5]:9999", ["[fd00::5]:8080"], [])
    assert origin_allowed("http://[fd00::5]:8080", ["[fd00::5]:8080"], [])
    assert host_allowed("[fd00::5]:8080", ["fd00::5"])
    assert not host_allowed("evil.example", ["fd00::5"])


def test_old_running_feed_cannot_be_stopped_anonymously(client, conn):
    fid = repo.insert(conn, "feedings", {"started_at": to_db(utcnow() - timedelta(days=60)), "method": "breast"})
    r = client.post(f"/feed/{fid}/stop", follow_redirects=False)
    assert r.headers["location"].startswith("/admin/login")
    assert repo.get(conn, "feedings", fid)["ended_at"] is None
    r = client.post(f"/feed/{fid}/side", data={"side": "left"}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin/login")


def test_malformed_ids_are_404_not_500(admin):
    assert admin.get("/feed/99999999999999999999/edit").status_code == 404
    assert admin.post("/api/alerts/99999999999999999999/ack").status_code == 404
    assert admin.get("/admin/vitals/²").status_code == 404
    assert admin.get("/admin/reminders/0").status_code == 404


def test_non_ascii_cookie_does_not_crash(client):
    signer = SessionSigner("s")
    assert signer.verify("v1.1.9999999999.n.fp.éé", "fp") is None
    client.cookies.set("lilypad_admin", "v1.1.9999999999.n.fp.%C3%A9")
    assert client.get("/admin", follow_redirects=False).status_code == 303

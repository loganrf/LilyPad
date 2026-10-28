"""Open (password-free) logging flows."""

from datetime import timedelta

from app import repo
from app.timeutil import from_db, to_db, utcnow


def test_home_renders_without_data(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No feeds yet" in r.text
    assert "Lily" in r.text


def test_feed_timer_start_stop_and_details(client, conn):
    r = client.post("/feed/start", data={"method": "breast", "side": "left"}, follow_redirects=False)
    assert r.status_code == 303
    assert "toast=feed-started" in r.headers["location"]
    active = repo.active_feeding(conn)
    assert active and active["side"] == "left" and active["ended_at"] is None

    # A second start is refused while one is running.
    r = client.post("/feed/start", data={"method": "breast", "side": "right"}, follow_redirects=False)
    assert "feed-active" in r.headers["location"]

    home = client.get("/")
    assert "Stop feed" in home.text

    client.post(f"/feed/{active['id']}/side", data={"side": "both"})
    assert repo.get(conn, "feedings", active["id"])["side"] == "both"

    r = client.post(f"/feed/{active['id']}/stop", follow_redirects=False)
    assert r.headers["location"].startswith(f"/feed/{active['id']}/edit")
    stopped = repo.get(conn, "feedings", active["id"])
    assert stopped["ended_at"] is not None

    page = client.get(f"/feed/{active['id']}/edit")
    assert page.status_code == 200
    r = client.post(f"/feed/{active['id']}/edit", data={
        "method": "breast", "side": "both", "start_at": page.text.split('name="start_at" value="')[1].split('"')[0],
        "duration": "22", "quality": "3", "issues": ["spit_up", "gassy", "bogus"], "notes": "good latch",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    saved = repo.get(conn, "feedings", active["id"])
    assert saved["quality"] == 3
    assert saved["issues"] == "spit_up,gassy"
    assert round(repo.feeding_minutes(saved)) == 22


def test_log_past_bottle_feed_in_ounces(client, conn):
    from tests.conftest import save_settings

    save_settings(conn, volume_unit="oz")
    r = client.post("/feed/new", data={
        "method": "bottle", "bottle_content": "formula", "amount": "3", "start_ago": "30", "duration": "15",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    feed = repo.last_feeding(conn)
    assert feed["method"] == "bottle" and feed["side"] is None
    assert abs(feed["amount_ml"] - 88.7) < 0.2
    started = from_db(feed["started_at"])
    assert abs((utcnow() - started) - timedelta(minutes=30)) < timedelta(minutes=1)


def test_feed_validation_errors_rerender(client):
    r = client.post("/feed/new", data={"method": "bottle", "start_ago": "0", "duration": "999"})
    assert r.status_code == 400
    assert "Duration must be at most" in r.text
    r = client.post("/feed/new", data={"start_ago": "0"})
    assert r.status_code == 400
    assert "Choose a feeding method" in r.text


def test_future_time_rejected(client):
    future = (utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/diaper/new", data={"kind": "wet", "when_ago": "custom", "when_at": future})
    assert r.status_code == 400
    assert "in the future" in r.text


def test_one_tap_wet_and_detailed_dirty(client, conn):
    r = client.post("/diaper/quick", data={"kind": "wet"}, follow_redirects=False)
    assert "undo=diapers" in r.headers["location"]
    r = client.post("/diaper/new", data={
        "kind": "dirty", "color": "pale", "consistency": "soft", "amount": "medium", "when_ago": "5",
    }, follow_redirects=False)
    assert r.status_code == 303
    dirty = repo.last_diaper(conn, ["dirty"])
    assert dirty["color"] == "pale"
    home = client.get("/").text
    assert "liver problem" in home  # stool color flag surfaces on the home screen


def test_wet_diaper_ignores_stool_fields(client, conn):
    client.post("/diaper/new", data={"kind": "wet", "color": "red", "when_ago": "0"})
    d = repo.last_diaper(conn)
    assert d["kind"] == "wet" and d["color"] is None


def test_note_for_pediatrician(client, conn):
    r = client.post("/note/new", data={
        "body": "Rash behind left ear", "tags": "Skin, rash, skin", "for_pediatrician": "1", "when_ago": "0",
    }, follow_redirects=False)
    assert r.status_code == 303
    notes = repo.pediatrician_notes(conn)
    assert len(notes) == 1 and notes[0]["tags"] == "skin, rash"
    r = client.post("/note/new", data={"body": "  ", "when_ago": "0"})
    assert r.status_code == 400


def test_vitals_with_alert_sends_notification(client, conn, outbox):
    from tests.conftest import save_settings

    save_settings(conn, ntfy_enabled=1, ntfy_topic="lilypad-test-topic", notify_detail="standard")
    temp = next(p for p in repo.vital_params(conn) if p["name"] == "Temperature")
    r = client.post("/vitals/new", data={f"p_{temp['id']}": "100.9", "when_ago": "0"}, follow_redirects=False)
    assert "toast=vitals-alert" in r.headers["location"]
    bodies = outbox.bodies()
    assert len(bodies) == 1
    assert bodies[0]["topic"] == "lilypad-test-topic"
    assert bodies[0]["priority"] == 5
    assert "100.9" in bodies[0]["title"]
    assert "Lily" not in bodies[0]["title"]  # standard detail level omits the name
    alert = conn.execute("SELECT * FROM alerts WHERE source = 'vital'").fetchone()
    assert alert is not None
    home = client.get("/").text
    assert "Acknowledge" in home
    client.post(f"/api/alerts/{alert['id']}/ack")
    assert "Acknowledge" not in client.get("/").text


def test_vitals_watch_range_does_not_notify(client, conn, outbox):
    temp = next(p for p in repo.vital_params(conn) if p["name"] == "Temperature")
    r = client.post("/vitals/new", data={f"p_{temp['id']}": "97.2", "when_ago": "0"}, follow_redirects=False)
    assert "toast=vitals-watch" in r.headers["location"]
    assert outbox.requests == []


def test_vitals_requires_a_value(client):
    r = client.post("/vitals/new", data={"when_ago": "0"})
    assert r.status_code == 400
    assert "at least one reading" in r.text


def test_reading_keeps_limits_snapshot(client, conn):
    temp = next(p for p in repo.vital_params(conn) if p["name"] == "Temperature")
    client.post("/vitals/new", data={f"p_{temp['id']}": "100.5", "when_ago": "0"})
    conn.execute("UPDATE vital_params SET alert_high = 102 WHERE id = ?", (temp["id"],))
    reading = repo.latest_reading(conn, temp["id"])
    assert repo.reading_status(reading["value"], reading) == "alert"


def test_custom_routine_done(client, conn):
    conn.execute("UPDATE reminders SET enabled = 1 WHERE kind = 'custom'")
    rid = conn.execute("SELECT id FROM reminders WHERE kind = 'custom'").fetchone()[0]
    assert "Routines" in client.get("/").text
    r = client.post(f"/task/{rid}/done", follow_redirects=False)
    assert r.status_code == 303
    assert repo.last_task_done(conn, rid) is not None


def test_delete_undo_restore(client, conn):
    client.post("/diaper/quick", data={"kind": "wet"})
    d = repo.last_diaper(conn)
    r = client.post(f"/entry/diapers/{d['id']}/delete", data={"next": "/timeline"}, follow_redirects=False)
    assert r.headers["location"].startswith("/timeline")
    assert "undo_action=restore" in r.headers["location"]
    assert repo.get(conn, "diapers", d["id"]) is None
    client.post(f"/entry/diapers/{d['id']}/restore", data={"next": "/"})
    assert repo.get(conn, "diapers", d["id"]) is not None


def test_restore_after_undo_window_needs_admin(client, conn):
    client.post("/diaper/quick", data={"kind": "wet"})
    d = repo.last_diaper(conn)
    old = to_db(utcnow() - timedelta(hours=2))
    conn.execute("UPDATE diapers SET deleted_at = ? WHERE id = ?", (old, d["id"]))
    r = client.post(f"/entry/diapers/{d['id']}/restore", data={"next": "/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin/login")
    assert repo.get(conn, "diapers", d["id"]) is None


def test_unknown_table_404(client):
    assert client.post("/entry/settings/1/delete").status_code == 404


def test_timeline_shows_recent_only(client, conn):
    client.post("/note/new", data={"body": "recent note", "when_ago": "0"})
    old = to_db(utcnow() - timedelta(days=5))
    repo.insert(conn, "notes", {"occurred_at": old, "body": "ancient secret note"})
    page = client.get("/timeline").text
    assert "recent note" in page
    assert "ancient secret note" not in page


def test_who_cookie(client, conn):
    from tests.conftest import save_settings

    save_settings(conn, caregivers="Sam, Alex")
    client.post("/who", data={"who": "Alex", "next": "/"})
    client.post("/diaper/quick", data={"kind": "wet"})
    assert repo.last_diaper(conn)["logged_by"] == "Alex"
    client.post("/who", data={"who": "Mallory", "next": "/"})
    client.post("/diaper/quick", data={"kind": "dry"})
    assert repo.last_diaper(conn)["logged_by"] == ""


def test_dash_json(client, conn):
    client.post("/feed/new", data={"method": "breast", "side": "left", "start_ago": "60", "duration": "15"})
    client.post("/diaper/quick", data={"kind": "wet"})
    data = client.get("/api/dash").json()
    assert data["baby"]["name"] == "Lily"
    assert data["feeding"]["last"]["side"] == "left"
    assert data["feeding"]["suggested_side"] == "right"
    assert data["last_24h"]["feeds"] == 1 and data["last_24h"]["wet"] == 1
    assert any(r["kind"] == "feeding" for r in data["reminders"])
    assert "_statuses" not in data


def test_snooze_api(client, conn):
    rid = conn.execute("SELECT id FROM reminders WHERE kind = 'feeding'").fetchone()[0]
    r = client.post(f"/api/reminders/{rid}/snooze?minutes=20")
    assert r.status_code == 200 and r.json()["ok"]
    assert client.post("/api/reminders/999/snooze").status_code == 404

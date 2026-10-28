"""Reminder engine: windows, due, nagging, snooze, resets and active hours."""

from datetime import datetime, timedelta, timezone

import pytest

from app import repo, settings_store
from app.reminders import compute, snooze, tick
from app.timeutil import to_db
from tests.conftest import save_settings

T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(app, conn):
    save_settings(conn, ntfy_enabled=1, ntfy_topic="lilypad-test", base_url="http://lily.lan:8080")
    conn.execute("UPDATE reminders SET enabled = 0")
    conn.execute(
        """INSERT INTO reminders (name, kind, interval_minutes, window_minutes, nag_minutes, max_nags,
           priority, created_at, updated_at) VALUES ('Feeding', 'feeding', 180, 30, 15, 2, 3, ?, ?)""",
        (to_db(T0), to_db(T0)),
    )
    rid = conn.execute("SELECT max(id) FROM reminders").fetchone()[0]
    repo.insert(conn, "feedings", {"started_at": to_db(T0), "ended_at": to_db(T0 + timedelta(minutes=20)),
                                   "method": "breast", "side": "left"})
    notifier = app.state.lilypad.notifier
    return conn, rid, notifier


def run(conn, notifier, at):
    return tick(conn, settings_store.load(conn), notifier, at)


def test_cycle_window_due_nags(setup, outbox):
    conn, rid, notifier = setup
    assert run(conn, notifier, T0 + timedelta(minutes=100)) == []
    sent = run(conn, notifier, T0 + timedelta(minutes=150))
    assert [a["stage"] for a in sent] == ["window"]
    assert run(conn, notifier, T0 + timedelta(minutes=160)) == []  # not repeated
    assert [a["stage"] for a in run(conn, notifier, T0 + timedelta(minutes=180))] == ["due"]
    assert run(conn, notifier, T0 + timedelta(minutes=190)) == []
    assert [a["stage"] for a in run(conn, notifier, T0 + timedelta(minutes=195))] == ["overdue"]
    assert [a["stage"] for a in run(conn, notifier, T0 + timedelta(minutes=210))] == ["overdue"]
    assert run(conn, notifier, T0 + timedelta(minutes=240)) == []  # max_nags reached
    bodies = outbox.bodies()
    assert [b["priority"] for b in bodies] == [2, 3, 4, 4]
    assert bodies[0]["title"] == "Feeding window open"
    assert bodies[1]["click"] == "http://lily.lan:8080/"
    assert any(a["label"].startswith("Snooze") for a in bodies[1]["actions"])


def test_logging_resets_cycle(setup):
    conn, rid, notifier = setup
    run(conn, notifier, T0 + timedelta(minutes=185))
    repo.insert(conn, "feedings", {"started_at": to_db(T0 + timedelta(minutes=186)),
                                   "ended_at": to_db(T0 + timedelta(minutes=200)), "method": "bottle"})
    assert run(conn, notifier, T0 + timedelta(minutes=200)) == []
    status = compute(conn, repo.reminder(conn, rid), settings_store.load(conn), T0 + timedelta(minutes=200))
    assert status.stage == "ok"
    assert status.due_at == T0 + timedelta(minutes=186 + 180)


def test_in_progress_feed_suppresses(setup):
    conn, rid, notifier = setup
    repo.insert(conn, "feedings", {"started_at": to_db(T0 + timedelta(minutes=170)), "method": "breast"})
    assert run(conn, notifier, T0 + timedelta(minutes=175)) == []
    status = compute(conn, repo.reminder(conn, rid), settings_store.load(conn), T0 + timedelta(minutes=175))
    assert status.stage == "in_progress"
    # A forgotten timer stops suppressing after STALE_FEED_MINUTES.
    late = T0 + timedelta(minutes=170 + 200)
    status = compute(conn, repo.reminder(conn, rid), settings_store.load(conn), late)
    assert status.stage != "in_progress"


def test_end_anchor(setup):
    conn, rid, notifier = setup
    conn.execute("UPDATE reminders SET anchor = 'end' WHERE id = ?", (rid,))
    status = compute(conn, repo.reminder(conn, rid), settings_store.load(conn), T0)
    assert status.due_at == T0 + timedelta(minutes=20 + 180)


def test_snooze(setup):
    conn, rid, notifier = setup
    run(conn, notifier, T0 + timedelta(minutes=181))
    snooze(conn, repo.reminder(conn, rid), settings_store.load(conn), 30, T0 + timedelta(minutes=182))
    assert run(conn, notifier, T0 + timedelta(minutes=200)) == []  # nag suppressed
    status = compute(conn, repo.reminder(conn, rid), settings_store.load(conn), T0 + timedelta(minutes=200))
    assert status.stage == "snoozed"
    sent = run(conn, notifier, T0 + timedelta(minutes=213))
    assert [a["stage"] for a in sent] == ["overdue"]  # snooze over: remind again


def test_restart_does_not_resend_or_flood(setup):
    conn, rid, notifier = setup
    # Server was down through the window and several nag intervals: one alert only.
    sent = run(conn, notifier, T0 + timedelta(minutes=230))
    assert [a["stage"] for a in sent] == ["overdue"]
    assert run(conn, notifier, T0 + timedelta(minutes=231)) == []


def test_active_hours(setup):
    conn, rid, notifier = setup
    save_settings(conn, timezone="UTC")
    conn.execute("UPDATE reminders SET active_start = '16:00', active_end = '22:00' WHERE id = ?", (rid,))
    assert run(conn, notifier, T0 + timedelta(minutes=181)) == []  # 15:01 UTC: paused
    sent = run(conn, notifier, T0 + timedelta(minutes=241))  # 16:01: inside the window
    assert [a["stage"] for a in sent] == ["overdue"]


def test_channel_switches_and_detail_levels(setup, outbox):
    conn, rid, notifier = setup
    conn.execute("UPDATE reminders SET notify_ntfy = 0 WHERE id = ?", (rid,))
    sent = run(conn, notifier, T0 + timedelta(minutes=181))
    assert sent and outbox.requests == []
    assert sent[0]["delivery"]["ntfy"] == "off for this reminder"


def test_minimal_detail_leaks_nothing(setup, outbox):
    conn, rid, notifier = setup
    save_settings(conn, notify_detail="minimal", baby_name="Juniper",
                  dash_webhook_enabled=1, dash_webhook_url="http://hooks.local/x")
    run(conn, notifier, T0 + timedelta(minutes=181))
    ntfy, hook = outbox.bodies()
    text = ntfy["title"] + ntfy["message"]
    assert "Juniper" not in text and "Feeding" not in text
    assert hook["baby"] is None and hook["context"] == {}
    assert "Feeding" not in hook["alert"]["title"] + hook["alert"]["message"]


def test_detailed_includes_specifics(setup, outbox):
    conn, rid, notifier = setup
    save_settings(conn, notify_detail="detailed", baby_name="Lily")
    run(conn, notifier, T0 + timedelta(minutes=181))
    body = outbox.bodies()[0]
    assert body["title"].startswith("Lily:")
    assert "left" in body["message"].lower()


def test_ntfy_failure_is_logged_not_raised(setup, outbox):
    conn, rid, notifier = setup
    outbox.status = 500
    sent = run(conn, notifier, T0 + timedelta(minutes=181))
    assert sent[0]["delivery"]["ntfy"].startswith("error")
    assert sent[0]["delivery"]["dash"] == "shown"


def test_custom_routine_falls_back_to_reminder_time(app, conn):
    conn.execute("UPDATE reminders SET enabled = 1, updated_at = ? WHERE kind = 'custom'", (to_db(T0),))
    rem = conn.execute("SELECT * FROM reminders WHERE kind = 'custom'").fetchone()
    status = compute(conn, dict(rem), settings_store.load(conn), T0 + timedelta(hours=1))
    assert not status.anchor_from_event
    assert status.due_at == T0 + timedelta(minutes=rem["interval_minutes"])


def test_diaper_kind_filters(app, conn):
    conn.execute(
        """INSERT INTO reminders (name, kind, interval_minutes, created_at, updated_at)
           VALUES ('Poop check', 'diaper_dirty', 1440, ?, ?)""", (to_db(T0), to_db(T0)))
    rem = dict(conn.execute("SELECT * FROM reminders WHERE kind = 'diaper_dirty'").fetchone())
    repo.insert(conn, "diapers", {"occurred_at": to_db(T0 + timedelta(hours=1)), "kind": "dirty"})
    repo.insert(conn, "diapers", {"occurred_at": to_db(T0 + timedelta(hours=2)), "kind": "wet"})
    status = compute(conn, rem, settings_store.load(conn), T0 + timedelta(hours=3))
    assert status.anchor == T0 + timedelta(hours=1)

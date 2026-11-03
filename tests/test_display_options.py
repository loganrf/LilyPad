"""How vitals show their check time, and one-tap diaper logging on the dash."""

import re

import pytest

from app import repo, settings_store
from app.timeutil import fmt_datetime, from_db, get_zone
from tests.conftest import csrf_of, save_settings


def _log_temperature(client, conn, minutes_ago="5") -> dict:
    temp = next(p for p in repo.vital_params(conn) if p["name"] == "Temperature")
    r = client.post("/vitals/new", data={f"p_{temp['id']}": "98.6", "when_ago": minutes_ago},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return repo.latest_reading(conn, temp["id"])


def _home_vital(client, name="Temperature") -> tuple[str, str]:
    """(raw HTML, visible text) of one vital's row on the home screen."""
    html = client.get("/").text
    m = re.search(r'<li class="vital [^"]*">\s*<span class="vital-name">' + name + r"</span>(.*?)</li>",
                  html, re.S)
    assert m, "vital row not found"
    return m.group(1), " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())


def _checked_at(conn, reading) -> str:
    tz = get_zone(settings_store.load(conn).timezone_name)
    return fmt_datetime(from_db(reading["measured_at"]), tz)


def test_vitals_show_time_since_by_default(client, conn):
    reading = _log_temperature(client, conn)
    html, text = _home_vital(client)
    assert "5m ago" in text and "data-ago" in html
    assert _checked_at(conn, reading) not in text


def test_vitals_can_show_the_check_date(client, conn):
    reading = _log_temperature(client, conn)
    save_settings(conn, vitals_time_display="date")
    html, text = _home_vital(client)
    assert _checked_at(conn, reading) in text
    assert "ago" not in text and "data-ago" not in html


def test_vitals_can_show_both(client, conn):
    reading = _log_temperature(client, conn)
    save_settings(conn, vitals_time_display="both")
    html, text = _home_vital(client)
    assert f"{_checked_at(conn, reading)} · 5m ago" in text and "data-ago" in html


def test_dash_json_carries_display_options(client, conn):
    data = client.get("/api/dash").json()
    assert data["vitals_time_display"] == "ago" and data["dash_diaper_buttons"] is False
    save_settings(conn, vitals_time_display="both", dash_diaper_buttons="1")
    data = client.get("/api/dash").json()
    assert data["vitals_time_display"] == "both" and data["dash_diaper_buttons"] is True
    save_settings(conn, vitals_time_display="sideways")
    assert client.get("/api/dash").json()["vitals_time_display"] == "ago"


def test_settings_form_saves_display_options(admin, conn):
    token = csrf_of(admin)
    r = admin.post("/admin/settings", data={"csrf": token, "baby_name": "Lily", "vitals_time_display": "date",
                                            "dash_diaper_buttons": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    s = settings_store.load(conn)
    assert s.vitals_time_display == "date" and s.flag("dash_diaper_buttons")
    page = admin.get("/admin/settings").text
    assert re.search(r'name="vitals_time_display" value="date" checked', page)
    assert re.search(r'name="dash_diaper_buttons" value="1" checked', page)
    # Unknown values fall back to the default; an unticked box turns the buttons off.
    admin.post("/admin/settings", data={"csrf": token, "baby_name": "Lily", "vitals_time_display": "x"})
    s = settings_store.load(conn)
    assert s["vitals_time_display"] == "ago" and not s.flag("dash_diaper_buttons")


def test_dash_shows_diaper_buttons_only_when_enabled(client, conn):
    assert "data-diaper-log hidden" in client.get("/dash").text
    save_settings(conn, dash_diaper_buttons="1")
    page = client.get("/dash").text
    assert "data-diaper-log hidden" not in page
    # Without JavaScript the buttons still post the one-tap form.
    form = re.search(r'<form method="post" action="/diaper/quick"[^>]*>(.*?)</form>', page, re.S)
    assert form and re.findall(r'value="(\w+)"', form.group(1)) == ["wet", "dirty", "mixed"]


@pytest.mark.parametrize("kind", ["wet", "dirty", "mixed", "dry"])
def test_api_logs_a_diaper(client, conn, kind):
    r = client.post(f"/api/diapers?kind={kind}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["kind"] == kind and body["occurred_at"].endswith("Z")
    row = repo.get(conn, "diapers", body["id"])
    assert row["kind"] == kind and row["occurred_at"] == body["occurred_at"]


def test_api_diaper_takes_a_form_field_and_the_caregiver(client, conn):
    save_settings(conn, caregivers="Sam, Alex")
    client.post("/who", data={"who": "Sam", "next": "/"})
    r = client.post("/api/diapers", data={"kind": "dirty"})
    assert r.status_code == 200 and r.json()["kind_label"] == "Dirty"
    assert repo.last_diaper(conn)["logged_by"] == "Sam"


@pytest.mark.parametrize("query", ["", "?kind=", "?kind=poop", "?kind=wet%00"])
def test_api_diaper_rejects_unknown_kinds(client, conn, query):
    assert client.post("/api/diapers" + query).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM diapers").fetchone()[0] == 0


def test_api_diaper_blocks_other_sites(client, conn):
    r = client.post("/api/diapers?kind=wet", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM diapers").fetchone()[0] == 0


def test_dash_undo_returns_to_the_dash(client, conn):
    diaper_id = client.post("/api/diapers?kind=wet").json()["id"]
    r = client.post(f"/entry/diapers/{diaper_id}/delete", data={"undo": "1", "next": "/dash"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dash?toast=undone")
    assert repo.get(conn, "diapers", diaper_id) is None

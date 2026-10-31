"""Direct notifications to Dash LED clocks."""

import json

import httpx
import pytest

from app import dash_clock, settings_store
from app.events import EventBus
from app.notify import Notification, Notifier
from tests.conftest import csrf_of, save_settings

CLOCK = "http://192.168.1.63"


# -- text measurement and timing ------------------------------------------------------

def test_text_width_matches_firmware():
    # Same expectations as the firmware's own unit tests (test_core/test_main.cpp).
    assert dash_clock.text_width("") == 0
    assert dash_clock.text_width("12:34") == 17
    assert dash_clock.text_width("12:34:56") == 27
    assert dash_clock.text_width("é") == dash_clock.text_width("?")
    assert dash_clock.text_width("Check Lily") == 35


def test_scrolling_text_runs_for_requested_minutes():
    # 35 px of text + 32 px of display = 67 px per pass, 2.68 s at 25 px/s.
    assert dash_clock.timing("Check Lily", 600) == {"repeat": 224, "scroll_speed": 25}


def test_text_that_fits_uses_duration():
    assert dash_clock.timing("Feed", 600) == {"duration": 600}
    assert dash_clock.timing("Feed", 0) == {"duration": 0}


def test_until_dismissed_scrolls_forever():
    assert dash_clock.timing("Check Lily", 0) == {"repeat": 0, "scroll_speed": 25}


@pytest.mark.parametrize("text", ["Check Lily", "Lily", "x" * 9, "Feeding overdue: last feed 3h ago"])
@pytest.mark.parametrize("minutes", [1, 5, 10, 30])
def test_timing_stays_within_firmware_limits(text, minutes):
    fields = dash_clock.timing(text, minutes * 60)
    if "duration" in fields:
        assert 0 <= fields["duration"] <= dash_clock.MAX_DURATION_SECONDS
        return
    assert 1 <= fields["repeat"] <= dash_clock.MAX_REPEAT
    assert 1 <= fields["scroll_speed"] <= 100
    travel = dash_clock.DISPLAY_WIDTH + dash_clock.text_width(text)
    shown = fields["repeat"] * travel / fields["scroll_speed"]
    assert abs(shown - minutes * 60) <= max(travel / fields["scroll_speed"], 0.05 * minutes * 60)


def test_render_text_and_payload():
    payload = dash_clock.build_payload("Check {baby}", "#00FF00", 10, baby="Lily", title="Feeding due")
    assert payload == {"text": "Check Lily", "color": "#00FF00", "font": "small",
                       "repeat": 224, "scroll_speed": 25}
    assert dash_clock.render_text("{title}!", baby="Lily", title="Feeding due") == "Feeding due!"
    assert dash_clock.render_text("  ", baby="Lily", title="x") == "Check Lily"
    assert len(dash_clock.render_text("a" * 400, baby="", title="").encode()) == 256
    assert dash_clock.build_payload("x", "red", 1, baby="", title="")["color"] == dash_clock.DEFAULT_COLOR


def test_parse_addresses():
    raw = f" {CLOCK}/ \n\n{CLOCK}\nhttp://dash-a1b2c3.local/api/notify\n"
    assert dash_clock.parse_addresses(raw) == [CLOCK, "http://dash-a1b2c3.local"]
    assert dash_clock.address_errors("192.168.1.63")


# -- delivery --------------------------------------------------------------------------

class Clocks:
    def __init__(self, down=()):
        self.requests: list[httpx.Request] = []
        self.down = set(down)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.down:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr("app.notify.time.sleep", lambda s: None)


def deliver(conn, clocks, **note):
    notifier = Notifier(EventBus(), transport=httpx.MockTransport(clocks.handler))
    defaults = dict(title="Feeding due", message="Due now.", source="reminder", stage="due", reminder_id=1)
    return notifier.deliver(conn, settings_store.load(conn), Notification(**{**defaults, **note}))


def test_delivers_to_clock(conn):
    save_settings(conn, dash_clock_enabled=1, dash_clock_urls=CLOCK, baby_name="Lily")
    clocks = Clocks()
    alert = deliver(conn, clocks)
    assert alert["delivery"]["clock"] == "sent"
    (req,) = clocks.requests
    assert str(req.url) == CLOCK + "/api/notify"
    assert json.loads(req.content) == {"text": "Check Lily", "color": "#FF4080", "font": "small",
                                       "repeat": 224, "scroll_speed": 25}


def test_not_configured_by_default(conn):
    clocks = Clocks()
    assert deliver(conn, clocks)["delivery"]["clock"] == "not configured"
    assert clocks.requests == []


def test_respects_reminder_dash_switch(conn):
    save_settings(conn, dash_clock_enabled=1, dash_clock_urls=CLOCK)
    clocks = Clocks()
    assert deliver(conn, clocks, to_dash=False)["delivery"]["clock"] == "off for this reminder"
    assert clocks.requests == []


def test_window_stage_skipped_unless_asked(conn):
    save_settings(conn, dash_clock_enabled=1, dash_clock_urls=CLOCK)
    clocks = Clocks()
    assert deliver(conn, clocks, stage="window")["delivery"]["clock"] == "skipped (window open)"
    assert clocks.requests == []
    save_settings(conn, dash_clock_skip_window=0)
    assert deliver(conn, clocks, stage="window")["delivery"]["clock"] == "sent"


def test_multiple_clocks_and_partial_failure(conn):
    save_settings(conn, dash_clock_enabled=1,
                  dash_clock_urls=f"{CLOCK}\nhttp://192.168.1.64\nhttp://dash.lan:8080",
                  dash_clock_headers="Authorization: Basic abc")
    clocks = Clocks(down={"192.168.1.64"})
    result = deliver(conn, clocks)["delivery"]["clock"]
    assert result.startswith("error: 1 of 3 clocks failed") and "192.168.1.64" in result
    assert {r.url.host for r in clocks.requests} == {"192.168.1.63", "192.168.1.64", "dash.lan"}
    assert all(r.headers["authorization"] == "Basic abc" for r in clocks.requests)

    clocks = Clocks()
    assert deliver(conn, clocks)["delivery"]["clock"] == "sent to 3 clocks"


def test_clock_failure_is_logged_not_raised(conn):
    save_settings(conn, dash_clock_enabled=1, dash_clock_urls=CLOCK)
    alert = deliver(conn, Clocks(down={"192.168.1.63"}))
    assert alert["delivery"]["clock"].startswith("error:")
    row = conn.execute("SELECT delivery FROM alerts WHERE id = ?", (alert["id"],)).fetchone()
    assert json.loads(row["delivery"])["clock"].startswith("error:")


# -- admin -------------------------------------------------------------------------------

def settings_form(admin, **overrides):
    data = {"csrf": csrf_of(admin), "baby_name": "Lily", "open_window_hours": "48", "volume_unit": "ml",
            "ntfy_server": "https://ntfy.sh", "notify_detail": "standard",
            "dash_clock_enabled": "1", "dash_clock_urls": f"{CLOCK}/\r\n{CLOCK}",
            "dash_clock_text": "Check {baby}", "dash_clock_color": "#00C0FF", "dash_clock_minutes": "10",
            "dash_clock_skip_window": "1", "dash_clock_headers": "Authorization: Basic abc"}
    data.update(overrides)
    return admin.post("/admin/settings", data=data, follow_redirects=False)


def test_settings_save_and_render(admin, conn):
    assert settings_form(admin).status_code == 303
    s = settings_store.load(conn)
    assert s.dash_clock_ready and s["dash_clock_urls"] == CLOCK
    assert s["dash_clock_color"] == "#00C0FF" and s.dash_clock_minutes == 10
    assert s["dash_clock_headers"] == "Authorization: Basic abc"
    page = admin.get("/admin/settings").text
    assert CLOCK in page and "Basic abc" not in page
    assert "Dash clocks: <strong>on</strong>" in admin.get("/admin").text


@pytest.mark.parametrize("field,value,message", [
    ("dash_clock_urls", "192.168.1.63", "must start with http"),
    ("dash_clock_urls", "", "Add a Dash clock address"),
    ("dash_clock_color", "pink", "#FF4080"),
    ("dash_clock_minutes", "45", "Dash clock minutes"),
])
def test_settings_validation(admin, field, value, message):
    r = settings_form(admin, **{field: value})
    assert r.status_code == 400 and message in r.text


def test_test_notification_reaches_clock(admin, conn, outbox):
    save_settings(conn, dash_clock_enabled=1, dash_clock_urls=CLOCK)
    token = csrf_of(admin, "/admin/alerts")
    admin.post("/admin/test-notification", data={"csrf": token})
    (req,) = outbox.requests
    assert str(req.url) == CLOCK + "/api/notify"
    assert json.loads(req.content)["text"] == "Check Lily"


def test_export_leaves_out_clock_headers(conn):
    assert "dash_clock_headers" in settings_store.SECRET_KEYS

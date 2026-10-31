import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, settings_store
from app.config import Config
from app.events import EventBus
from app.main import create_app
from app.notify import Notifier

PASSWORD = "correct horse battery"


class Outbox:
    """Captures outbound HTTP (ntfy + webhook) instead of hitting the network."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"id": "x"})

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def make_app(tmp_path, outbox):
    def factory(**overrides):
        config = Config(data_dir=tmp_path, scheduler_enabled=False, admin_password=overrides.pop("admin_password", PASSWORD),
                        **overrides)
        bus = EventBus()
        notifier = Notifier(bus, transport=httpx.MockTransport(outbox.handler))
        app = create_app(config, notifier=notifier)
        # The notifier must share the app's bus so SSE subscribers see alerts.
        app.state.lilypad.notifier.bus = app.state.lilypad.bus
        return app
    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://lilypad.test") as c:
        yield c


@pytest.fixture
def conn(app):
    c = db.connect(app.state.lilypad.config.db_path)
    yield c
    c.close()


@pytest.fixture
def admin(client):
    """The same client, logged in to the admin area."""
    r = client.post("/admin/login", data={"password": PASSWORD, "next": "/admin"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def csrf_of(client, path="/admin/settings") -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


def save_settings(conn, **values):
    settings_store.save(conn, {k: str(v) for k, v in values.items()})

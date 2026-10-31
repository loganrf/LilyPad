"""Installable web app (PWA): manifests, icons, service worker, offline page."""

import json
import re
import struct

from app.web import STATIC_DIR, static_fingerprint
from tests.conftest import save_settings


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def check_icons(client, icons):
    for icon in icons:
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        if icon["type"] == "image/png":
            w, h = png_size(r.content)
            assert icon["sizes"] == f"{w}x{h}", icon["src"]


def test_manifest_is_installable(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["id"] == "/" and m["start_url"] == "/" and m["scope"] == "/"
    assert m["display"] == "standalone"
    assert m["name"] and m["short_name"]
    check_icons(client, m["icons"])
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    for required in (("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"), ("512x512", "maskable")):
        assert required in sizes
    for shortcut in m["shortcuts"]:
        assert client.get(shortcut["url"]).status_code == 200, shortcut["url"]
        check_icons(client, [dict(i, type=i.get("type", "")) for i in shortcut.get("icons", [])])


def test_dash_has_its_own_fullscreen_manifest(client):
    m = json.loads(client.get("/static/dash.webmanifest").text)
    assert m["id"] == "/dash" and m["start_url"] == "/dash"
    assert m["display"] == "fullscreen"
    check_icons(client, m["icons"])
    page = client.get("/dash").text
    assert 'rel="manifest" href="/static/dash.webmanifest"' in page
    assert "/static/pwa.js?v=" in page


def test_pages_link_manifest_and_register_service_worker(client):
    for path in ("/", "/timeline", "/privacy", "/admin/login", "/diaper/new"):
        page = client.get(path).text
        assert 'rel="manifest" href="/static/manifest.webmanifest"' in page, path
        assert "/static/pwa.js?v=" in page, path
        assert 'rel="apple-touch-icon"' in page, path


def precache_urls(sw: str) -> list[str]:
    version = re.search(r'var VERSION = "([0-9a-f]+)";', sw).group(1)
    block = re.search(r"var PRECACHE = \[(.*?)\];", sw, re.S).group(1)
    block = block.replace("OFFLINE_URL", '"%s"' % re.search(r'var OFFLINE_URL = "([^"]+)";', sw).group(1))
    return [url + (version if plus else "") for url, plus in re.findall(r'"(/[^"]*)"( \+ VERSION)?', block)]


def test_service_worker_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"  # browsers must see updates promptly
    assert f'var VERSION = "{static_fingerprint()}";' in r.text
    for url in precache_urls(r.text):
        assert client.get(url).status_code == 200, url


def test_offline_page_works_from_the_precache(client):
    """Every script and stylesheet on the offline page must be precached."""
    precached = set(precache_urls(client.get("/sw.js").text))
    assert "/offline" in precached
    page = client.get("/offline").text
    assets = re.findall(r'<(?:script src|link rel="stylesheet" href)="([^"]+)"', page)
    assert assets and set(assets) <= precached


def test_service_worker_never_stores_pages(client):
    # Privacy: only the precache list is stored; responses are never put in
    # the cache at runtime, so no care data ends up on the device.
    sw = client.get("/sw.js").text
    assert ".put(" not in sw
    assert all(u == "/offline" or u.startswith("/static/") for u in precache_urls(sw))


def test_offline_page_has_no_household_data(client, conn):
    save_settings(conn, baby_name="Juniper", caregivers="Sam, Alex")
    client.post("/note/new", data={"body": "rash on left arm", "when_ago": "0"})
    assert "Juniper" in client.get("/").text
    r = client.get("/offline")
    assert r.status_code == 200 and "Can't reach LilyPad" in r.text and "data-offline" in r.text
    for secret in ("Juniper", "Sam", "Alex", "rash", 'name="csrf"'):
        assert secret not in r.text


def test_asset_version_tracks_static_file_contents(client, tmp_path):
    (tmp_path / "a.css").write_text("body{}")
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "i.svg").write_text("<svg/>")
    before = static_fingerprint(tmp_path)
    assert static_fingerprint(tmp_path) == before
    (tmp_path / "a.css").write_text("body{color:red}")
    assert static_fingerprint(tmp_path) != before
    # Pages use it as the cache-buster for every static asset.
    page = client.get("/").text
    assert f"/static/app.css?v={static_fingerprint(STATIC_DIR)}" in page

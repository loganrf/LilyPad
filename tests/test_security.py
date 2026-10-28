"""Request-level protections and crypto helpers."""

import time

from fastapi.testclient import TestClient

from app.security import (
    LoginThrottle, SessionSigner, generate_password, hash_password, host_allowed, origin_allowed,
    verify_password,
)


def test_password_hashing():
    h = hash_password("hunter2!")
    assert h.startswith("scrypt$")
    assert verify_password("hunter2!", h)
    assert not verify_password("hunter3!", h)
    assert not verify_password("x", "garbage")
    assert len(generate_password().split("-")) == 4


def test_session_tokens():
    signer = SessionSigner("secret")
    token = signer.issue("fp1", 60)
    session = signer.verify(token, "fp1")
    assert session is not None
    assert signer.verify(token, "fp2") is None  # password changed
    assert SessionSigner("other").verify(token, "fp1") is None
    assert signer.verify(token, "fp1", now=time.time() + 120) is None  # expired
    assert signer.check_csrf(session, signer.csrf_token(session))
    assert not signer.check_csrf(session, "nope")


def test_throttle():
    t = LoginThrottle()
    for _ in range(4):
        t.failure("ip", now=1000)
    assert t.locked_for("ip", now=1000) == 0
    t.failure("ip", now=1000)
    assert t.locked_for("ip", now=1000) == 30
    t.failure("ip", now=1000)
    assert t.locked_for("ip", now=1000) == 60
    assert t.locked_for("ip", now=1000 + 4000) == 0  # forgotten after an hour


def test_origin_helpers():
    assert origin_allowed("http://192.168.1.5:8080", ["192.168.1.5:8080"], [])
    assert origin_allowed("https://lily.example.com", ["lily.example.com"], [])
    assert origin_allowed("https://lily.example.com", ["lily.example.com:443"], [])
    assert not origin_allowed("https://evil.example", ["lily.example.com"], [])
    assert not origin_allowed("http://lily.example.com:8081", ["lily.example.com:8080"], [])
    assert not origin_allowed("null", ["x"], [])
    assert origin_allowed("https://proxy.example", ["lilypad:8080"], ["https://proxy.example"])
    assert host_allowed("anything", ["*"])
    assert host_allowed("lily.home.arpa:8080", ["*.home.arpa"])
    assert not host_allowed("evil.example", ["*.home.arpa", "192.168.1.5"])


def test_cross_site_post_blocked(client, conn):
    r = client.post("/diaper/quick", data={"kind": "wet"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert conn.execute("SELECT COUNT(*) FROM diapers").fetchone()[0] == 0
    r = client.post("/diaper/quick", data={"kind": "wet"}, headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403
    # Same-origin browsers and non-browser clients (no Origin, e.g. ntfy actions) are fine.
    r = client.post("/diaper/quick", data={"kind": "wet"}, headers={"Origin": "http://lilypad.test"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/diaper/quick", data={"kind": "wet"}, follow_redirects=False)
    assert r.status_code == 303


def test_forwarded_host_accepted(client):
    r = client.post("/diaper/quick", data={"kind": "wet"}, follow_redirects=False,
                    headers={"Origin": "https://lily.example.com", "X-Forwarded-Host": "lily.example.com"})
    assert r.status_code == 303


def test_host_allowlist(make_app):
    app = make_app(allowed_hosts=["lilypad.test"])
    with TestClient(app, base_url="http://lilypad.test") as c:
        assert c.get("/").status_code == 200
    with TestClient(app, base_url="http://rebound.evil") as c:
        assert c.get("/").status_code == 421
        assert c.get("/healthz").status_code == 200


def test_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "noindex" in r.headers["x-robots-tag"]
    dash = client.get("/dash")
    assert "x-frame-options" not in dash.headers
    assert "frame-ancestors *" in dash.headers["content-security-policy"]
    assert client.get("/robots.txt").text.startswith("User-agent: *")


def test_no_inline_scripts_or_styles(client):
    """CSP forbids inline script/style, so templates must not use them."""
    import re
    for path in ("/", "/timeline", "/dash", "/feed/new", "/diaper/new", "/vitals/new", "/note/new", "/privacy"):
        html = client.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert " style=" not in html and "<style" not in html, path
        assert not re.search(r"\son[a-z]+=", html), path


def test_xss_escaped(client):
    client.post("/note/new", data={"body": "<script>alert(1)</script>", "tags": "<b>x</b>", "when_ago": "0"})
    html = client.get("/timeline").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html

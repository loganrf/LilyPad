"""Admin password, admin sessions, CSRF tokens and request-origin checks.

Only the admin area (settings, reminder/vitals configuration, full history,
reports and exports) is password protected. Day-to-day logging is
intentionally open; see docs/PRIVACY_AND_SECURITY.md for the reasoning and
what that exposes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from urllib.parse import urlsplit

# --- password hashing ---------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_password() -> str:
    """Readable random password, e.g. 'mint-otter-4821-cloud'."""
    words = (
        "apple bloom cedar cloud daisy ember fern frost grove honey ivy juniper "
        "lark maple meadow mint moss otter pebble pine plum river robin sage "
        "spruce star thistle tulip willow wren"
    ).split()
    parts = [secrets.choice(words) for _ in range(3)]
    parts.insert(2, str(secrets.randbelow(9000) + 1000))
    return "-".join(parts)


# --- admin session tokens -------------------------------------------------------

@dataclass(frozen=True)
class AdminSession:
    issued_at: int
    expires_at: int
    nonce: str

    def remaining_seconds(self, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now or time.time())))


class SessionSigner:
    """HMAC-signed, stateless admin session tokens.

    The token embeds a fingerprint of the current password hash, so changing
    the password logs out every existing session.
    """

    def __init__(self, secret: str):
        self._key = hashlib.sha256(("lilypad-session:" + secret).encode()).digest()

    def _sig(self, payload: str) -> str:
        mac = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def issue(self, password_fingerprint: str, lifetime_seconds: int, now: float | None = None) -> str:
        issued = int(now or time.time())
        nonce = secrets.token_urlsafe(9)
        payload = f"v1.{issued}.{issued + lifetime_seconds}.{nonce}.{password_fingerprint}"
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str, password_fingerprint: str, now: float | None = None) -> AdminSession | None:
        try:
            version, issued, expires, nonce, fingerprint, sig = token.split(".")
        except (ValueError, AttributeError):
            return None
        payload = f"{version}.{issued}.{expires}.{nonce}.{fingerprint}"
        if version != "v1" or not _same(sig, self._sig(payload)):
            return None
        if not _same(fingerprint, password_fingerprint):
            return None
        try:
            session = AdminSession(int(issued), int(expires), nonce)
        except ValueError:
            return None
        if session.expires_at <= (now or time.time()):
            return None
        return session

    def csrf_token(self, session: AdminSession) -> str:
        return self._sig(f"csrf.{session.issued_at}.{session.nonce}")

    def check_csrf(self, session: AdminSession, token: str | None) -> bool:
        return isinstance(token, str) and bool(token) and _same(token, self.csrf_token(session))


def _same(a: str, b: str) -> bool:
    """Constant-time string comparison that tolerates non-ASCII input."""
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


# --- login throttling -----------------------------------------------------------

class LoginThrottle:
    """Per-client exponential lockout after repeated failed logins."""

    FREE_ATTEMPTS = 5
    BASE_LOCK_SECONDS = 30
    MAX_LOCK_SECONDS = 15 * 60
    FORGET_AFTER_SECONDS = 60 * 60

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float, float]] = {}  # key -> (fails, locked_until, last)

    def locked_for(self, key: str, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            fails, locked_until, last = self._state.get(key, (0, 0.0, 0.0))
            if now - last > self.FORGET_AFTER_SECONDS:
                self._state.pop(key, None)
                return 0
            return max(0, int(locked_until - now + 0.999))

    def attempt(self, key: str, now: float | None = None) -> int:
        """Atomically check the lock and count this attempt.

        Returns seconds remaining if locked (attempt refused), else 0. Counting
        before the password is checked stops a burst of parallel requests from
        all slipping past the lockout. Call ``success`` if the password was right.
        """
        now = now or time.time()
        with self._lock:
            fails, locked_until, last = self._state.get(key, (0, 0.0, 0.0))
            if now - last > self.FORGET_AFTER_SECONDS:
                fails, locked_until = 0, 0.0
            if locked_until > now:
                return max(1, int(locked_until - now + 0.999))
            self._record_failure(key, fails, now)
            return 0

    def failure(self, key: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            fails, _, last = self._state.get(key, (0, 0.0, 0.0))
            if now - last > self.FORGET_AFTER_SECONDS:
                fails = 0
            self._record_failure(key, fails, now)

    def _record_failure(self, key: str, previous_fails: int, now: float) -> None:
        fails = previous_fails + 1
        locked_until = 0.0
        if fails >= self.FREE_ATTEMPTS:
            exponent = fails - self.FREE_ATTEMPTS
            locked_until = now + min(self.MAX_LOCK_SECONDS, self.BASE_LOCK_SECONDS * 2**exponent)
        self._state[key] = (fails, locked_until, now)

    def success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


# --- request origin / host checks -----------------------------------------------

def split_host_port(netloc: str) -> tuple[str, str | None]:
    """'Example.com:8080' -> ('example.com', '8080'); '[fd00::5]:80' -> ('fd00::5', '80')."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        host, _, rest = netloc[1:].partition("]")
        port = rest[1:] if rest.startswith(":") and rest[1:] else None
        return host, port
    if netloc.count(":") == 1:
        host, port = netloc.split(":")
        return host, port or None
    return netloc, None  # bare hostname, or an unbracketed IPv6 literal


def host_allowed(host_header: str, allowed: list[str]) -> bool:
    if "*" in allowed:
        return True
    host, _ = split_host_port(host_header)
    for pattern in allowed:
        pattern = pattern.strip().lower().strip("[]")
        if ("*" in pattern and fnmatch(host, pattern)) or host == pattern:
            return True
    return False


def origin_allowed(origin: str, request_hosts: list[str], extra_origins: list[str]) -> bool:
    """Is a browser-supplied Origin (or Referer) the app's own origin?"""
    if not origin or origin == "null":
        return False
    normalized = origin.rstrip("/").lower()
    if normalized in (o.rstrip("/").lower() for o in extra_origins):
        return True
    parts = urlsplit(normalized)
    if not parts.netloc or parts.scheme not in ("http", "https"):
        return False
    default = {"http": "80", "https": "443"}[parts.scheme]
    o_host, o_port = split_host_port(parts.netloc)
    o_port = o_port or default
    for candidate in request_hosts:
        if not candidate or not candidate.strip():
            continue
        h_host, h_port = split_host_port(candidate)
        if o_host == h_host and o_port == (h_port or default):
            return True
    return False

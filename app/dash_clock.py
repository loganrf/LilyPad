"""Direct notifications to Dash LED matrix clocks (github.com/loganrf/dash).

LilyPad POSTs ``{"text", "color", ...}`` to ``<address>/api/notify``. The same
path works on a single clock (``http://192.168.1.63``) and on a Dash server
(``https://dash.example``), which forwards the message to every clock it manages.

Dash shows text that fits on its 32-pixel display for ``duration`` seconds,
but scrolls longer text a fixed number of times (``repeat``). To keep a
message up for a chosen length of time either way, we measure the text with
the clock's own small font and work out the number of passes.
"""

from __future__ import annotations

import math
import re
from typing import Any

DISPLAY_WIDTH = 32
DEFAULT_SCROLL_SPEED = 25  # pixels per second, the firmware default
MAX_REPEAT = 255
MAX_DURATION_SECONDS = 3600
MAX_TEXT_BYTES = 256
MAX_ADDRESSES = 8
MAX_MINUTES = 30

# Column widths of the firmware's small (3x5) font for characters 0x20-0x7E,
# copied from firmware/lib/dashcore/src/font_data.h (kSmallWidths). Glyphs are
# separated by one blank column; anything outside printable ASCII shows as '?'.
_SMALL_WIDTHS = (
    1, 1, 3, 3, 3, 3, 3, 1, 2, 2, 3, 3, 2, 3, 1, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 1, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 2, 3, 3, 3, 3, 3, 3, 3,
    3, 1, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 1, 3, 3,
)

COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_COLOR = "#FF4080"
DEFAULT_TEXT = "Check {baby}"


def text_width(text: str) -> int:
    """Width in pixels of ``text`` in the clock's small font."""
    widths = [_SMALL_WIDTHS[ord(c) - 0x20] if 0x20 <= ord(c) <= 0x7E else _SMALL_WIDTHS[ord("?") - 0x20]
              for c in text]
    return sum(widths) + len(widths) - 1 if widths else 0


def fit_text(text: str) -> str:
    """Single line, printable, and within the firmware's 256-byte limit."""
    text = " ".join(text.split())
    while len(text.encode()) > MAX_TEXT_BYTES:
        text = text[:-1]
    return text


def render_text(template: str, *, baby: str, title: str) -> str:
    text = (template.strip() or DEFAULT_TEXT).replace("{baby}", baby).replace("{title}", title)
    return fit_text(text) or fit_text(title) or "LilyPad"


def timing(text: str, seconds: int) -> dict[str, int]:
    """Fields that keep ``text`` on the display for about ``seconds`` (0 = until dismissed)."""
    width = text_width(text)
    if width <= DISPLAY_WIDTH:
        return {"duration": max(0, min(MAX_DURATION_SECONDS, seconds))}
    if seconds <= 0:
        return {"repeat": 0, "scroll_speed": DEFAULT_SCROLL_SPEED}
    travel = DISPLAY_WIDTH + width  # enters from the right, leaves past the left
    speed = DEFAULT_SCROLL_SPEED
    if seconds * speed / travel > MAX_REPEAT:
        # Too many passes for the firmware's limit: scroll more slowly instead.
        speed = max(1, math.floor(MAX_REPEAT * travel / seconds))
    repeat = max(1, min(MAX_REPEAT, round(seconds * speed / travel)))
    return {"repeat": repeat, "scroll_speed": speed}


def build_payload(template: str, color: str, minutes: int, *, baby: str, title: str) -> dict[str, Any]:
    text = render_text(template, baby=baby, title=title)
    return {
        "text": text,
        "color": color if COLOR_RE.match(color or "") else DEFAULT_COLOR,
        "font": "small",
        **timing(text, max(0, minutes) * 60),
    }


def parse_addresses(raw: str) -> list[str]:
    """One clock or Dash server per line; blank lines and duplicates ignored."""
    seen: list[str] = []
    for line in (raw or "").splitlines():
        addr = line.strip().rstrip("/")
        if addr.endswith("/api/notify"):
            addr = addr[: -len("/api/notify")]
        if addr and addr not in seen:
            seen.append(addr)
    return seen


def address_errors(raw: str) -> list[str]:
    addresses = parse_addresses(raw)
    errors = [f"Dash clock address {a!r} must start with http:// or https://."
              for a in addresses if not a.lower().startswith(("http://", "https://"))]
    if len(addresses) > MAX_ADDRESSES:
        errors.append(f"At most {MAX_ADDRESSES} Dash clock addresses.")
    return errors

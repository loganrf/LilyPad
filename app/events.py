"""In-process pub/sub feeding the Server-Sent Events stream used by /dash
and the home page. Safe to publish from worker threads.

LilyPad runs as a single process (see Dockerfile), so in-memory fan-out is
enough.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, separators=(',', ':'))}\n\n"


class EventBus:
    MAX_QUEUE = 100

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, type_: str, data: dict[str, Any] | None = None) -> None:
        event = Event(type_, data or {})
        with self._lock:
            queues = list(self._subscribers)
        if not queues or self._loop is None or self._loop.is_closed():
            return

        def _deliver():
            for q in queues:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # a stalled client just misses events; it also polls

        try:
            self._loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            pass  # loop shutting down

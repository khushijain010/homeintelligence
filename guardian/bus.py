"""In-process async pub/sub. Each subscriber gets its own queue of (topic, payload).

publish() is safe to call from FastAPI's worker threads (sync endpoints): it hands the
item to the subscriber's event loop instead of touching the asyncio.Queue directly.
"""

import asyncio
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subs: list[tuple[set[str] | None, asyncio.Queue, asyncio.AbstractEventLoop]] = []

    def subscribe(self, topics: set[str] | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append((topics, q, asyncio.get_running_loop()))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs = [s for s in self._subs if s[1] is not q]

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        item = (topic, payload or {})
        for topics, q, loop in list(self._subs):
            if topics is not None and topic not in topics:
                continue
            if loop is current:
                q.put_nowait(item)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(q.put_nowait, item)


bus = Bus()

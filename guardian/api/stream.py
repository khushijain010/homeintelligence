"""GET /api/stream: Server-Sent Events. Each bus message becomes one `message` event."""

import json

from fastapi import APIRouter, Request
from sse_starlette import EventSourceResponse

from guardian.bus import bus

router = APIRouter(prefix="/api")


@router.get("/stream")
async def stream(request: Request) -> EventSourceResponse:
    q = bus.subscribe()

    async def events():
        try:
            while not await request.is_disconnected():
                topic, payload = await q.get()
                yield {"data": json.dumps({"topic": topic, **payload})}
        finally:
            bus.unsubscribe(q)

    return EventSourceResponse(events(), ping=15)

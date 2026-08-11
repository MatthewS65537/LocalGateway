"""``/admin/events`` — SSE event bus endpoint (worker-only, like /metrics).

Exposes the in-process :mod:`events` pub/sub as a Server-Sent Events stream.
The UI opens one ``EventSource('/admin/events')`` per page; typed events
(``hello``, ``tick``, ``circuit``) are fanned out to subscribed handlers.

Mounted only on the worker (where live state lives). The supervisor's
streaming proxy forwards the connection when the gateway is running.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .. import events

router = APIRouter()


@router.get("/admin/events")
async def event_stream(request: Request):
    q = events.subscribe()

    async def generate():
        try:
            yield (
                f"event: hello\ndata: {json.dumps({'ts': time.time()})}\n\n"
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    event_type = msg.get("type", "message")
                    payload = json.dumps(msg.get("data", {}))
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

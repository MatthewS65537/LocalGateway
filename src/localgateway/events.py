"""In-process pub/sub event bus for the worker.

A lightweight event distribution layer: subscribers register an asyncio.Queue,
publishers fire typed events. Used by the ``/admin/events`` SSE endpoint to
push live state (inflight, circuit trips, log availability) to the UI,
replacing per-page polling.

Thread-safety: ``publish()`` may be called from any thread (request handlers
in the event loop, or background threads). When a loop is registered via
``set_loop()``, the enqueue is scheduled with ``call_soon_threadsafe`` so it
never blocks the caller and is safe from non-loop threads.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
_state_version: int = 0


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _loop
    _loop = loop


def bump_state_version() -> None:
    """Increment the monotonically increasing state version.

    Callers (stats, circuitbreaker, ratelimit) bump this whenever they mutate
    in-memory state so subscribers can decide whether to refetch.
    """
    global _state_version
    _state_version += 1


def get_state_version() -> int:
    return _state_version


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)


def publish(event_type: str, data: dict[str, Any] | None = None) -> None:
    msg = {"type": event_type, "data": data or {}, "ts": time.time()}
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_do_publish, msg)
    else:
        _do_publish(msg)


def _do_publish(msg: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except Exception:
                pass

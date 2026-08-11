"""Phase B — L1 SSE event bus tests.

Tests the in-process pub/sub (events.py), the /admin/events SSE endpoint
format, and circuit-breaker event emission.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from localgateway import events


# ---------- pub/sub unit tests ----------

def test_pubsub_subscribe_publish_receive():
    async def run():
        events.set_loop(asyncio.get_running_loop())
        q = events.subscribe()
        assert events.subscriber_count() == 1
        events.publish("test", {"msg": "hello"})
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["type"] == "test"
        assert msg["data"]["msg"] == "hello"
        assert "ts" in msg
        events.unsubscribe(q)
        assert events.subscriber_count() == 0
        events.set_loop(None)

    asyncio.run(run())


def test_pubsub_multiple_subscribers():
    async def run():
        events.set_loop(asyncio.get_running_loop())
        q1 = events.subscribe()
        q2 = events.subscribe()
        assert events.subscriber_count() == 2
        events.publish("tick", {"version": 5})
        m1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        m2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert m1 == m2
        assert m1["data"]["version"] == 5
        events.unsubscribe(q1)
        events.unsubscribe(q2)
        events.set_loop(None)

    asyncio.run(run())


def test_pubsub_drop_oldest_on_full_queue():
    async def run():
        events.set_loop(asyncio.get_running_loop())
        q = events.subscribe()
        # Fill the queue (maxsize=256)
        for i in range(256):
            events.publish("fill", {"i": i})
        # Wait for all to be enqueued
        await asyncio.sleep(0.1)
        # Publish one more — should drop the oldest
        events.publish("overflow", {"i": 999})
        await asyncio.sleep(0.1)
        # First item should no longer be i=0; last should be i=999
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        assert first["data"]["i"] != 0  # oldest was dropped
        # Drain to the end
        last = first
        while not q.empty():
            last = await asyncio.wait_for(q.get(), timeout=1.0)
        assert last["data"]["i"] == 999
        events.unsubscribe(q)
        events.set_loop(None)

    asyncio.run(run())


def test_state_version_monotonic():
    v0 = events.get_state_version()
    events.bump_state_version()
    events.bump_state_version()
    assert events.get_state_version() == v0 + 2


def test_publish_without_loop_does_not_crash():
    events.set_loop(None)
    q = events.subscribe()
    events.publish("noloop", {"ok": True})
    assert events.subscriber_count() == 1
    events.unsubscribe(q)


# ---------- SSE endpoint test ----------

def test_sse_endpoint_hello_and_format():
    """The /admin/events endpoint returns text/event-stream and emits hello.

    Calls the handler directly and iterates the body generator to verify the
    first chunk. Full end-to-end streaming is verified via curl in live browser
    verification.
    """
    from localgateway.endpoints.events import event_stream
    from starlette.requests import Request

    async def run():
        scope = {
            "type": "http", "method": "GET", "path": "/admin/events",
            "headers": [], "query_string": b"", "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 3456), "scheme": "http",
        }
        request = Request(scope)
        response = await event_stream(request)
        assert response.media_type == "text/event-stream"
        # Iterate the body iterator to get the hello event
        first = await response.body_iterator.__anext__()
        text = first.decode() if isinstance(first, bytes) else first
        assert "event: hello" in text
        assert "data:" in text

    asyncio.run(run())


# ---------- circuit event emission ----------

def test_circuit_trip_publishes_event():
    from localgateway import circuitbreaker

    async def run():
        events.set_loop(asyncio.get_running_loop())
        q = events.subscribe()
        circuitbreaker.reset_state()
        circuitbreaker.configure(enabled=True, threshold=2, backoff_s=1.0)

        # Two failures to trip
        circuitbreaker.record_outcome("p1", "m1", False, "err1")
        tripped = circuitbreaker.record_outcome("p1", "m1", False, "err2")
        assert tripped is True

        # Should have published a circuit event
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["type"] == "circuit"
        assert msg["data"]["backend"] == "p1:m1"
        assert msg["data"]["action"] == "trip"

        events.unsubscribe(q)
        events.set_loop(None)
        circuitbreaker.reset_state()

    asyncio.run(run())


def test_circuit_reset_publishes_event():
    from localgateway import circuitbreaker

    async def run():
        events.set_loop(asyncio.get_running_loop())
        q = events.subscribe()
        circuitbreaker.reset_state()
        circuitbreaker.configure(enabled=True, threshold=1, backoff_s=1.0)
        circuitbreaker.record_outcome("p1", "m1", False, "err")

        # Drain the trip event first
        trip_msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert trip_msg["data"]["action"] == "trip"

        circuitbreaker.reset("p1", "m1")
        msg = await asyncio.wait_for(q.get(), timeout=1.0)
        assert msg["type"] == "circuit"
        assert msg["data"]["action"] == "reset"

        events.unsubscribe(q)
        events.set_loop(None)
        circuitbreaker.reset_state()

    asyncio.run(run())

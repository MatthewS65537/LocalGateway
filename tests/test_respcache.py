"""F5 stage 1: exact response cache.

Verifies hit/miss/store/eviction for non-stream and stream paths, the
cost=0/cache_hit=1 accounting on hits, non-cacheable request exclusion,
and TTL expiry.
"""
from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest

from conftest import build_config

from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import respcache
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr
from localgateway.usage import set_db_path
from localgateway.worker import create_app


@pytest.fixture()
async def c_client(mock_servers, tmp_path):
    """Config with response cache enabled on mock-reasoner."""
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    cfg["server"]["response_cache_enabled"] = True
    cfg["server"]["response_cache_ttl_sec"] = 3600
    cfg["server"]["response_cache_max_entries"] = 100
    cfg["server"]["tokenizer"] = "heuristic"
    for m in cfg["models"]:
        if m["id"] == "mock-reasoner":
            m["cache_responses"] = True
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    db_path = tmp_path / "usage.db"
    config_mod._config = None
    config_mod._config_mtime = -1.0
    usage_mod._initialized = False
    logs_mod._initialized = False
    respcache._initialized = False
    stats_mod.reset()
    ratelimit._cooldowns.clear()
    ratelimit._permanent.clear()
    _rr.clear()
    app = create_app(str(cfg_path))
    set_db_path(str(db_path))
    logs_mod.set_db_path(str(db_path))
    respcache.set_db_path(str(db_path))
    usage_mod.stop_writer()
    logs_mod.stop_writer()
    respcache.stop_writer()  # sync writes for deterministic test reads
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c, db_path


def _usage_rows(db_path, model="mock-reasoner"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM usage WHERE logical_model = ? ORDER BY id", (model,)
    ).fetchall()]
    conn.close()
    return rows


async def test_nonstream_cache_miss_then_hit(c_client):
    c, db = c_client
    body = {"model": "mock-reasoner", "messages": [{"role": "user", "content": "hello"}]}
    # First request: miss (goes to provider, stores response).
    r1 = await c.post("/v1/chat/completions", json=body)
    assert r1.status_code == 200
    assert "x-cache-hit" not in r1.headers
    assert "hello from" in r1.text

    # Second identical request: hit (X-Cache-Hit: true, same body, no provider call).
    r2 = await c.post("/v1/chat/completions", json=body)
    assert r2.status_code == 200
    assert r2.headers.get("x-cache-hit") == "true"
    assert r1.json()["choices"][0]["message"]["content"] == r2.json()["choices"][0]["message"]["content"]

    # Usage rows: first has cost>0 (provider), second has cost=0 + cache_hit=1.
    rows = _usage_rows(db)
    assert len(rows) == 2
    assert rows[0]["cache_hit"] == 0
    assert rows[1]["cache_hit"] == 1
    assert rows[1]["cost"] == 0.0
    assert rows[0]["cost"] is not None and rows[0]["cost"] > 0


async def test_nonstream_different_prompt_is_miss(c_client):
    c, _ = c_client
    await c.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "A"}],
    })
    r = await c.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "B"}],
    })
    assert "x-cache-hit" not in r.headers


async def test_stream_cache_miss_then_hit(c_client):
    c, db = c_client
    body = {"model": "mock-reasoner", "stream": True,
            "messages": [{"role": "user", "content": "stream me"}]}

    # First: miss — collect the full stream.
    chunks1: list[bytes] = []
    async with c.stream("POST", "/v1/chat/completions", json=body) as r1:
        assert r1.status_code == 200
        assert "x-cache-hit" not in r1.headers
        async for chunk in r1.aiter_bytes():
            chunks1.append(chunk)

    # Second: hit — same bytes, X-Cache-Hit header.
    chunks2: list[bytes] = []
    async with c.stream("POST", "/v1/chat/completions", json=body) as r2:
        assert r2.status_code == 200
        assert r2.headers.get("x-cache-hit") == "true"
        async for chunk in r2.aiter_bytes():
            chunks2.append(chunk)

    assert b"".join(chunks1) == b"".join(chunks2), "cached stream must replay identical bytes"

    # Stream hit logged with cost=0, cache_hit=1.
    rows = _usage_rows(db)
    assert len(rows) == 2
    assert rows[1]["cache_hit"] == 1
    assert rows[1]["cost"] == 0.0
    assert rows[1]["stream"] == 1


async def test_noncacheable_tools_not_cached(c_client):
    """Requests with tools are never cached (non-deterministic)."""
    c, _ = c_client
    body = {"model": "mock-reasoner",
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}
    r1 = await c.post("/v1/chat/completions", json=body)
    assert r1.status_code == 200
    r2 = await c.post("/v1/chat/completions", json=body)
    assert "x-cache-hit" not in r2.headers, "tools request must not be cached"


async def test_high_temperature_not_cached(c_client):
    """temperature > 0.1 is non-deterministic — not cached."""
    c, _ = c_client
    body = {"model": "mock-reasoner", "temperature": 0.9,
            "messages": [{"role": "user", "content": "creative"}]}
    await c.post("/v1/chat/completions", json=body)
    r = await c.post("/v1/chat/completions", json=body)
    assert "x-cache-hit" not in r.headers


async def test_cache_ttl_expiry(c_client, monkeypatch):
    """An expired entry (TTL elapsed) is a miss, not a hit."""
    c, _ = c_client
    body = {"model": "mock-reasoner", "messages": [{"role": "user", "content": "ttl test"}]}
    await c.post("/v1/chat/completions", json=body)
    # Artificially age the stored entry past TTL (both the SQLite row AND the
    # in-memory read-through entry, which mirrors it).
    respcache._mem.clear()
    conn = sqlite3.connect(str(respcache._db_path))
    conn.execute("UPDATE resp_cache SET created = ? ", (time.time() - 7200,))
    conn.commit()
    conn.close()
    r = await c.post("/v1/chat/completions", json=body)
    assert "x-cache-hit" not in r.headers, "expired entry must not hit"


async def test_cache_eviction_by_lru(c_client):
    """When max_entries is exceeded, the oldest entry is evicted."""
    c, db = c_client
    # Lower the max_entries to 2 for this test via direct config manipulation.
    config_mod._config.server.response_cache_max_entries = 2
    # Fill 3 distinct entries.
    for i in range(3):
        await c.post("/v1/chat/completions", json={
            "model": "mock-reasoner", "messages": [{"role": "user", "content": f"evict-{i}"}],
        })
    # The cache should have at most 2 entries.
    st = respcache.stats()
    assert st["entries"] <= 2, f"expected ≤2 entries after LRU eviction, got {st['entries']}"

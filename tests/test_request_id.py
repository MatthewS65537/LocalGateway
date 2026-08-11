"""B4 regression: request correlation IDs (X-Request-Id).

Verifies the load-bearing contract for F2 (tracing) and F5 (cache accounting):
- Every response carries X-Request-Id (minted when absent, honored when valid).
- The same ID is persisted in the usage row and the log row meta.
- All fallback attempts in one request share the ID.
- Works across chat (stream + non-stream), responses, and embeddings.
"""
from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from conftest import build_config

from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import usage as usage_mod
from localgateway.usage import set_db_path
from localgateway.worker import create_app


@pytest.fixture()
def rid_app(mock_servers, tmp_path):
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    # Embeddings + responses models.
    cfg["models"].append({
        "id": "mock-embed", "endpoint": "embeddings",
        "backends": [{"provider": "mock1", "model": "mock-emb", "priority": 1}],
    })
    cfg["models"].append({
        "id": "mock-resp", "endpoint": "responses",
        "backends": [{"provider": "mock1", "model": "mock-reasoner", "priority": 1}],
    })
    cfg["server"]["tokenizer"] = "heuristic"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    db_path = tmp_path / "usage.db"
    config_mod._config = None
    config_mod._config_mtime = -1.0
    usage_mod._initialized = False
    logs_mod._initialized = False
    from localgateway import stats as stats_mod
    from localgateway.ratelimit import ratelimit
    from localgateway.router import _rr
    stats_mod.reset()
    ratelimit._cooldowns.clear()
    ratelimit._permanent.clear()
    _rr.clear()
    app = create_app(str(cfg_path))
    set_db_path(str(db_path))
    logs_mod.set_db_path(str(db_path))
    usage_mod.stop_writer()
    logs_mod.stop_writer()
    return app, db_path


@pytest.fixture()
async def rid_client(rid_app):
    app, _ = rid_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


def _rows(db_path, request_id):
    """Usage + log rows for a given request_id."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    usage = [dict(r) for r in conn.execute(
        "SELECT * FROM usage WHERE request_id = ? ORDER BY id", (request_id,)
    ).fetchall()]
    log_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM logs WHERE meta LIKE ? ORDER BY id", (f'%"{request_id}"%',)
    ).fetchall()]
    conn.close()
    return usage, log_rows


async def test_chat_nonstream_mints_and_persists_id(rid_client, rid_app):
    _, db = rid_app
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) == 12, f"expected 12-hex minted id, got {rid!r}"
    usage, logs = _rows(db, rid)
    assert len(usage) == 1
    assert usage[0]["request_id"] == rid
    assert logs, "log row with request_id in meta must exist"


async def test_chat_honors_client_supplied_id(rid_client, rid_app):
    _, db = rid_app
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-request-id": "my-trace-123"})
    assert r.headers.get("x-request-id") == "my-trace-123"
    usage, _ = _rows(db, "my-trace-123")
    assert len(usage) == 1


async def test_chat_rejects_malformed_client_id(rid_client):
    """A client ID with illegal chars is ignored; a fresh one is minted."""
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-request-id": "bad id with spaces!"})
    rid = r.headers.get("x-request-id")
    assert rid and rid != "bad id with spaces!" and len(rid) == 12


async def test_chat_stream_propagates_id(rid_client, rid_app):
    _, db = rid_app
    async with rid_client.stream("POST", "/v1/chat/completions", json={
        "model": "mock-reasoner", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-request-id": "stream-trace"}) as r:
        assert r.status_code == 200
        rid = r.headers.get("x-request-id")
        async for _ in r.aiter_bytes():
            pass
    assert rid == "stream-trace"
    usage, _ = _rows(db, "stream-trace")
    assert len(usage) == 1 and usage[0]["stream"] == 1


async def test_failover_shares_id_across_attempts(rid_client, rid_app):
    """A 2-attempt fallback (mock-failover: tier1=dead, tier2=ok) must use one
    request_id for both the failed and the successful usage row."""
    _, db = rid_app
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "mock-failover", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    usage, _ = _rows(db, rid)
    # One failed attempt (tier1 dead) + one success (tier2) sharing the ID.
    assert len(usage) == 2
    assert usage[0]["success"] == 0
    assert usage[1]["success"] == 1
    assert all(u["request_id"] == rid for u in usage)


async def test_responses_propagates_id(rid_client, rid_app):
    _, db = rid_app
    r = await rid_client.post("/v1/responses", json={
        "model": "mock-resp", "input": "hi",
    }, headers={"x-request-id": "resp-trace"})
    assert r.status_code == 200
    assert r.headers.get("x-request-id") == "resp-trace"
    usage, _ = _rows(db, "resp-trace")
    assert len(usage) == 1


async def test_embeddings_propagates_id(rid_client, rid_app):
    _, db = rid_app
    r = await rid_client.post("/v1/embeddings", json={
        "model": "mock-embed", "input": "hello world",
    }, headers={"x-request-id": "emb-trace"})
    assert r.status_code == 200
    assert r.headers.get("x-request-id") == "emb-trace"
    usage, _ = _rows(db, "emb-trace")
    assert len(usage) == 1


async def test_error_response_carries_id(rid_client):
    """Even an error response (404 model-not-found) echoes the request ID."""
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-request-id": "err-trace"})
    assert r.status_code == 404
    assert r.headers.get("x-request-id") == "err-trace"


async def test_trace_reconstructs_attempt_timeline(rid_client, rid_app):
    """F2: GET /admin/logs?request_id= returns all log rows for one request,
    letting the attempt timeline be reconstructed (failed → success fallback)."""
    _, db = rid_app
    r = await rid_client.post("/v1/chat/completions", json={
        "model": "mock-failover", "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-request-id": "trace-xyz"})
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid == "trace-xyz"

    # Query the logs endpoint filtered by request_id.
    lr = await rid_client.get(f"/admin/logs?request_id={rid}")
    assert lr.status_code == 200
    log_rows = lr.json()["logs"]
    assert len(log_rows) >= 2, f"expected ≥2 log rows (fail+success), got {len(log_rows)}"
    # Every row's meta must carry the same request_id.
    for row in log_rows:
        meta = row.get("meta") or {}
        if isinstance(meta, str):
            import json as _j
            meta = _j.loads(meta)
        assert meta.get("request_id") == rid

"""API surface expansion: embeddings, /v1/models/{id}, token counting,
usage export, config backups, stats-driven routing sorts."""
from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest

from conftest import build_config

from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.config import GatewayConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, ProviderPrefs, select_backends
from localgateway.usage import set_db_path
from localgateway.worker import create_app


@pytest.fixture()
def x_app(mock_servers, tmp_path):
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    cfg["models"].append({
        "id": "mock-embed",
        "endpoint": "embeddings",
        "backends": [
            {"provider": "mock1", "model": "mock-emb", "priority": 1},
            {"provider": "mock2", "model": "mock-emb", "priority": 2},
        ],
    })
    cfg["models"].append({
        "id": "mock-embed-failover",
        "endpoint": "embeddings",
        "backends": [
            {"provider": "mock1", "model": "mock-emb-dead", "priority": 1},
            {"provider": "mock2", "model": "mock-emb", "priority": 2},
        ],
    })
    cfg["pricing"]["mock1:mock-emb"] = {"input": 0.00001, "output": 0.0}
    # Responses API model
    cfg["models"].append({
        "id": "mock-resp",
        "endpoint": "responses",
        "backends": [
            {"provider": "mock1", "model": "mock-reasoner", "priority": 1},
        ],
    })
    cfg["pricing"]["mock1:mock-reasoner"] = {"input": 0.00001, "output": 0.00002}
    # Deterministic token estimates regardless of whether tiktoken is installed.
    cfg["server"]["tokenizer"] = "heuristic"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    db_path = tmp_path / "usage.db"

    config_mod._config = None
    config_mod._config_mtime = -1.0
    usage_mod._initialized = False
    logs_mod._initialized = False
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
async def xclient(x_app):
    app, _ = x_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


# ---- embeddings ----

@pytest.mark.asyncio
async def test_embeddings_happy_path(xclient, x_app):
    _, db_path = x_app
    r = await xclient.post("/v1/embeddings", json={"model": "mock-embed", "input": "hello world"})
    assert r.status_code == 200
    data = r.json()
    assert data["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert r.headers["X-Provider"] == "mock1"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage").fetchone()
    conn.close()
    assert row["logical_model"] == "mock-embed"
    assert row["input_tokens"] > 0
    assert row["cost"] > 0  # pricing mock1:mock-emb input rate applied


@pytest.mark.asyncio
async def test_embeddings_list_input(xclient):
    r = await xclient.post("/v1/embeddings", json={"model": "mock-embed", "input": ["one", "two"]})
    assert r.status_code == 200
    assert len(r.json()["data"]) == 2


@pytest.mark.asyncio
async def test_embeddings_fallback(xclient):
    r = await xclient.post("/v1/embeddings", json={"model": "mock-embed-failover", "input": "hi"})
    assert r.status_code == 200
    assert r.headers["X-Provider"] == "mock2"


@pytest.mark.asyncio
async def test_embeddings_wrong_endpoint_rejected(xclient):
    r = await xclient.post("/v1/embeddings", json={"model": "mock-reasoner", "input": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "wrong_endpoint"


@pytest.mark.asyncio
async def test_embeddings_unknown_model(xclient):
    r = await xclient.post("/v1/embeddings", json={"model": "nope", "input": "hi"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_embeddings_api_v1_alias(xclient):
    r = await xclient.post("/api/v1/embeddings", json={"model": "mock-embed", "input": "hi"})
    assert r.status_code == 200


# ---- /v1/responses ----

@pytest.mark.asyncio
async def test_responses_non_stream(xclient, x_app):
    """F4: POST /v1/responses routes through the tier/failover engine."""
    _, db_path = x_app
    r = await xclient.post("/v1/responses", json={"model": "mock-resp", "input": "Hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "response"
    assert data["usage"]["input_tokens"] == 12
    assert data["usage"]["output_tokens"] == 100
    assert r.headers["X-Provider"] == "mock1"
    assert "X-Routing-Reason" in r.headers
    # Usage logged with cost
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage WHERE logical_model = 'mock-resp'").fetchone()
    conn.close()
    assert row is not None
    assert row["success"] == 1
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 100


@pytest.mark.asyncio
async def test_responses_wrong_endpoint_rejected(xclient):
    r = await xclient.post("/v1/responses", json={"model": "mock-reasoner", "input": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "wrong_endpoint"


@pytest.mark.asyncio
async def test_responses_api_v1_alias(xclient):
    r = await xclient.post("/api/v1/responses", json={"model": "mock-resp", "input": "hi"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_responses_stream(xclient, x_app):
    """F4: streaming responses pass through SSE and extract usage from response.completed."""
    r = await xclient.post("/v1/responses", json={"model": "mock-resp", "input": "Hi", "stream": True})
    assert r.status_code == 200
    # Collect SSE events inline (sse_collect helper is hardcoded to /v1/chat/completions)
    events = []
    buf = ""
    async for text in r.aiter_text():
        buf += text
        while "\n\n" in buf:
            raw, buf = buf.split("\n\n", 1)
            for line in raw.split("\n"):
                if line.startswith("data: "):
                    data = line[6:]
                    try:
                        events.append(json.loads(data))
                    except Exception:
                        events.append(data)
    types = [e.get("type") for e in events if isinstance(e, dict)]
    assert "response.created" in types
    assert "response.output_text.delta" in types
    assert "response.completed" in types
    # Usage logged from the completed event
    _, db_path = x_app
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage WHERE logical_model = 'mock-resp' AND stream = 1").fetchone()
    conn.close()
    assert row is not None
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 100


# ---- /v1/models/{id} ----

@pytest.mark.asyncio
async def test_retrieve_model(xclient):
    r = await xclient.get("/v1/models/mock-reasoner")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "mock-reasoner"
    assert data["object"] == "model"
    assert data["endpoint"] == "chat"


@pytest.mark.asyncio
async def test_retrieve_model_by_alias(xclient):
    r = await xclient.get("/v1/models/reasoner-alias")
    assert r.status_code == 200
    assert r.json()["id"] == "mock-reasoner"


@pytest.mark.asyncio
async def test_retrieve_embeddings_model(xclient):
    r = await xclient.get("/v1/models/mock-embed")
    assert r.status_code == 200
    assert r.json()["endpoint"] == "embeddings"


@pytest.mark.asyncio
async def test_retrieve_model_not_found(xclient):
    r = await xclient.get("/v1/models/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


# ---- token counting ----

@pytest.mark.asyncio
async def test_count_tokens_messages(xclient):
    r = await xclient.post("/admin/tokens", json={
        "model": "mock-reasoner",
        "messages": [{"role": "user", "content": "hello world, this is a test"}],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["messages_tokens"] > 0
    assert "tokenizer" in data
    fits = {b["backend"]: b["fits"] for b in data["backends"]}
    assert fits["mock1:mock-reasoner"] is True   # 32k ctx
    assert fits["mock2:mock-reasoner"] is True   # 16k ctx


@pytest.mark.asyncio
async def test_count_tokens_context_fit(xclient):
    r = await xclient.post("/admin/tokens", json={
        "model": "mock-reasoner",
        "messages": [{"role": "user", "content": "x" * 100000}],
    })
    data = r.json()
    fits = {b["backend"]: b["fits"] for b in data["backends"]}
    assert fits["mock1:mock-reasoner"] is True   # 32768 >= ~25k
    assert fits["mock2:mock-reasoner"] is False  # 16384 < ~25k


@pytest.mark.asyncio
async def test_count_tokens_embedding_input(xclient):
    r = await xclient.post("/admin/tokens", json={"input": ["hello", "world"]})
    assert r.status_code == 200
    assert r.json()["input_tokens"] > 0


@pytest.mark.asyncio
async def test_count_tokens_requires_content(xclient):
    r = await xclient.post("/admin/tokens", json={})
    assert r.status_code == 422


# ---- usage export ----

@pytest.mark.asyncio
async def test_usage_export_json(xclient):
    await xclient.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    r = await xclient.get("/admin/usage/export?hours=1&format=json")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    assert data["rows"][0]["logical_model"] == "mock-reasoner"


@pytest.mark.asyncio
async def test_usage_export_csv(xclient):
    await xclient.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    r = await xclient.get("/admin/usage/export?hours=1&format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("ts,logical_model,provider")
    assert "mock-reasoner" in lines[1]


# ---- config backups ----

@pytest.mark.asyncio
async def test_backups_list_and_restore(xclient):
    # Two saves → one backup generation exists.
    cfg = config_mod.load_config()
    original_desc = cfg.models[0].description
    config_mod.save_config(cfg)
    cfg2 = config_mod.load_config()
    cfg2.models[0].description = "changed-desc"
    config_mod.save_config(cfg2)

    r = await xclient.get("/admin/backups")
    assert r.status_code == 200
    backups = r.json()["backups"]
    assert any(b["current"] for b in backups)
    slots = [b["slot"] for b in backups if not b["current"]]
    assert 0 in slots

    r2 = await xclient.post("/admin/backups/restore", json={"slot": 0})
    assert r2.status_code == 200
    cfg3 = config_mod.load_config()
    assert cfg3.models[0].description == original_desc


@pytest.mark.asyncio
async def test_backups_restore_missing_slot(xclient):
    r = await xclient.post("/admin/backups/restore", json={"slot": 99})
    assert r.status_code == 404


# ---- stats-driven routing ----

def _insert_tps_rows(db_path, model: str, rows: list[tuple[str, str, float]]):
    usage_mod._init()  # ensure schema exists before raw inserts
    conn = sqlite3.connect(str(db_path))
    for provider, backend, tps in rows:
        conn.execute(
            """INSERT INTO usage (ts, logical_model, provider, backend_model, success,
               input_tokens, output_tokens, cost, latency_ms, tps, stream, is_probe)
               VALUES (?, ?, ?, ?, 1, 10, 100, 0, 1000, ?, 0, 0)""",
            (time.time(), model, provider, backend, tps),
        )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_throughput_sort_uses_measured_tps(xclient, x_app):
    """sort=throughput within a tier ranks by usage-DB TPS p50, not proxy."""
    _, db_path = x_app
    # Both mock1/mock2 serve mock-reasoner; give mock2 much higher TPS.
    _insert_tps_rows(db_path, "mock-reasoner", [
        ("mock1", "mock-reasoner", 20.0),
        ("mock2", "mock-reasoner", 150.0),
    ])
    usage_mod.invalidate_tps_map()
    # Same-tier model for a fair sort comparison.
    cfg = config_mod.load_config()
    cfg.models.append(config_mod.ModelConfig.model_validate({
        "id": "mock-pair",
        "backends": [
            {"provider": "mock1", "model": "mock-reasoner", "priority": 1},
            {"provider": "mock2", "model": "mock-reasoner", "priority": 1},
        ],
    }))
    config_mod.save_config(cfg)
    _insert_tps_rows(db_path, "mock-pair", [
        ("mock1", "mock-reasoner", 20.0),
        ("mock2", "mock-reasoner", 150.0),
    ])
    usage_mod.invalidate_tps_map()

    r = await xclient.post("/v1/chat/completions", json={
        "model": "mock-pair",
        "provider": {"sort": "throughput"},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert r.headers["X-Provider"] == "mock2"  # higher measured TPS wins


@pytest.mark.asyncio
async def test_value_sort(xclient, x_app):
    """sort=value ranks by TPS per dollar."""
    _, db_path = x_app
    cfg = config_mod.load_config()
    cfg.models.append(config_mod.ModelConfig.model_validate({
        "id": "mock-value",
        "backends": [
            {"provider": "mock1", "model": "mock-reasoner", "priority": 1},
            {"provider": "mock2", "model": "mock-reasoner", "priority": 1},
        ],
    }))
    # mock2 is 10x more expensive; mock1 is only 2x slower → mock1 better value.
    cfg.pricing["mock1:mock-reasoner"] = config_mod.PricingEntry(input=1e-6, output=2e-6)
    cfg.pricing["mock2:mock-reasoner"] = config_mod.PricingEntry(input=1e-5, output=2e-5)
    config_mod.save_config(cfg)
    _insert_tps_rows(db_path, "mock-value", [
        ("mock1", "mock-reasoner", 50.0),
        ("mock2", "mock-reasoner", 100.0),
    ])
    usage_mod.invalidate_tps_map()

    r = await xclient.post("/v1/chat/completions", json={
        "model": "mock-value",
        "provider": {"sort": "value"},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert r.headers["X-Provider"] == "mock1"


def test_tps_map_helper(x_app):
    _, db_path = x_app
    _insert_tps_rows(db_path, "mock-reasoner", [
        ("mock1", "mock-reasoner", 30.0),
        ("mock1", "mock-reasoner", 50.0),
        ("mock2", "mock-reasoner", 90.0),
    ])
    usage_mod.invalidate_tps_map()
    m = usage_mod.get_backend_tps_map("mock-reasoner")
    assert m["mock1:mock-reasoner"] == 40.0  # p50 of [30, 50]
    assert m["mock2:mock-reasoner"] == 90.0

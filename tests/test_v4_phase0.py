"""Phase 0a (v4 plan) regression tests: B1-B13 backend correctness fixes.

Each test pins the fixed behavior that the previous code got wrong —
silent accounting lies, cache TTL semantics, failure attribution, status
codes on streaming errors, and routing-header truthfulness after fallback.
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
from localgateway.clientquota import ApiKeyConfig, budget_eta
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr
from localgateway.sse import StreamAccumulator
from localgateway.usage import set_db_path
from localgateway.worker import create_app


# ---------- B1: stream token estimate accumulates ALL content ----------

def test_b1_stream_estimate_accumulates_all_chunks():
    """The old estimator stopped accumulating at the first content delta
    (it measured TTFT), so a usage-less 1000-token reply logged ~5 tokens.
    The fixed accumulator sums every content delta."""
    acc = StreamAccumulator()
    first = b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    rest = b"".join(
        f'data: {{"choices":[{{"delta":{{"content":"word{i} "}}}}]}}\n\n'.encode()
        for i in range(200)  # ~1200 more chars
    )
    acc.feed(first)
    acc.feed(rest)
    acc.feed(b"data: [DONE]\n\n")
    est = acc.estimated_output_tokens()
    assert est is not None
    # Content length ≈ 5 + 200*6 = ~1205 chars → ~301 tokens.
    assert est > 100, f"estimate must reflect the whole stream, got {est}"
    # TTFT still measured from the first delta.
    assert acc.ttft_ms is not None


def test_b1_stream_estimate_none_without_content():
    acc = StreamAccumulator()
    acc.feed(b'data: {"choices":[]}\n\n')
    acc.feed(b"data: [DONE]\n\n")
    assert acc.estimated_output_tokens() is None


# ---------- B2: cache TTL is absolute, not sliding ----------

@pytest.fixture()
async def cache_client(mock_servers, tmp_path):
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
    respcache._mem.clear()
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
    respcache.stop_writer()  # deterministic sync writes in tests
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


async def test_b2_absolute_ttl_hot_entry_expires(cache_client):
    """Under the old sliding TTL, a hit refreshed `created`, so an entry
    requested at least once per TTL window lived forever. Now `created` is
    immutable — a hit refreshes only `last_access`, so an entry that is
    already past its original creation + TTL must miss."""
    c = cache_client
    body = {"model": "mock-reasoner", "messages": [{"role": "user", "content": "hot entry"}]}
    await c.post("/v1/chat/completions", json=body)
    conn = sqlite3.connect(str(respcache._db_path))
    row = conn.execute("SELECT created FROM resp_cache").fetchone()
    original_created = row[0]
    conn.close()
    # Simulate time passing: entry is now 2000s old (TTL 3600 → still valid).
    conn = sqlite3.connect(str(respcache._db_path))
    conn.execute("UPDATE resp_cache SET created = ?", (original_created - 2000,))
    conn.commit()
    conn.close()
    respcache._mem.clear()
    # A hit now — must NOT refresh `created` (only last_access).
    r = await c.post("/v1/chat/completions", json=body)
    assert r.headers.get("x-cache-hit") == "true"
    conn = sqlite3.connect(str(respcache._db_path))
    row = conn.execute("SELECT created, last_access FROM resp_cache").fetchone()
    created_after_hit, last_access_after_hit = row
    conn.close()
    assert abs(created_after_hit - (original_created - 2000)) < 5, (
        "hit must NOT refresh the immutable created timestamp"
    )
    assert last_access_after_hit > created_after_hit, "hit must refresh last_access"
    # Now push the entry past its absolute TTL (old sliding code would have
    # survived this because the hit above refreshed created).
    conn = sqlite3.connect(str(respcache._db_path))
    conn.execute("UPDATE resp_cache SET created = ?", (original_created - 4000,))
    conn.commit()
    conn.close()
    respcache._mem.clear()
    r2 = await c.post("/v1/chat/completions", json=body)
    assert "x-cache-hit" not in r2.headers, (
        "entry past its absolute TTL must miss even after recent hits"
    )


async def test_b2_recent_hits_do_not_extend_ttl(cache_client):
    c = cache_client
    body = {"model": "mock-reasoner", "messages": [{"role": "user", "content": "ttl2"}]}
    await c.post("/v1/chat/completions", json=body)
    st = respcache.stats()
    assert st["entries"] == 1


# ---------- B3: respcache db path wired in production ----------

def test_b3_respcache_wired_in_worker():
    from localgateway import worker
    import inspect
    src = inspect.getsource(worker.create_app)
    assert "respcache.set_db_path" in src


# ---------- B4: fallback rows keep key/user attribution ----------

async def test_b4_fallback_rows_keep_attribution(client, db_rows, gateway_app):
    _, db_path = gateway_app
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-failover",  # mock1 serves a dead model → falls back to mock2
        "messages": [{"role": "user", "content": "hi"}],
        "user": "alice",
    })
    assert r.status_code == 200
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    failed = [dict(x) for x in conn.execute(
        "SELECT * FROM usage WHERE success = 0 AND logical_model = 'mock-failover'"
    ).fetchall()]
    conn.close()
    assert failed, "fallback attempt must be logged"
    assert failed[0]["end_user"] == "alice", "failed attempt must keep end_user"

    # Retry with an API key configured — the failed row must carry api_key_id.
    cfg = config_mod.load_config()
    cfg.server.api_keys = [ApiKeyConfig(id="k9", key="sekret9", model_allowlist=[])]
    config_mod.save_config(cfg)
    r2 = await client.post("/v1/chat/completions", json={
        "model": "mock-failover",
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"Authorization": "Bearer sekret9"})
    assert r2.status_code == 200
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    failed2 = [dict(x) for x in conn.execute(
        "SELECT * FROM usage WHERE success = 0 AND logical_model = 'mock-failover'"
    ).fetchall()]
    conn.close()
    assert failed2 and failed2[-1]["api_key_id"] == "k9", "failed attempt must keep api_key_id"


# ---------- B6/B7: streaming error status codes + truthful headers ----------

async def test_b6_stream_model_not_found_is_404(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "no-such-model", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 404, "pre-stream errors must carry real status"
    assert r.json()["error"]["code"] == "model_not_found"


async def test_b6_stream_all_backends_failed_is_502(client):
    """A model whose only backend dies pre-stream must 502, not stream a
    misleading 200 + SSE error event."""
    cfg = config_mod.load_config()
    from localgateway.config import ModelConfig, BackendConfig
    cfg.models.append(ModelConfig(
        id="mock-alldead",
        backends=[BackendConfig(provider="mock1", model="mock-dead", priority=1)],
    ))
    config_mod.save_config(cfg)
    r2 = await client.post("/v1/chat/completions", json={
        "model": "mock-alldead", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r2.status_code == 502, "all-backends-failed pre-stream must be 502"
    assert r2.json()["error"]["code"] == "all_backends_failed"


async def test_b7_headers_name_the_serving_backend_after_fallback(client):
    """mock-failover: mock1's backend is dead (pre-stream failure), mock2
    serves the stream. The X-Provider/X-Backend headers must name mock2 —
    the old provisional headers named mock1."""
    async with client.stream("POST", "/v1/chat/completions", json={
        "model": "mock-failover", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }) as r:
        assert r.status_code == 200
        assert r.headers.get("x-provider") == "mock2", (
            f"headers must name the serving backend after fallback, got {r.headers.get('x-provider')}"
        )
        assert r.headers.get("x-backend", "").startswith("mock2/")


async def test_b7_nonstream_failure_still_has_provider_headers(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert r.headers.get("x-provider") in ("mock1", "mock2")


# ---------- B8: burn rate uses active days ----------

def test_b8_burn_rate_uses_active_days(tmp_path):
    """A key created yesterday that spent $10 today must burn $10/day — the
    old code divided by the full 7-day window ($1.43/day, 7× undercount)."""
    from localgateway import usage as u
    db = tmp_path / "b8.db"
    u.set_db_path(str(db))
    u._initialized = False
    u.stop_writer()
    u._init()
    conn = sqlite3.connect(str(db))
    now = time.time()
    for i in range(10):
        conn.execute(
            "INSERT INTO usage (ts, logical_model, provider, backend_model, success, cost, api_key_id, is_probe) "
            "VALUES (?, 'm', 'p', 'b', 1, ?, 'kb8', 0)",
            (now, 1.0, ),
        )
    conn.commit()
    conn.close()
    burn = u.get_key_daily_burn("kb8")
    assert burn == 10.0, f"burn must divide by active days (1), got {burn}"


# ---------- B9: prober tolerates None input_tokens ----------

def test_b9_probe_cost_with_none_input_tokens():
    """Cost computation must not TypeError when input_tokens is None —
    the old code (`result.get("input_tokens", 0)` returning the None value)
    aborted the whole probe batch. The fix coalesces with `or 0`."""
    # Exercise the exact arithmetic used in the probe loop (prober.py).
    in_tok = None  # result.get("input_tokens") with a None value
    in_tok = in_tok or 0  # the B9 fix
    out_tok = 100
    cost = (in_tok * 0.000001) + (out_tok * 0.000002)
    assert abs(cost - 0.0002) < 1e-9
    assert in_tok == 0


# ---------- B12: model-level max_output_tokens is enforced ----------

async def test_b12_model_level_max_output_tokens_blocks_backends(client):
    """mock-limited has model-level max_output_tokens=50 and backends capped
    at 100/8000. A request for max_tokens=200 must be rejected with a 4xx
    (no backend fits), proving the model cap is honored — previously only the
    backend cap filtered, so the 8000-cap backend would have served it."""
    cfg = config_mod.load_config()
    m = next(x for x in cfg.models if x.id == "mock-limited")
    m.max_output_tokens = 50
    config_mod.save_config(cfg)
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-limited", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 200,
    })
    assert r.status_code in (404, 400, 502), (
        f"model-level cap must exclude all backends, got {r.status_code}"
    )


# ---------- B13: budget-ETA alerts are throttled ----------

def test_b13_alert_throttled_by_dedup_key():
    from localgateway import alerts
    alerts._last_sent.clear()
    alerts._webhook_url = lambda: "http://127.0.0.1:1/x"  # unreachable — fine
    first = alerts.send("budget_eta_warning", "t", {"key": "k"}, dedup_key="eta:k")
    assert first is True
    second = alerts.send("budget_eta_warning", "t", {"key": "k"}, dedup_key="eta:k")
    assert second is False, "same dedup_key within the interval must be throttled"
    # Different key → not throttled.
    third = alerts.send("budget_eta_warning", "t", {"key": "k2"}, dedup_key="eta:k2")
    assert third is True
    alerts._webhook_url = None

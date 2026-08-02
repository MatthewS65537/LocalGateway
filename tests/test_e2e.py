import pytest

from localgateway.ratelimit import ratelimit

from helpers import sse_collect


def _msg(events):
    return "".join(
        e["choices"][0]["delta"].get("content", "")
        for e in events
        if isinstance(e, dict) and e.get("choices")
    )


# ---------- chat: non-streaming ----------

async def test_non_stream_dual_emit_and_accounting(client, db_rows):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    d = r.json()
    msg = d["choices"][0]["message"]
    assert msg["reasoning_content"] == "I thought about it"
    assert msg["reasoning"] == "I thought about it"
    assert d["usage"]["completion_tokens_details"]["reasoning_tokens"] == 50

    rows = db_rows("mock-reasoner")
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] == 1
    assert row["cached_tokens"] == 12 and row["output_tokens"] == 200
    assert row["reasoning_tokens"] == 50
    assert row["cost"] > 0
    assert row["tps"] and row["tps"] > 0


# ---------- chat: streaming ----------

async def test_stream_dual_emit_usage_and_done(client, db_rows):
    events = await sse_collect(client, {
        "model": "mock-reasoner", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert events[-1] == "[DONE]"
    data = [e for e in events if isinstance(e, dict)]

    reasoning = [e for e in data if e.get("choices") and e["choices"][0].get("delta", {}).get("reasoning_content")]
    assert reasoning
    for e in reasoning:
        delta = e["choices"][0]["delta"]
        assert delta["reasoning"] == delta["reasoning_content"]

    usage_events = [e for e in data if e.get("usage")]
    assert usage_events and usage_events[-1]["usage"]["completion_tokens"] == 200
    assert _msg(events) == "0123456789" * 20

    rows = db_rows("mock-reasoner")
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] == 1 and row["stream"] == 1
    assert row["ttft_ms"] is not None
    assert row["reasoning_tokens"] == 50
    # 200 tokens over ~1s of generation -> ~200 tps (wide tolerance for CI)
    assert row["tps"] is not None and 80 <= row["tps"] <= 500


async def test_stream_failover_on_500(client, db_rows):
    events = await sse_collect(client, {
        "model": "mock-failover", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert events[-1] == "[DONE]"
    assert "0123456789" in _msg(events)

    rows = db_rows("mock-failover")
    failed = [r for r in rows if r["success"] == 0]
    ok = [r for r in rows if r["success"] == 1]
    assert failed and failed[0]["provider"] == "mock1"
    assert ok and ok[0]["provider"] == "mock2"


async def test_mid_stream_cut_emits_error_and_logs_failure(client, db_rows):
    events = await sse_collect(client, {
        "model": "mock-cutmodel", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert events[-1] == "[DONE]"
    errors = [e for e in events if isinstance(e, dict) and "error" in e]
    assert errors and errors[0]["error"]["code"] == "stream_interrupted"

    rows = db_rows("mock-cutmodel")
    assert rows and rows[-1]["success"] == 0
    assert rows[-1]["error"]


async def test_idle_timeout_falls_back(client, db_rows):
    events = await sse_collect(client, {
        "model": "mock-stallmodel", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert events[-1] == "[DONE]"
    assert "0123456789" in _msg(events)
    rows = db_rows("mock-stallmodel")
    assert any(r["success"] == 0 and "idle timeout" in (r["error"] or "") for r in rows)
    assert any(r["success"] == 1 and r["provider"] == "mock2" for r in rows)


async def test_rate_limit_cooldown_skips_backend(client, mock_servers):
    for _ in range(2):
        events = await sse_collect(client, {
            "model": "mock-429model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert events[-1] == "[DONE]"
        assert "0123456789" in _msg(events)

    assert "mock1:mock-429" in ratelimit.snapshot()
    # second request must NOT have hit the 429 backend again (cooldown skip)
    assert mock_servers["app1"].state.calls["mock-429"] == 1


# ---------- limit-aware routing ----------

async def test_limit_routing_to_capable_backend(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-limited", "max_tokens": 500,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.json()["choices"][0]["message"]["content"] == "hello from two"


async def test_limit_routing_prefers_tier1_when_fits(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-limited", "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.json()["choices"][0]["message"]["content"] == "hello from one"


async def test_limit_routing_rejects_when_none_fit(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-limited", "max_tokens": 9000,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "output_limit_exceeded"
    assert "8000" in err["message"]


async def test_disabled_model_404(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-disabled", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 404


async def test_unknown_model_404(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "nope", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 404


# ---------- catalog / admin ----------

async def test_models_endpoint_rich(client):
    r = await client.get("/v1/models")
    data = {m["id"]: m for m in r.json()["data"]}
    m = data["mock-reasoner"]
    assert m["description"] == "Reasoning test model"
    assert m["context_length"] == 32768
    assert m["backend_count"] == 2
    assert float(m["pricing"]["prompt"]) == 1e-06
    assert "mock-disabled" not in data


async def test_admin_overview(client):
    await client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    r = await client.get("/admin/models/overview?hours=24")
    models = {m["id"]: m for m in r.json()["models"]}
    m = models["mock-reasoner"]
    assert m["requests"] >= 1
    assert m["tokens"] >= 200
    assert m["backend_count"] == 2
    assert m["enabled"] is True
    assert models["mock-disabled"]["enabled"] is False


async def test_admin_stats_percentiles(client):
    for _ in range(2):
        await sse_collect(client, {
            "model": "mock-reasoner", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
    r = await client.get("/admin/models/mock-reasoner/stats?hours=24&p=p50")
    assert r.status_code == 200
    body = r.json()
    assert body["percentile"] == "p50"
    rows = {b["provider"]: b for b in body["backends"]}
    b1 = rows["mock1"]
    assert b1["context_length"] == 32768
    assert b1["max_output_tokens"] == 4096
    assert b1["requests"] >= 1
    assert b1["ttft_ms"] is not None
    assert b1["tps"] is not None and 80 <= b1["tps"] <= 500
    assert b1["success_rate"] == 100.0

    r90 = await client.get("/admin/models/mock-reasoner/stats?hours=24&p=p90")
    assert r90.json()["percentile"] == "p90"


async def test_admin_series(client):
    await sse_collect(client, {
        "model": "mock-reasoner", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    r = await client.get("/admin/models/mock-reasoner/series?hours=24")
    body = r.json()
    assert body["bucket_seconds"] == 3600
    pts = body["series"].get("mock1:mock-reasoner", [])
    assert pts and pts[0]["tps"] > 0


async def test_admin_discover(client):
    r = await client.get("/admin/providers/mock1/models")
    ids = [m["id"] for m in r.json()["models"]]
    assert "mock-reasoner" in ids and "mock-plain" in ids


async def test_admin_backend_test_probe(client):
    r = await client.post("/admin/backends/test", json={
        "provider": "mock2", "model": "mock-reasoner", "stream": True,
    })
    body = r.json()
    assert body["ok"] is True
    assert body["latency_ms"] >= 0


async def test_admin_backend_test_skips_snoozed(client):
    from localgateway.ratelimit import ratelimit
    ratelimit.snooze_permanent("mock2", "mock-reasoner")
    try:
        r = await client.post("/admin/backends/test", json={
            "provider": "mock2", "model": "mock-reasoner", "stream": True,
        })
        body = r.json()
        assert body["ok"] is False
        assert body["skipped"] is True
    finally:
        ratelimit.unsnooze("mock2", "mock-reasoner")


async def test_admin_health(client):
    r = await client.get("/admin/health")
    body = r.json()
    assert "backends" in body and "stats" in body and "providers" in body
    b = [x for x in body["backends"] if x["backend_model"] == "mock-dead"]
    assert b and b[0]["priority"] == 1


async def test_admin_snooze_unsnooze_all(client):
    r = await client.post("/admin/backends/snooze", json={
        "provider": "mock1", "model": "mock-plain", "permanent": True,
    })
    assert r.status_code == 200 and r.json()["remaining"] == -1
    r = await client.get("/admin/rate-limits")
    snap = r.json()
    assert snap.get("mock1:mock-plain") == -1

    r = await client.post("/admin/backends/unsnooze", json={
        "provider": "mock1", "model": "mock-plain",
    })
    assert r.status_code == 200
    snap = (await client.get("/admin/rate-limits")).json()
    assert "mock1:mock-plain" not in snap

    await client.post("/admin/backends/snooze", json={
        "provider": "mock1", "model": "mock-plain", "permanent": True,
    })
    r = await client.post("/admin/backends/unsnooze-all")
    assert r.status_code == 200 and r.json()["cleared"] >= 1
    snap = (await client.get("/admin/rate-limits")).json()
    assert "mock1:mock-plain" not in snap


async def test_admin_unsnooze_orphaned_key(client):
    r = await client.post("/admin/backends/unsnooze", json={
        "provider": "ghost-provider", "model": "ghost-model",
    })
    assert r.status_code == 200


async def test_alias_and_provider_headers(client, db_rows):
    r = await client.post("/v1/chat/completions", json={
        "model": "reasoner-alias", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    assert r.headers.get("x-provider") in ("mock1", "mock2")
    assert r.headers.get("x-backend", "").startswith(r.headers.get("x-provider") + "/")
    rows = db_rows("mock-reasoner")
    assert rows and rows[-1]["success"] == 1


async def test_api_v1_models_and_chat(client):
    r = await client.get("/api/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "mock-reasoner" in ids
    m = next(x for x in r.json()["data"] if x["id"] == "mock-reasoner")
    assert m.get("aliases") == ["reasoner-alias"]
    assert m.get("name") == "Mock Reasoner"

    r2 = await client.post("/api/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    assert r2.status_code == 200
    assert r2.headers.get("x-provider")


async def test_provider_prefs_ignore_e2e(client, db_rows):
    r = await client.post("/v1/chat/completions", json={
        "model": "mock-reasoner",
        "messages": [{"role": "user", "content": "hi"}],
        "provider": {"ignore": ["mock1"], "allow_fallbacks": True},
    })
    assert r.status_code == 200
    assert r.headers.get("x-provider") == "mock2"

"""Governance: multi-API-key auth, RPM limits, budgets, allowlists, attribution."""
from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from conftest import build_config

from localgateway import clientquota
from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr
from localgateway.usage import set_db_path
from localgateway.worker import create_app


def _cfg_with_keys(p1: int, p2: int) -> dict:
    cfg = build_config(p1, p2)
    cfg["server"]["api_keys"] = [
        {"id": "k-full", "key": "secret-full", "label": "Full access"},
        {"id": "k-rpm", "key": "secret-rpm", "rpm": 1},
        {"id": "k-budget", "key": "secret-budget", "daily_budget_usd": 0.0001},
        {"id": "k-limited", "key": "secret-limited", "model_allowlist": ["mock-limited"]},
        {"id": "k-off", "key": "secret-off", "enabled": False},
    ]
    return cfg


@pytest.fixture()
def gov_app(mock_servers, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_cfg_with_keys(mock_servers["p1"], mock_servers["p2"])))
    db_path = tmp_path / "usage.db"

    config_mod._config = None
    config_mod._config_mtime = -1.0
    usage_mod._initialized = False
    logs_mod._initialized = False
    stats_mod.reset()
    clientquota.reset_state()
    ratelimit._cooldowns.clear()
    ratelimit._permanent.clear()
    _rr.clear()

    app = create_app(str(cfg_path))
    set_db_path(str(db_path))
    logs_mod.set_db_path(str(db_path))
    # Force sync writes for deterministic test reads.
    from localgateway import usage as _u, logs as _l
    _u.stop_writer()
    _l.stop_writer()
    return app, db_path


@pytest.fixture()
async def gclient(gov_app):
    app, _ = gov_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


def _chat(model: str = "mock-reasoner", user: str | None = None) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if user:
        body["user"] = user
    return body


# ---- auth ----

@pytest.mark.asyncio
async def test_no_key_rejected_when_api_keys_configured(gclient):
    r = await gclient.post("/v1/chat/completions", json=_chat())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unknown_key_rejected(gclient):
    r = await gclient.post("/v1/chat/completions", json=_chat(),
                           headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_accepted(gclient):
    r = await gclient.post("/v1/chat/completions", json=_chat(),
                           headers={"Authorization": "Bearer secret-full"})
    assert r.status_code == 200
    assert r.headers.get("X-Provider") == "mock1"


@pytest.mark.asyncio
async def test_disabled_key_rejected(gclient):
    r = await gclient.post("/v1/chat/completions", json=_chat(),
                           headers={"Authorization": "Bearer secret-off"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_legacy_key_fallback_when_api_keys_present(gov_app, mock_servers, tmp_path):
    """server.api_key still works as a full-access fallback alongside api_keys."""
    app, _ = gov_app
    cfg = config_mod.load_config()
    cfg.server.api_key = "legacy-secret"
    config_mod.save_config(cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post("/v1/chat/completions", json=_chat(),
                         headers={"Authorization": "Bearer legacy-secret"})
        assert r.status_code == 200


# ---- quotas ----

@pytest.mark.asyncio
async def test_rpm_limit(gclient):
    h = {"Authorization": "Bearer secret-rpm"}
    r1 = await gclient.post("/v1/chat/completions", json=_chat(), headers=h)
    assert r1.status_code == 200
    r2 = await gclient.post("/v1/chat/completions", json=_chat(), headers=h)
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "rpm_exceeded"
    assert "Retry-After" in r2.headers


@pytest.mark.asyncio
async def test_daily_budget_hard_stop(gclient, gov_app):
    h = {"Authorization": "Bearer secret-budget"}
    r1 = await gclient.post("/v1/chat/completions", json=_chat(), headers=h)
    assert r1.status_code == 200
    clientquota.reset_state()  # drop the 5s spend cache so the new row is seen
    r2 = await gclient.post("/v1/chat/completions", json=_chat(), headers=h)
    assert r2.status_code == 402
    assert r2.json()["error"]["code"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_model_allowlist(gclient):
    h = {"Authorization": "Bearer secret-limited"}
    ok = await gclient.post("/v1/chat/completions", json=_chat("mock-limited"), headers=h)
    assert ok.status_code == 200
    denied = await gclient.post("/v1/chat/completions", json=_chat("mock-reasoner"), headers=h)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "model_not_allowed"


# ---- attribution ----

@pytest.mark.asyncio
async def test_key_and_user_attribution(gclient, gov_app):
    _, db_path = gov_app
    h = {"Authorization": "Bearer secret-full"}
    r = await gclient.post("/v1/chat/completions", json=_chat(user="agent-007"), headers=h)
    assert r.status_code == 200
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM usage ORDER BY id").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["api_key_id"] == "k-full"
    assert rows[0]["end_user"] == "agent-007"


@pytest.mark.asyncio
async def test_open_gateway_attribution_empty(gov_app, mock_servers, tmp_path):
    """With no keys configured, requests still work and log no key id."""
    app, db_path = gov_app
    cfg = config_mod.load_config()
    cfg.server.api_keys = []
    config_mod.save_config(cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post("/v1/chat/completions", json=_chat())
        assert r.status_code == 200
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT api_key_id FROM usage").fetchone()
    conn.close()
    assert row["api_key_id"] is None


# ---- admin endpoints ----

@pytest.mark.asyncio
async def test_keys_spend_endpoint(gclient):
    h = {"Authorization": "Bearer secret-full"}
    await gclient.post("/v1/chat/completions", json=_chat(), headers=h)
    r = await gclient.get("/admin/keys/spend")
    assert r.status_code == 200
    keys = {k["id"]: k for k in r.json()["keys"]}
    assert "k-full" in keys
    assert keys["k-full"]["day_requests"] >= 1
    assert keys["k-budget"]["daily_budget_usd"] == 0.0001


@pytest.mark.asyncio
async def test_reveal_server_key(gclient):
    r = await gclient.post("/admin/config/server-key/k-full/reveal")
    assert r.status_code == 200
    assert r.json()["key"] == "secret-full"
    r404 = await gclient.post("/admin/config/server-key/nope/reveal")
    assert r404.status_code == 404


@pytest.mark.asyncio
async def test_config_redacts_server_keys(gclient):
    r = await gclient.get("/admin/config")
    assert r.status_code == 200
    data = r.json()
    for k in data["server"]["api_keys"]:
        assert k["key"] == "••••••"
    # Round-trip PUT must not clobber the real keys.
    r2 = await gclient.put("/admin/config", json=data)
    assert r2.status_code == 200
    cfg = config_mod.load_config()
    secrets = {k.id: k.key for k in cfg.server.api_keys}
    assert secrets["k-full"] == "secret-full"


@pytest.mark.asyncio
async def test_usage_summary_by_key_and_user(gclient):
    h = {"Authorization": "Bearer secret-full"}
    await gclient.post("/v1/chat/completions", json=_chat(user="u1"), headers=h)
    r = await gclient.get("/admin/usage?hours=1")
    assert r.status_code == 200
    data = r.json()
    assert "k-full" in data["by_key"]
    assert "u1" in data["by_user"]

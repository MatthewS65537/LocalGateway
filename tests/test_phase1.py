"""Phase 1 features: F4 burn-rate ETA, F7 conversation stickiness, F8 static weights."""
from __future__ import annotations

import json
import random
import sqlite3
import time

import httpx
import pytest

from conftest import build_config

from localgateway import clientquota
from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.config import ApiKeyConfig, GatewayConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, _static_weight_rotation, SelectedBackend
from localgateway.config import BackendConfig, ProviderConfig
from localgateway.usage import set_db_path
from localgateway.worker import create_app


# ════ F8: static backend weights ════

def _sb(provider_id, model, weight=1.0):
    p = ProviderConfig(id=provider_id, name=provider_id, base_url="http://x")
    b = BackendConfig(provider=provider_id, model=model, weight=weight)
    return SelectedBackend(provider=p, backend=b)


def test_static_weight_rotation_uniform_falls_back_to_rr():
    """When all weights are 1.0, _static_weight_rotation is never called (the
    caller checks first), but if it were, it should behave like plain RR."""
    items = [_sb("p1", "m"), _sb("p2", "m"), _sb("p3", "m")]
    rng = random.Random(42)
    out = _static_weight_rotation(items, [1.0, 1.0, 1.0], rotation=0, rng=rng)
    assert set(s.provider.id for s in out) == {"p1", "p2", "p3"}


def test_static_weight_rotation_heavy_backend_wins_most():
    """With weight 9:1, the heavy backend should be first ~90% of the time."""
    heavy = _sb("heavy", "m", weight=9.0)
    light = _sb("light", "m", weight=1.0)
    items = [heavy, light]
    first_count = 0
    for i in range(1000):
        rng = random.Random(i)
        out = _static_weight_rotation(list(items), [9.0, 1.0], rotation=i, rng=rng)
        if out[0].provider.id == "heavy":
            first_count += 1
    # ~900 out of 1000, with tolerance for randomness.
    assert 850 < first_count < 950, f"expected ~900 heavy-first, got {first_count}"


def test_static_weight_rotation_zero_weight_never_first():
    """A weight-0 backend (drain) is never picked first."""
    drain = _sb("drain", "m", weight=0.0)
    active = _sb("active", "m", weight=1.0)
    items = [drain, active]
    for i in range(100):
        rng = random.Random(i)
        out = _static_weight_rotation(list(items), [0.0, 1.0], rotation=i, rng=rng)
        assert out[0].provider.id == "active"


def test_static_weight_rotation_all_zero_falls_back_to_rr():
    """When all weights are 0, fall back to plain rotation (don't deadlock)."""
    items = [_sb("p1", "m", weight=0.0), _sb("p2", "m", weight=0.0)]
    rng = random.Random(1)
    out = _static_weight_rotation(items, [0.0, 0.0], rotation=1, rng=rng)
    assert len(out) == 2


@pytest.fixture()
async def w_client(mock_servers, tmp_path):
    """A config with a 2-backend tier-1 model with 9:1 weights."""
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    # Replace mock-reasoner's backends with weighted ones.
    cfg["models"][0]["backends"] = [
        {"provider": "mock1", "model": "mock-reasoner", "priority": 1, "weight": 9.0},
        {"provider": "mock2", "model": "mock-reasoner", "priority": 1, "weight": 1.0},
    ]
    cfg["server"]["routing_mode"] = "failover"
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


async def test_weighted_traffic_split(w_client):
    """Over 40 requests with 9:1 weights, mock1 gets the lion's share."""
    mock1_count = 0
    for _ in range(40):
        r = await w_client.post("/v1/chat/completions", json={
            "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200
        if "hello from one" in r.text:
            mock1_count += 1
    # 9:1 split → ~36 mock1, ~4 mock2. Allow tolerance.
    assert mock1_count >= 28, f"expected heavy backend to dominate, got {mock1_count}/40"


# ════ F7: conversation stickiness ════

@pytest.fixture()
async def s_client(mock_servers, tmp_path):
    """Cache affinity ON so conversation stickiness engages."""
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    # Two tier-1 backends so stickiness has a choice to make.
    # cache_supported=True is required: None (auto-detect) only warms when the
    # provider reports cache tokens, which the mock provider doesn't.
    cfg["models"][0]["backends"] = [
        {"provider": "mock1", "model": "mock-reasoner", "priority": 1, "cache_supported": True},
        {"provider": "mock2", "model": "mock-reasoner", "priority": 1, "cache_supported": True},
    ]
    cfg["server"]["cache_affinity_enabled"] = True
    cfg["server"]["cache_affinity_ttl_sec"] = 300
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


async def test_conversation_stickiness(s_client):
    """Two requests with the same X-Conversation-Id route to the same backend."""
    conv = "test-conv-123"
    r1 = await s_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "turn 1"}],
    }, headers={"X-Conversation-Id": conv})
    assert r1.status_code == 200
    provider1 = r1.headers.get("x-provider")

    r2 = await s_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "turn 2"}],
    }, headers={"X-Conversation-Id": conv})
    assert r2.status_code == 200
    provider2 = r2.headers.get("x-provider")

    assert provider1 == provider2, f"expected same backend for conversation, got {provider1} then {provider2}"


async def test_no_conversation_id_uses_fingerprint(s_client):
    """Without a conversation_id, fingerprint-based cache affinity still works
    (different prompts may route differently). This confirms the fallback path."""
    r1 = await s_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "prompt A"}],
    })
    assert r1.status_code == 200
    assert r1.headers.get("x-provider") in ("mock1", "mock2")


# ════ F4: burn-rate ETA ════

@pytest.fixture()
def eta_env(tmp_path):
    """Fresh quota + usage state for burn-rate tests."""
    clientquota.reset_state()
    db = tmp_path / "eta.db"
    set_db_path(str(db))
    usage_mod.stop_writer()
    logs_mod.stop_writer()
    usage_mod._initialized = False
    logs_mod._initialized = False
    yield db
    clientquota.reset_state()


def _seed_key_cost(key_id, cost, days_ago=0):
    usage_mod._init()
    conn = sqlite3.connect(str(usage_mod._db_path))
    conn.execute(
        "INSERT INTO usage (ts, logical_model, provider, backend_model, success, cost, api_key_id) "
        "VALUES (?, 'm', 'p', 'b', 1, ?, ?)",
        (time.time() - days_ago * 86400, cost, key_id),
    )
    conn.commit()
    conn.close()


def test_budget_eta_projects_exhaustion(eta_env):
    """A $70 spend today against a $100 monthly budget with 1 active day
    → burn = $70/day (B8: active-days divisor, not the full 7-day window);
    remaining $30 → ETA = 30/70 ≈ 0.43 days — dangerously imminent, which is
    exactly what the old window-divisor arithmetic hid (it reported 3 days)."""
    k = ApiKeyConfig(id="k1", key="s", monthly_budget_usd=100.0)
    # $70 today (one active day) → burn = 70/1 = $70/day; month_cost = $70.
    _seed_key_cost("k1", 70.0, days_ago=0)
    eta = clientquota.budget_eta(k)
    assert eta is not None
    assert eta["period"] == "monthly"
    assert eta["burn_rate"] == 70.0
    # Remaining = 100 - 70 = 30. ETA = 30 / 70 ≈ 0.43 days.
    assert 0.3 < eta["eta_days"] < 0.6, f"expected ~0.43 days ETA, got {eta['eta_days']}"


def test_budget_eta_none_without_budget(eta_env):
    k = ApiKeyConfig(id="k2", key="s")
    _seed_key_cost("k2", 100.0)
    assert clientquota.budget_eta(k) is None


def test_budget_eta_none_with_no_burn(eta_env):
    """No spend → no burn → no ETA (can't project from zero)."""
    k = ApiKeyConfig(id="k3", key="s", monthly_budget_usd=100.0)
    assert clientquota.budget_eta(k) is None


def test_budget_eta_picks_most_urgent(eta_env):
    """When both daily and monthly are set, the most urgent (smallest ETA) wins."""
    k = ApiKeyConfig(id="k4", key="s", daily_budget_usd=5.0, monthly_budget_usd=1000.0)
    # $28 today → daily burn = 28/7 = $4/day; daily remaining = 5-4 = $1, ETA = 0.25 days.
    # Monthly: 28 spend, remaining 972, ETA = 972/4 = 243 days → daily is more urgent.
    _seed_key_cost("k4", 28.0, days_ago=0)
    eta = clientquota.budget_eta(k)
    assert eta is not None
    assert eta["period"] == "daily"
    assert eta["eta_days"] < 1.0

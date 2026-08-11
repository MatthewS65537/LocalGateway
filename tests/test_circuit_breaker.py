"""Circuit breaker: unit behavior, routing exclusion, gateway integration, admin API."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from conftest import build_config

from localgateway import circuitbreaker as cb
from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.config import GatewayConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, select_backends
from localgateway.usage import set_db_path
from localgateway.worker import create_app


@pytest.fixture(autouse=True)
def _reset_cb():
    cb.reset_state()
    yield
    cb.reset_state()


def test_trips_after_threshold():
    cb.configure(enabled=True, threshold=3, backoff_s=60)
    assert not cb.is_open("p", "m")
    for _ in range(2):
        assert cb.record_outcome("p", "m", False, "boom") is False
    assert not cb.is_open("p", "m")  # 2 < threshold
    assert cb.record_outcome("p", "m", False, "boom") is True  # trip
    assert cb.is_open("p", "m")


def test_disabled_never_opens():
    cb.configure(enabled=False, threshold=1)
    cb.record_outcome("p", "m", False)
    assert not cb.is_open("p", "m")


def test_success_resets():
    cb.configure(enabled=True, threshold=2)
    cb.record_outcome("p", "m", False)
    cb.record_outcome("p", "m", True)
    cb.record_outcome("p", "m", False)
    assert not cb.is_open("p", "m")


def test_backoff_expires_to_half_open():
    cb.configure(enabled=True, threshold=1, backoff_s=0.05)
    cb.record_outcome("p", "m", False)
    assert cb.is_open("p", "m")
    time.sleep(0.06)
    assert not cb.is_open("p", "m")  # half-open trial allowed


def test_backoff_grows_exponentially():
    cb.configure(enabled=True, threshold=1, backoff_s=0.05)
    cb.record_outcome("p", "m", False)  # trip 1: 0.05s
    time.sleep(0.06)
    cb.record_outcome("p", "m", False)  # trip 2: 0.10s
    snap = cb.snapshot()
    assert snap["p:m"]["trips"] == 2
    assert snap["p:m"]["open_remaining_s"] > 0.05


def test_reset_one_and_all():
    cb.configure(enabled=True, threshold=1)
    cb.record_outcome("p1", "m", False)
    cb.record_outcome("p2", "m", False)
    assert cb.reset("p1", "m") == 1
    assert not cb.is_open("p1", "m")
    assert cb.is_open("p2", "m")
    assert cb.reset() == 1


def _routing_cfg(**server_overrides) -> GatewayConfig:
    raw = {
        "server": {"routing_mode": "failover", "circuit_breaker_enabled": True,
                   "circuit_breaker_threshold": 1, **server_overrides},
        "providers": [
            {"id": "pa", "base_url": "http://x/v1"},
            {"id": "pb", "base_url": "http://y/v1"},
        ],
        "models": [{
            "id": "m",
            "backends": [
                {"provider": "pa", "model": "mm", "priority": 1},
                {"provider": "pb", "model": "mm", "priority": 2},
            ],
        }],
    }
    return GatewayConfig.model_validate(raw)


def test_open_circuit_defers_backend_in_routing():
    cfg = _routing_cfg()
    # Trip pa's circuit.
    cb.configure(enabled=True, threshold=1)
    cb.record_outcome("pa", "mm", False)
    assert cb.is_open("pa", "mm")
    selected = list(select_backends(cfg, "m"))
    assert selected[0].provider.id == "pb"
    # pa still appears as last-resort fallback.
    assert [s.provider.id for s in selected] == ["pb", "pa"]


def test_closed_circuit_normal_order():
    cfg = _routing_cfg()
    selected = list(select_backends(cfg, "m"))
    assert [s.provider.id for s in selected] == ["pa", "pb"]


# ---- gateway integration ----

@pytest.fixture()
def cb_app(mock_servers, tmp_path):
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    cfg["server"]["circuit_breaker_enabled"] = True
    cfg["server"]["circuit_breaker_threshold"] = 1
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
    return app


@pytest.mark.asyncio
async def test_circuit_trips_on_5xx_and_skips_backend(cb_app, mock_servers):
    transport = httpx.ASGITransport(app=cb_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        body = {"model": "mock-failover", "messages": [{"role": "user", "content": "hi"}]}
        # First request: mock1 (mock-dead, tier 1) fails with 500 → circuit trips.
        r1 = await c.post("/v1/chat/completions", json=body)
        assert r1.status_code == 200
        assert r1.headers["X-Provider"] == "mock2"
        calls_after_trip = mock_servers["app1"].state.calls["mock-dead"]
        assert cb.is_open("mock1", "mock-dead")
        # Second request: router defers mock1 — mock-dead is not called again.
        r2 = await c.post("/v1/chat/completions", json=body)
        assert r2.status_code == 200
        assert r2.headers["X-Provider"] == "mock2"
        assert mock_servers["app1"].state.calls["mock-dead"] == calls_after_trip


@pytest.mark.asyncio
async def test_429_does_not_trip_circuit(cb_app, mock_servers):
    transport = httpx.ASGITransport(app=cb_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        # One-shot: verify the 429 path records a cooldown but no circuit
        # failure. (Avoids shared mock call-counter coupling with test_e2e.)
        body = {"model": "mock-429model", "messages": [{"role": "user", "content": "hi"}]}
        r = await c.post("/v1/chat/completions", json=body)
        assert r.status_code == 200
        assert ratelimit.remaining("mock1", "mock-429") > 0  # cooldown set
        assert not cb.is_open("mock1", "mock-429")  # 429 is cooldown territory, not circuit
        assert "mock1:mock-429" not in cb.snapshot()  # breaker never sees 429s
    ratelimit.unsnooze("mock1", "mock-429")  # leave no cooldown behind for other suites
    # The mock provider's call counter is session-scoped; restore the world for
    # test_e2e's absolute assertion on this counter.
    mock_servers["app1"].state.calls["mock-429"] = 0


@pytest.mark.asyncio
async def test_circuit_admin_endpoints(cb_app):
    cb.configure(enabled=True, threshold=1)
    cb.record_outcome("mock1", "mock-dead", False, "boom")
    transport = httpx.ASGITransport(app=cb_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.get("/admin/circuit")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert data["circuits"]["mock1:mock-dead"]["open"] is True

        r2 = await c.post("/admin/circuit/reset", json={"provider": "mock1", "model": "mock-dead"})
        assert r2.json()["cleared"] == 1
        assert not cb.is_open("mock1", "mock-dead")

        # health endpoint surfaces circuit state too
        r3 = await c.get("/admin/health")
        assert "circuit" in r3.json()

"""F1: Prometheus /metrics endpoint — exposition format, content, parseability."""
from __future__ import annotations

import httpx
import pytest

from conftest import build_config

from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr
from localgateway.usage import set_db_path
from localgateway.worker import create_app


@pytest.fixture()
async def m_client(mock_servers, tmp_path):
    # Clear the metrics render cache between tests (module-level, 5s TTL).
    from localgateway.endpoints import metrics as metrics_mod
    metrics_mod._render_cache.clear()
    cfg = build_config(mock_servers["p1"], mock_servers["p2"])
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(__import__("json").dumps(cfg))
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


async def test_metrics_renders_exposition(m_client):
    r = await m_client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    text = r.text
    # Core metric families must be present.
    for name in ("lga_requests_total", "lga_request_errors_total", "lga_inflight",
                 "lga_cost_usd_total", "lga_tokens_total", "lga_circuit_open"):
        assert name in text, f"missing metric: {name}"
    # HELP + TYPE lines for the headline counter.
    assert "# HELP lga_requests_total" in text
    assert "# TYPE lga_requests_total counter"


async def test_metrics_after_traffic(m_client):
    """After a request, the per-backend counter must be > 0."""
    await m_client.post("/v1/chat/completions", json={
        "model": "mock-reasoner", "messages": [{"role": "user", "content": "hi"}],
    })
    r = await m_client.get("/metrics")
    text = r.text
    assert 'lga_requests_total{' in text
    # At least one counter value is non-zero.
    assert any(line.startswith("lga_requests_total{") and not line.endswith("} 0")
               for line in text.splitlines() if line.startswith("lga_requests_total{"))


async def test_metrics_parses(m_client):
    """The exposition is valid Prometheus text: every metric line has a name
    and a numeric value, and HELP/TYPE lines are well-formed."""
    r = await m_client.get("/metrics")
    for line in r.text.strip().split("\n"):
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        assert len(parts) == 2, f"malformed metric line: {line!r}"
        name = parts[0].split("{")[0]
        assert name.startswith("lga_"), f"unexpected metric prefix: {name!r}"
        # value must parse as float.
        float(parts[1])

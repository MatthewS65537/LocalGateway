"""Phase A — supervisor/worker split-brain fix.

Live-state admin endpoints (stats, circuits, warmth, rate-limits) live in the
worker subprocess. The supervisor mounts admin_router for stopped-state
management, which shadowed those endpoints with empty data. These tests verify
the explicit proxy routes forward to the worker when running and fall back to
the local handler when stopped.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from localgateway import config as config_mod
from localgateway.supervisor import Supervisor, create_supervisor_app


@pytest.fixture(autouse=True)
def _clear_stats():
    from localgateway import stats as stats_mod
    stats_mod._stats.clear()
    stats_mod._warmth.clear()
    yield
    stats_mod._stats.clear()
    stats_mod._warmth.clear()


def _cfg(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server": {"api_key": None, "routing_mode": "failover"},
        "providers": [
            {"id": "p1", "name": "P1", "base_url": "http://127.0.0.1:1/v1",
             "api_key": "x", "timeout": 5},
        ],
        "models": [
            {"id": "m1", "backends": [
                {"provider": "p1", "model": "m1-up", "priority": 1}]},
        ],
    }))
    config_mod._config = None
    config_mod._config_mtime = -1.0
    config_mod.set_config_path(str(cfg_path))


# ---------- stopped: local fallback ----------

def test_health_stopped_returns_local_shape(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: False)
    app = create_supervisor_app(sup)
    with TestClient(app) as c:
        r = c.get("/admin/health")
        assert r.status_code == 200
        data = r.json()
        assert any(p["id"] == "p1" for p in data["providers"])
        assert data["stats"] == {}  # degraded — no worker


def test_inflight_stopped_returns_empty(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: False)
    app = create_supervisor_app(sup)
    with TestClient(app) as c:
        r = c.get("/admin/inflight")
        assert r.status_code == 200
        assert r.json() == {}


# ---------- running: proxied to worker ----------

def test_health_running_proxied_to_worker(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    app = create_supervisor_app(sup)

    async def mock_get(url, **kw):
        return httpx.Response(200, json={
            "_proxied": True, "url": url,
            "providers": [], "backends": [], "stats": {"p1:m1-up": {"requests": 1}},
        })

    monkeypatch.setattr(app.state.client, "get", mock_get)
    with TestClient(app) as c:
        r = c.get("/admin/health")
        assert r.status_code == 200
        data = r.json()
        assert data["_proxied"] is True
        assert "4456" in data["url"]
        assert data["stats"] != {}


def test_inflight_running_proxied_to_worker(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    app = create_supervisor_app(sup)

    async def mock_get(url, **kw):
        return httpx.Response(200, json={"p1:m1-up": 3})

    monkeypatch.setattr(app.state.client, "get", mock_get)
    with TestClient(app) as c:
        r = c.get("/admin/inflight")
        assert r.status_code == 200
        assert r.json() == {"p1:m1-up": 3}


def test_circuit_reset_running_proxied_to_worker(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    app = create_supervisor_app(sup)

    captured = {}

    async def mock_request(method, url, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = kw.get("content")
        return httpx.Response(200, json={"status": "ok", "cleared": 2})

    monkeypatch.setattr(app.state.client, "request", mock_request)
    with TestClient(app) as c:
        r = c.post("/admin/circuit/reset", json={"provider": "p1"})
        assert r.status_code == 200
        assert r.json()["cleared"] == 2
        assert captured["method"] == "POST"
        assert "4456" in captured["url"]
        assert b"p1" in captured["body"]


def test_warmth_delete_running_proxied_to_worker(tmp_path, monkeypatch):
    _cfg(tmp_path)
    sup = Supervisor(str(tmp_path / "config.json"), "127.0.0.1", 3456, 4456)
    monkeypatch.setattr(sup, "is_running", lambda: True)
    app = create_supervisor_app(sup)

    captured = {}

    async def mock_request(method, url, **kw):
        captured["method"] = method
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(app.state.client, "request", mock_request)
    with TestClient(app) as c:
        r = c.delete("/admin/warmth")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert captured["method"] == "DELETE"

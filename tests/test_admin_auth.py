from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from localgateway import config as config_mod
from localgateway.auth import check_api_key, is_loopback_host
from localgateway.supervisor import create_supervisor_app
from localgateway.worker import create_app

from conftest import build_config  # noqa: F401  (fixtures import path)


class _FakeRequest:
    def __init__(self, host: str, auth: str | None = None):
        self.client = type("C", (), {"host": host})()
        self.headers = {}
        if auth:
            self.headers["Authorization"] = auth


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.8.9.10", True),
    ("::1", True),
    ("::ffff:127.0.0.1", True),
    ("localhost", True),
    ("192.168.1.5", False),
    ("10.0.0.2", False),
    ("", False),
    (None, False),
])
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def _config_with_key(key="secret-key-123"):
    cfg = config_mod.GatewayConfig()
    cfg.server.api_key = key
    return cfg


def test_check_api_key_no_key_allows_everyone():
    cfg = config_mod.GatewayConfig()
    cfg.server.api_key = None
    assert check_api_key(_FakeRequest("192.168.1.5"), cfg) is True
    assert check_api_key(_FakeRequest("127.0.0.1"), cfg) is True


def test_check_api_key_loopback_exempt():
    cfg = _config_with_key()
    assert check_api_key(_FakeRequest("127.0.0.1"), cfg) is True
    assert check_api_key(_FakeRequest("::1"), cfg) is True


def test_check_api_key_non_loopback_needs_token():
    cfg = _config_with_key()
    assert check_api_key(_FakeRequest("192.168.1.5"), cfg) is False
    assert check_api_key(_FakeRequest("192.168.1.5", "wrong"), cfg) is False
    assert check_api_key(_FakeRequest("192.168.1.5", "Bearer secret-key-123"), cfg) is True
    assert check_api_key(_FakeRequest("192.168.1.5", "secret-key-123"), cfg) is True


# ---------- worker app (ASGI-level) ----------

@pytest.fixture()
def worker_with_key(mock_servers, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(build_config(mock_servers["p1"], mock_servers["p2"])))
    cfg = json.loads(cfg_path.read_text())
    cfg["server"]["api_key"] = "secret-key-123"
    cfg_path.write_text(json.dumps(cfg))

    config_mod._config = None
    config_mod._config_mtime = -1.0

    app = create_app(str(cfg_path))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def remote_client(monkeypatch):
    """Force the middleware to see a non-loopback peer address."""
    from localgateway import auth as auth_mod

    monkeypatch.setattr(auth_mod, "client_host", lambda req: "203.0.113.7")
    yield


@pytest.fixture()
def loopback_client(monkeypatch):
    from localgateway import auth as auth_mod

    monkeypatch.setattr(auth_mod, "client_host", lambda req: "127.0.0.1")
    yield


def test_worker_admin_loopback_exempt(worker_with_key, loopback_client):
    r = worker_with_key.get("/admin/config")
    assert r.status_code == 200


def test_worker_admin_non_loopback_requires_key(worker_with_key, remote_client):
    r = worker_with_key.get("/admin/config")
    assert r.status_code == 401


def test_worker_admin_non_loopback_with_key(worker_with_key, remote_client):
    r = worker_with_key.get(
        "/admin/config",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert r.status_code == 200


def test_worker_api_requires_key_even_on_loopback(worker_with_key):
    r = worker_with_key.get("/v1/models")
    assert r.status_code == 401
    r = worker_with_key.get("/v1/models", headers={"Authorization": "Bearer secret-key-123"})
    assert r.status_code == 200


def test_worker_pages_loopback_exempt(worker_with_key, loopback_client):
    for path in ["/", "/models", "/providers", "/usage", "/logs", "/settings"]:
        r = worker_with_key.get(path)
        assert r.status_code == 200, path


def test_worker_pages_non_loopback_requires_key(worker_with_key, remote_client):
    for path in ["/", "/models"]:
        r = worker_with_key.get(path)
        assert r.status_code == 401, path
        r = worker_with_key.get(path, headers={"Authorization": "Bearer secret-key-123"})
        assert r.status_code == 200, path


# ---------- supervisor app (ASGI-level) ----------

@pytest.fixture()
def supervisor_with_key(mock_servers, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(build_config(mock_servers["p1"], mock_servers["p2"])))
    cfg = json.loads(cfg_path.read_text())
    cfg["server"]["api_key"] = "secret-key-123"
    cfg_path.write_text(json.dumps(cfg))

    config_mod._config = None
    config_mod._config_mtime = -1.0
    config_mod.set_config_path(str(cfg_path))

    from localgateway.supervisor import Supervisor

    sup = Supervisor(str(cfg_path), "127.0.0.1", 3456, 4456)
    app = create_supervisor_app(sup)
    with TestClient(app) as c:
        yield c


def test_supervisor_admin_loopback_exempt(supervisor_with_key, loopback_client):
    r = supervisor_with_key.get("/admin/server/status")
    assert r.status_code == 200


def test_supervisor_admin_non_loopback_requires_key(supervisor_with_key, remote_client):
    r = supervisor_with_key.get("/admin/server/status")
    assert r.status_code == 401


def test_supervisor_admin_non_loopback_with_key(supervisor_with_key, remote_client):
    r = supervisor_with_key.get(
        "/admin/server/status",
        headers={"Authorization": "Bearer secret-key-123"},
    )
    assert r.status_code == 200


def test_supervisor_pages_open_without_key(supervisor_with_key, loopback_client):
    for path in ["/", "/models", "/providers", "/usage", "/logs", "/settings"]:
        r = supervisor_with_key.get(path)
        assert r.status_code == 200, path


def test_supervisor_origin_guard_blocks_cross_origin_mutation(supervisor_with_key, loopback_client):
    r = supervisor_with_key.post(
        "/admin/config/api-key/mock1/reveal",
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_supervisor_origin_guard_covers_server_control(supervisor_with_key, loopback_client):
    r = supervisor_with_key.post(
        "/admin/server/start",
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_supervisor_key_reveal_is_post_only(supervisor_with_key, loopback_client):
    # GET must not return the key: the supervisor falls through to its proxy
    # catch-all (503, worker not running) rather than serving the POST route.
    r = supervisor_with_key.get("/admin/config/api-key/mock1/reveal")
    assert r.status_code in (405, 503)
    assert "api_key" not in r.text


def test_supervisor_csp_headers_present(supervisor_with_key, loopback_client):
    r = supervisor_with_key.get("/")
    assert "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"

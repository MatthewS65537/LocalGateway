from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time

import httpx
import pytest
import uvicorn

from localgateway import config as config_mod
from localgateway import logs as logs_mod
from localgateway import stats as stats_mod
from localgateway import usage as usage_mod
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr
from localgateway.usage import set_db_path
from localgateway.worker import create_app

from mock_provider import create_mock_app


@pytest.fixture(autouse=True)
def _reset_routing_state():
    """Clear round-robin and rate-limit state before every test so module
    singletons don't leak across tests (root cause of test_resolve_alias flakiness).
    Also stop any lingering background writers from prior tests.
    """
    _rr.clear()
    ratelimit._cooldowns.clear()
    ratelimit._permanent.clear()
    # Stop background DB writers that might still be running from a prior test.
    try:
        from localgateway import usage as _u, logs as _l, respcache as _rc
        _u.stop_writer()
        _l.stop_writer()
        _rc.stop_writer()
    except Exception:
        pass
    yield


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread:
    def __init__(self, app, port):
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("mock server failed to start")
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def build_config(p1: int, p2: int) -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": 0, "api_key": None, "routing_mode": "failover", "routing_decay": 0.4, "probe_enabled": False},
        "providers": [
            {
                "id": "mock1", "name": "Mock One",
                "base_url": f"http://127.0.0.1:{p1}/v1",
                "api_key": "x", "timeout": 10, "stream_idle_timeout": 0.5,
            },
            {
                "id": "mock2", "name": "Mock Two",
                "base_url": f"http://127.0.0.1:{p2}/v1",
                "api_key": "x", "timeout": 10,
            },
        ],
        "models": [
            {
                "id": "mock-reasoner", "description": "Reasoning test model",
                "context_length": 32768,
                "aliases": ["reasoner-alias"],
                "default_params": {"temperature": 0.1},
                "display_name": "Mock Reasoner",
                "modality": "text",
                "tags": ["test"],
                "capabilities": {"text": True, "streaming": True},
                "backends": [
                    {"provider": "mock1", "model": "mock-reasoner", "priority": 1,
                     "context_length": 32768, "max_output_tokens": 4096},
                    {"provider": "mock2", "model": "mock-reasoner", "priority": 2,
                     "context_length": 16384, "max_output_tokens": 8192},
                ],
            },
            {
                "id": "mock-limited", "description": "Limit routing test",
                "backends": [
                    {"provider": "mock1", "model": "mock-reasoner", "priority": 1,
                     "max_output_tokens": 100},
                    {"provider": "mock2", "model": "mock-reasoner", "priority": 2,
                     "max_output_tokens": 8000},
                ],
            },
            {
                "id": "mock-failover",
                "backends": [
                    {"provider": "mock1", "model": "mock-dead", "priority": 1},
                    {"provider": "mock2", "model": "mock-reasoner", "priority": 2},
                ],
            },
            {
                "id": "mock-cutmodel",
                "backends": [{"provider": "mock1", "model": "mock-cut", "priority": 1}],
            },
            {
                "id": "mock-stallmodel",
                "backends": [
                    {"provider": "mock1", "model": "mock-stall", "priority": 1},
                    {"provider": "mock2", "model": "mock-reasoner", "priority": 2},
                ],
            },
            {
                "id": "mock-429model",
                "backends": [
                    {"provider": "mock1", "model": "mock-429", "priority": 1},
                    {"provider": "mock2", "model": "mock-reasoner", "priority": 2},
                ],
            },
            {
                "id": "mock-disabled", "enabled": False,
                "backends": [{"provider": "mock1", "model": "mock-reasoner", "priority": 1}],
            },
        ],
        "pricing": {"mock1:mock-reasoner": {"input": 0.000001, "output": 0.000002}},
    }


@pytest.fixture(scope="session")
def mock_servers():
    p1, p2 = _free_port(), _free_port()
    app1, app2 = create_mock_app("one"), create_mock_app("two")
    s1, s2 = _ServerThread(app1, p1), _ServerThread(app2, p2)
    s1.start()
    s2.start()
    yield {"p1": p1, "p2": p2, "app1": app1, "app2": app2}
    s1.stop()
    s2.stop()


@pytest.fixture()
def gateway_app(mock_servers, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(build_config(mock_servers["p1"], mock_servers["p2"])))
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
    # Tests need synchronous writes so db_rows sees rows immediately after a
    # request. Stop the background writer (started by set_db_path) to force
    # the sync fallback path. Production uses the background writer.
    usage_mod.stop_writer()
    logs_mod.stop_writer()
    return app, db_path


@pytest.fixture()
async def client(gateway_app):
    app, _ = gateway_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


@pytest.fixture()
def db_rows(gateway_app):
    _, db_path = gateway_app

    def _rows(model: str | None = None, table: str = "usage") -> list[dict]:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        q = f"SELECT * FROM {table}"
        params: list = []
        if model:
            q += " WHERE logical_model = ?" if table == "usage" else " WHERE model = ?"
            params.append(model)
        q += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
        conn.close()
        return rows

    return _rows

"""Regression tests for the redacted-placeholder API key bug.

GET /admin/config masks api keys with a bullet sentinel. A config write path
that persists that sentinel verbatim permanently clobbers the real key, and
httpx then crashes with a cryptic UnicodeEncodeError ('ascii' codec can't
encode ...) on every request through that provider. These tests pin the
guards: the sentinel can never be saved, and a corrupted key produces an
actionable error instead of a codec crash.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from localgateway import config as config_mod
from localgateway.config import ProviderConfig, validated_api_key
from localgateway.provider import call_provider
from localgateway.worker import create_app

from conftest import build_config  # noqa: F401  (fixtures import path)

REDACTED = "\u2022\u2022\u2022\u2022\u2022\u2022"


@pytest.fixture()
def worker(mock_servers, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(build_config(mock_servers["p1"], mock_servers["p2"])))

    config_mod._config = None
    config_mod._config_mtime = -1.0

    app = create_app(str(cfg_path))
    with TestClient(app) as c:
        yield c, cfg_path


def _on_disk(cfg_path) -> dict:
    return json.loads(cfg_path.read_text())


def test_get_config_masks_provider_keys(worker):
    client, _ = worker
    r = client.get("/admin/config")
    assert r.status_code == 200
    for p in r.json()["providers"]:
        assert p["api_key"] == REDACTED


def test_put_config_roundtrip_preserves_real_keys(worker):
    client, cfg_path = worker
    body = client.get("/admin/config").json()
    r = client.put("/admin/config", json=body)
    assert r.status_code == 200
    on_disk = _on_disk(cfg_path)
    assert on_disk["providers"][0]["api_key"] == "x"
    assert on_disk["providers"][1]["api_key"] == "x"


def test_put_config_rejects_sentinel_for_unknown_provider(worker):
    client, cfg_path = worker
    before = cfg_path.read_text()
    body = client.get("/admin/config").json()
    body["providers"].append({
        "id": "brand-new",
        "name": "New",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": REDACTED,  # sentinel under an id with no on-disk key to restore
        "headers": {},
        "timeout": 10,
    })
    r = client.put("/admin/config", json=body)
    assert r.status_code == 422
    assert "redacted placeholder" in r.json()["error"]
    assert cfg_path.read_text() == before  # on-disk config untouched


def test_set_provider_api_key_rejects_sentinel(worker):
    client, cfg_path = worker
    r = client.post("/admin/config/api-key", json={"provider": "mock1", "api_key": REDACTED})
    assert r.status_code == 422
    assert "redacted placeholder" in r.json()["error"]
    assert _on_disk(cfg_path)["providers"][0]["api_key"] == "x"


def test_discover_with_corrupted_key_returns_actionable_error(worker):
    client, cfg_path = worker
    # Simulate the historical corruption: sentinel persisted as the real key.
    cfg = _on_disk(cfg_path)
    cfg["providers"][0]["api_key"] = REDACTED
    cfg_path.write_text(json.dumps(cfg))
    config_mod._config = None
    config_mod._config_mtime = -1.0

    r = client.get("/admin/providers/mock1/models")
    assert r.status_code == 400
    err = r.json()["error"]
    assert "mock1" in err
    assert "Re-enter" in err
    assert "ascii" not in err  # never leak the raw codec error


def test_validated_api_key_unit():
    ok = ProviderConfig(id="p1", base_url="http://x", api_key="sk-real")
    assert validated_api_key(ok) == "sk-real"
    empty = ProviderConfig(id="p2", base_url="http://x", api_key="")
    assert validated_api_key(empty) == ""
    bad = ProviderConfig(id="p3", base_url="http://x", api_key=REDACTED)
    with pytest.raises(ValueError, match="p3"):
        validated_api_key(bad)


def test_call_provider_with_corrupted_key_fails_cleanly():
    bad = ProviderConfig(id="badprov", base_url="http://127.0.0.1:9/v1", api_key=REDACTED)

    async def run():
        async with httpx.AsyncClient() as client:
            return await call_provider(client, bad, "m", {"messages": []})

    result = asyncio.run(run())
    assert result.success is False
    assert "badprov" in result.error
    assert "Re-enter" in result.error
    assert "ascii" not in result.error

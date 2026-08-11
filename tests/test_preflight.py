"""Phase C — N1 config preflight validation tests."""
from __future__ import annotations

import json

from localgateway.config import GatewayConfig, validate_config, REDACTED_KEY


def _cfg(**overrides) -> GatewayConfig:
    base = {
        "server": {"api_key": None},
        "providers": [
            {"id": "p1", "name": "P1", "base_url": "http://x/v1", "api_key": "real-key", "timeout": 10},
        ],
        "models": [
            {"id": "m1", "backends": [
                {"provider": "p1", "model": "m1-up", "priority": 1, "context_length": 32768}],
             "context_length": 32768, "max_output_tokens": 4096},
        ],
        "pricing": {"p1:m1-up": {"input": 0.001, "output": 0.002}},
    }
    base.update(overrides)
    return GatewayConfig.model_validate(base)


def test_clean_config_no_issues():
    cfg = _cfg()
    issues = validate_config(cfg)
    assert issues == []


def test_duplicate_provider_id():
    cfg = _cfg(providers=[
        {"id": "p1", "name": "A", "base_url": "http://a/v1", "api_key": "x", "timeout": 5},
        {"id": "p1", "name": "B", "base_url": "http://b/v1", "api_key": "x", "timeout": 5},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "duplicate_provider_id"]
    assert len(errs) == 1
    assert errs[0]["severity"] == "error"


def test_duplicate_model_id():
    cfg = _cfg(models=[
        {"id": "m1", "backends": [{"provider": "p1", "model": "m1-up", "priority": 1}]},
        {"id": "m1", "backends": [{"provider": "p1", "model": "m1-up", "priority": 1}]},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "duplicate_model_id"]
    assert len(errs) == 1


def test_alias_collision():
    cfg = _cfg(models=[
        {"id": "m1", "aliases": ["alias1"],
         "backends": [{"provider": "p1", "model": "m1-up", "priority": 1}]},
        {"id": "m2", "aliases": ["alias1"],
         "backends": [{"provider": "p1", "model": "m2-up", "priority": 1}]},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "alias_collision"]
    assert len(errs) == 1


def test_orphan_backend():
    cfg = _cfg(models=[
        {"id": "m1", "backends": [
            {"provider": "p1", "model": "m1-up", "priority": 1},
            {"provider": "ghost", "model": "ghost-up", "priority": 2}],
         "context_length": 32768},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "orphan_backend"]
    assert len(errs) == 1
    assert "ghost" in errs[0]["message"]


def test_placeholder_key():
    cfg = _cfg(providers=[
        {"id": "p1", "name": "P1", "base_url": "http://x/v1",
         "api_key": REDACTED_KEY, "timeout": 5},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "placeholder_key"]
    assert len(errs) == 1


def test_non_ascii_key():
    cfg = _cfg(providers=[
        {"id": "p1", "name": "P1", "base_url": "http://x/v1",
         "api_key": "key-with-\u00e9", "timeout": 5},
    ])
    issues = validate_config(cfg)
    errs = [i for i in issues if i["code"] == "non_ascii_key"]
    assert len(errs) == 1


def test_pricing_gap_warning():
    cfg = _cfg(pricing={})
    issues = validate_config(cfg)
    warns = [i for i in issues if i["code"] == "pricing_gap"]
    assert len(warns) == 1
    assert warns[0]["severity"] == "warning"


def test_model_no_backends_warning():
    cfg = _cfg(models=[
        {"id": "m1", "backends": [], "context_length": 32768},
    ])
    issues = validate_config(cfg)
    warns = [i for i in issues if i["code"] == "model_no_backends"]
    assert len(warns) == 1


def test_disabled_provider_referenced_warning():
    cfg = _cfg(providers=[
        {"id": "p1", "name": "P1", "base_url": "http://x/v1", "api_key": "k",
         "timeout": 5, "enabled": False},
    ], models=[
        {"id": "m1", "backends": [
            {"provider": "p1", "model": "m1-up", "priority": 1, "enabled": True}],
         "context_length": 32768},
    ])
    issues = validate_config(cfg)
    warns = [i for i in issues if i["code"] == "disabled_provider_referenced"]
    assert len(warns) == 1


def test_mot_exceeds_context_warning():
    cfg = _cfg(models=[
        {"id": "m1", "max_output_tokens": 999999,
         "backends": [{"provider": "p1", "model": "m1-up", "priority": 1,
                       "context_length": 32768}],
         "context_length": 32768},
    ])
    issues = validate_config(cfg)
    warns = [i for i in issues if i["code"] == "mot_exceeds_context"]
    assert len(warns) == 1


async def test_validate_endpoint(client):
    """POST /admin/config/validate returns issues without writing."""
    # Get current config
    r = await client.get("/admin/config")
    cfg = r.json()
    # Introduce a duplicate provider id
    if cfg["providers"]:
        cfg["providers"].append({
            "id": cfg["providers"][0]["id"],
            "name": "Dup", "base_url": "http://x/v1", "api_key": "x", "timeout": 5,
        })
    r = await client.post("/admin/config/validate", json=cfg)
    assert r.status_code == 200
    issues = r.json()["issues"]
    assert any(i["code"] == "duplicate_provider_id" for i in issues)

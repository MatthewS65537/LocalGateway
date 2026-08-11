"""Context-capacity routing: a backend whose context window can't hold
input + requested completion is infeasible — cache warmth never wins over
context capacity ("route longer contexts to longer providers, even if it
means cold cache").
"""
from __future__ import annotations

from localgateway import stats as stats_mod
from localgateway.config import BackendConfig, GatewayConfig, ModelConfig, ProviderConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, select_backends
from localgateway.streaming import (
    _context_too_small_error,
    _requested_max_tokens,
    _resolve_backends,
)


def _config(short_ctx: int = 8_192, long_ctx: int = 131_072) -> GatewayConfig:
    """Two tier-1 providers: 'short' (small ctx) and 'long' (big ctx)."""
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(id="short", base_url="http://short"),
            ProviderConfig(id="long", base_url="http://long"),
        ],
        models=[
            ModelConfig(
                id="m",
                backends=[
                    BackendConfig(provider="short", model="m1", priority=1,
                                  context_length=short_ctx),
                    BackendConfig(provider="long", model="m1", priority=1,
                                  context_length=long_ctx),
                ],
            )
        ],
    )
    cfg.server.routing_mode = "failover"
    cfg.server.cache_affinity_enabled = True
    cfg.server.cache_affinity_ttl_sec = 300
    return cfg


def _warm(provider: str, cache_key: str):
    stats_mod.record_cache_activity(
        provider_id=provider, backend_model="m1", cache_key=cache_key,
        cached_tokens=0, cache_write_tokens=50, input_tokens=0,
        cache_supported=True,
    )


def _ids(it):
    return [s.provider.id for s in it]


def setup_function(_):
    _rr.clear()
    ratelimit._cooldowns.clear()
    stats_mod.reset()


def test_warm_short_backend_excluded_by_context_cold_long_wins():
    """Warm backend with insufficient context is not emitted at all — the
    cold long-context provider is used instead, even though it means a
    cold cache."""
    cfg = _config(short_ctx=8_192, long_ctx=131_072)
    ck = "bigprompt"
    _warm("short", ck)
    order = _ids(select_backends(cfg, "m", input_tokens=10_000, cache_key=ck))
    assert "short" not in order
    assert order == ["long"]


def test_output_budget_counts_toward_context_fit():
    """input + max_tokens must fit inside context_length."""
    cfg = _config(short_ctx=10_000, long_ctx=131_072)
    # 9k input alone fits the 10k backend...
    order = _ids(select_backends(cfg, "m", input_tokens=9_000, max_tokens=None))
    assert "short" in order
    # ...but 9k input + 2k requested completion does not.
    order = _ids(select_backends(cfg, "m", input_tokens=9_000, max_tokens=2_000))
    assert "short" not in order
    assert order == ["long"]


def test_max_output_tokens_parsed_for_responses_api():
    """_requested_max_tokens understands the Responses API field so the
    output budget reaches the router on that path."""
    assert _requested_max_tokens({"max_output_tokens": 2048}) == 2048
    assert _requested_max_tokens({"max_tokens": 512}) == 512
    assert _requested_max_tokens({}) is None


def test_context_only_failure_reports_context_length_exceeded():
    """When every backend was filtered for context (not missing config),
    the error names the cause instead of a misleading 404."""
    cfg = _config(short_ctx=8_192, long_ctx=16_384)
    backends, err, status = _resolve_backends(
        cfg, "m", max_tokens=2_000, input_tokens=20_000
    )
    assert backends == []
    assert status == 400
    assert err and err["code"] == "context_length_exceeded"
    assert "22000" in err["message"]


def test_context_too_small_error_none_when_a_backend_fits():
    cfg = _config(short_ctx=8_192, long_ctx=131_072)
    assert _context_too_small_error(cfg, cfg.models[0], 10_000, None) is None
    # No estimates supplied -> no accusation.
    assert _context_too_small_error(cfg, cfg.models[0], None, None) is None

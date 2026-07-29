from localgateway.config import BackendConfig, GatewayConfig, ModelConfig, ProviderConfig
from localgateway.streaming import (
    _requested_max_tokens,
    _resolve_backends,
    apply_default_params,
    compute_tps,
)


def test_compute_tps_stream_excludes_ttft():
    # 100 tokens over 2000ms total with 500ms TTFT -> 1500ms decode -> 66.67 tps
    assert compute_tps(100, 2000, 500) == 66.67


def test_compute_tps_non_stream_uses_full_latency():
    assert compute_tps(100, 2000, None) == 50.0


def test_compute_tps_guards():
    assert compute_tps(None, 2000, 500) is None
    assert compute_tps(100, None, 500) is None
    assert compute_tps(0, 2000, 500) is None
    # ttft >= latency falls back to full latency
    assert compute_tps(100, 1000, 1500) == 100.0


def test_requested_max_tokens_variants():
    assert _requested_max_tokens({"max_tokens": 500}) == 500
    assert _requested_max_tokens({"max_completion_tokens": 700}) == 700
    assert _requested_max_tokens({"max_completion_tokens": 700, "max_tokens": 500}) == 700
    assert _requested_max_tokens({}) is None
    assert _requested_max_tokens({"max_tokens": "junk"}) is None


def _config():
    return GatewayConfig(
        providers=[
            ProviderConfig(id="a", base_url="http://a"),
            ProviderConfig(id="b", base_url="http://b"),
        ],
        models=[
            ModelConfig(
                id="m",
                aliases=["m-alias", "gpt-m"],
                default_params={"temperature": 0.2, "max_tokens": 100},
                backends=[
                    BackendConfig(provider="a", model="m1", priority=1, max_output_tokens=2048),
                    BackendConfig(provider="b", model="m1", priority=2, max_output_tokens=8192),
                ],
            ),
            ModelConfig(id="off", enabled=False, aliases=["off-alias"], backends=[
                BackendConfig(provider="a", model="m1", priority=1),
            ]),
        ],
    )


def test_resolve_unknown_model_404():
    backends, err, status = _resolve_backends(_config(), "nope", None)
    assert backends == [] and status == 404 and err["code"] == "model_not_found"


def test_resolve_disabled_model_404():
    backends, err, status = _resolve_backends(_config(), "off", None)
    assert status == 404


def test_resolve_alias():
    cfg = _config()
    by_id, err1, s1 = _resolve_backends(cfg, "m", None)
    by_alias, err2, s2 = _resolve_backends(cfg, "m-alias", None)
    assert err1 is None and err2 is None
    assert [b.provider.id for b in by_id] == [b.provider.id for b in by_alias]


def test_resolve_disabled_alias_404():
    backends, err, status = _resolve_backends(_config(), "off-alias", None)
    assert status == 404


def test_model_by_id_alias():
    cfg = _config()
    assert cfg.model_by_id("gpt-m").id == "m"
    assert cfg.model_by_id("m").id == "m"


def test_apply_default_params_fills_missing():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_default_params(body, {"temperature": 0.2, "max_tokens": 100, "model": "hack"})
    assert out["temperature"] == 0.2
    assert out["max_tokens"] == 100
    assert out["model"] == "m"  # blocked from defaults


def test_apply_default_params_client_wins():
    body = {"model": "m", "temperature": 0.9, "messages": []}
    out = apply_default_params(body, {"temperature": 0.2, "top_p": 0.95})
    assert out["temperature"] == 0.9
    assert out["top_p"] == 0.95


def test_apply_default_params_ignores_unknown_keys():
    body = {"model": "m"}
    out = apply_default_params(body, {"temperature": 0.1, "evil_key": 1, "messages": []})
    assert "evil_key" not in out
    assert out.get("messages") != []


def test_resolve_no_fit_400_with_max_available():
    backends, err, status = _resolve_backends(_config(), "m", 99999)
    assert backends == [] and status == 400
    assert err["code"] == "output_limit_exceeded"
    assert "8192" in err["message"]


def test_resolve_partial_fit_routes_to_capable():
    backends, err, status = _resolve_backends(_config(), "m", 4096)
    assert err is None
    assert [b.provider.id for b in backends] == ["b"]


def test_resolve_unconstrained_returns_all():
    backends, err, status = _resolve_backends(_config(), "m", None)
    assert err is None
    assert sorted(b.provider.id for b in backends) == ["a", "b"]

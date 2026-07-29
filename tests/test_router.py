from localgateway.config import BackendConfig, GatewayConfig, ModelConfig, ProviderConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, select_backends


def _config(**backend_overrides):
    defaults = dict(provider="a", model="m1", priority=1)
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(id="a", base_url="http://a"),
            ProviderConfig(id="b", base_url="http://b"),
            ProviderConfig(id="c", base_url="http://c"),
        ],
        models=[
            ModelConfig(
                id="m",
                backends=[
                    BackendConfig(provider="a", model="m1", priority=1),
                    BackendConfig(provider="b", model="m1", priority=1),
                    BackendConfig(provider="c", model="m1", priority=2),
                ],
            )
        ],
    )
    cfg.server.routing_mode = "failover"
    return cfg


def _ids(it):
    return [s.provider.id for s in it]


def setup_function(_):
    _rr.clear()
    ratelimit._cooldowns.clear()


def test_tier_round_robin_rotates_within_tier():
    cfg = _config()
    orders = [_ids(select_backends(cfg, "m")) for _ in range(4)]
    assert orders[0][:2] != orders[1][:2]
    assert all(set(o[:2]) == {"a", "b"} for o in orders)
    assert all(o[-1] == "c" for o in orders)


def test_failover_tier_order():
    cfg = _config()
    order = _ids(select_backends(cfg, "m"))
    assert order[-1] == "c"


def test_disabled_backend_skipped():
    cfg = _config()
    cfg.models[0].backends[0].enabled = False
    assert "a" not in _ids(select_backends(cfg, "m"))


def test_disabled_provider_skipped():
    cfg = _config()
    cfg.providers[1].enabled = False
    assert "b" not in _ids(select_backends(cfg, "m"))


def test_disabled_model_yields_nothing():
    cfg = _config()
    cfg.models[0].enabled = False
    assert list(select_backends(cfg, "m")) == []


def test_unknown_model_yields_nothing():
    assert list(select_backends(_config(), "nope")) == []


def test_rate_limited_deferred_to_end():
    cfg = _config()
    ratelimit.set_cooldown("a", "m1", 60)
    ratelimit.set_cooldown("b", "m1", 60)
    order = _ids(select_backends(cfg, "m"))
    assert order[0] == "c"
    assert set(order[1:]) == {"a", "b"}


def test_max_tokens_excludes_small_limit_backends():
    cfg = _config()
    cfg.models[0].backends[0].max_output_tokens = 2048
    cfg.models[0].backends[1].max_output_tokens = 8192
    assert _ids(select_backends(cfg, "m", max_tokens=1000)) == ["a", "b", "c"]
    assert _ids(select_backends(cfg, "m", max_tokens=4096)) == ["b", "c"]
    assert _ids(select_backends(cfg, "m", max_tokens=99999)) == ["c"]


def test_max_tokens_none_is_unconstrained():
    cfg = _config()
    cfg.models[0].backends[0].max_output_tokens = 10
    assert "a" in _ids(select_backends(cfg, "m"))


def test_explore_mode_samples_tiers_with_decay():
    import random

    cfg = _config()
    cfg.server.routing_mode = "explore"
    cfg.server.routing_decay = 0.4

    rng = random.Random(42)
    firsts = []
    for _ in range(200):
        _rr.clear()
        order = _ids(select_backends(cfg, "m", rng=rng))
        firsts.append(order[0])

    tier1_starts = sum(1 for f in firsts if f in ("a", "b"))
    tier2_starts = sum(1 for f in firsts if f == "c")

    assert tier1_starts > tier2_starts
    assert tier2_starts > 0


def test_explore_mode_all_tiers_reachable():
    import random

    cfg = _config()
    cfg.server.routing_mode = "explore"
    cfg.server.routing_decay = 0.5

    rng = random.Random(123)
    seen = set()
    for _ in range(100):
        _rr.clear()
        order = _ids(select_backends(cfg, "m", rng=rng))
        seen.update(order)

    assert "a" in seen
    assert "b" in seen
    assert "c" in seen


def test_failover_mode_preserves_tier_order():
    cfg = _config()
    cfg.server.routing_mode = "failover"
    cfg.server.routing_decay = 0.4

    for _ in range(10):
        _rr.clear()
        order = _ids(select_backends(cfg, "m"))
        assert order[-1] == "c"
        assert set(order[:2]) == {"a", "b"}


def test_explore_mode_with_single_tier_acts_like_failover():
    cfg = GatewayConfig(
        providers=[
            ProviderConfig(id="a", base_url="http://a"),
        ],
        models=[
            ModelConfig(
                id="m",
                backends=[
                    BackendConfig(provider="a", model="m1", priority=1),
                ],
            )
        ],
    )
    cfg.server.routing_mode = "explore"

    order = _ids(select_backends(cfg, "m"))
    assert order == ["a"]


def test_provider_prefs_ignore():
    from localgateway.router import ProviderPrefs
    cfg = _config()
    prefs = ProviderPrefs(ignore=["a"])
    assert "a" not in _ids(select_backends(cfg, "m", prefs=prefs))


def test_provider_prefs_order():
    from localgateway.router import ProviderPrefs
    cfg = _config()
    prefs = ProviderPrefs(order=["c", "a"])
    order = _ids(select_backends(cfg, "m", prefs=prefs))
    assert order[0] == "c"
    assert "a" in order


def test_provider_prefs_no_fallbacks():
    from localgateway.router import ProviderPrefs
    cfg = _config()
    prefs = ProviderPrefs(order=["c"], allow_fallbacks=False)
    assert _ids(select_backends(cfg, "m", prefs=prefs)) == ["c"]


def test_provider_prefs_from_request():
    from localgateway.router import ProviderPrefs
    prefs = ProviderPrefs.from_request({
        "provider": {"order": ["a"], "ignore": ["b"], "allow_fallbacks": False, "sort": "latency"}
    })
    assert prefs.order == ["a"]
    assert prefs.ignore == ["b"]
    assert prefs.allow_fallbacks is False
    assert prefs.sort == "latency"
    assert ProviderPrefs.from_request({}) is None

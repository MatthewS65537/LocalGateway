"""Tests for cache-affinity routing (strict sticky + in-flight avoidance)."""
from __future__ import annotations

import time

from localgateway.config import BackendConfig, GatewayConfig, ModelConfig, ProviderConfig
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, select_backends, ProviderPrefs
from localgateway import stats as stats_mod
from localgateway.cache import fingerprint


def _config(cache_supported: dict[str, bool | None] | None = None, **server_overrides):
    """3 providers: a,b tier 1, c tier 2."""
    cs = cache_supported or {}
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
                    BackendConfig(provider="a", model="m1", priority=1,
                                  cache_supported=cs.get("a")),
                    BackendConfig(provider="b", model="m1", priority=1,
                                  cache_supported=cs.get("b")),
                    BackendConfig(provider="c", model="m1", priority=2,
                                  cache_supported=cs.get("c")),
                ],
            )
        ],
    )
    cfg.server.routing_mode = "failover"
    cfg.server.cache_affinity_enabled = True
    cfg.server.cache_affinity_ttl_sec = 300
    for k, v in server_overrides.items():
        setattr(cfg.server, k, v)
    return cfg


def _ids(it):
    return [s.provider.id for s in it]


def setup_function(_):
    _rr.clear()
    ratelimit._cooldowns.clear()
    stats_mod.reset()


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_stable_for_same_prefix():
    body = {"messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "What now?"},
    ]}
    fp1 = fingerprint(body)
    fp2 = fingerprint(body)
    assert fp1 == fp2
    assert len(fp1) == 16


def test_fingerprint_excludes_final_user_turn():
    """Same prefix, different final user message -> same fingerprint."""
    base = {"messages": [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
    ]}
    b1 = dict(base)
    b1["messages"] = base["messages"] + [{"role": "user", "content": "AAA"}]
    b2 = dict(base)
    b2["messages"] = base["messages"] + [{"role": "user", "content": "BBB"}]
    assert fingerprint(b1) == fingerprint(b2)


def test_fingerprint_changes_when_prefix_changes():
    b1 = {"messages": [{"role": "system", "content": "S1"}, {"role": "user", "content": "x"}]}
    b2 = {"messages": [{"role": "system", "content": "S2"}, {"role": "user", "content": "x"}]}
    assert fingerprint(b1) != fingerprint(b2)


def test_fingerprint_single_turn_uses_full_prompt():
    b1 = {"messages": [{"role": "user", "content": "hi"}]}
    b2 = {"messages": [{"role": "user", "content": "bye"}]}
    assert fingerprint(b1) != fingerprint(b2)
    assert fingerprint(b1) is not None


def test_fingerprint_none_on_empty():
    assert fingerprint({}) is None
    assert fingerprint({"messages": []}) is None
    assert fingerprint({"messages": [{"role": "user", "content": ""}]}) is None


def test_fingerprint_no_collision_beyond_truncation():
    """Two distinct prompts sharing a >2000-char prefix must not collide.

    The prefix is truncated to keep the hash input bounded; a length marker
    disambiguates truncation so two different long prompts can never warm
    each other's cache slot.
    """
    base = "A" * 2100
    b1 = {"messages": [{"role": "user", "content": base + "X"}]}
    b2 = {"messages": [{"role": "user", "content": base + "Y"}]}
    # Differ only past the truncation point -> must differ.
    assert fingerprint(b1) != fingerprint(b2)
    # A prompt exactly at the cap still differs from one that is longer.
    at_cap = {"messages": [{"role": "user", "content": "A" * 2000}]}
    over_cap = {"messages": [{"role": "user", "content": "A" * 2001}]}
    assert fingerprint(at_cap) != fingerprint(over_cap)


# ---------------------------------------------------------------- stickiness


def _warm(backend: str, cache_key: str, *, hit: bool = False, write: bool = False,
          miss: bool = False):
    """Register warmth for a backend via record_cache_activity."""
    # Each provider id "a"/"b"/"c" maps to backend model "m1".
    cached = 100 if hit else 0
    cw = 50 if write else 0
    inp = 200 if miss else (100 if not (hit or write) else 0)
    stats_mod.record_cache_activity(
        provider_id=backend, backend_model="m1", cache_key=cache_key,
        cached_tokens=cached, cache_write_tokens=cw, input_tokens=inp,
        cache_supported=True,
    )


def test_cold_key_uses_normal_round_robin():
    """Unknown cache_key -> normal tiered round-robin (no regression)."""
    cfg = _config()
    # No warmth registered.
    orders = [_ids(select_backends(cfg, "m", cache_key="newkey")) for _ in range(4)]
    # Tier 1 first (a,b in rotation), then c.
    assert all(o[:2] in (["a", "b"], ["b", "a"]) for o in orders)
    assert all(o[2] == "c" for o in orders)
    # Rotation actually happens across calls.
    assert orders[0][:2] != orders[1][:2]


def test_warm_backend_chosen_first_regardless_of_tier():
    """Warm tier-2 backend beats cold tier-1 backends (cache precedence)."""
    cfg = _config()
    ck = "prefix1"
    _warm("c", ck, write=True)  # c (tier 2) becomes warm
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] == "c"


def test_warm_busy_beats_cold_idle():
    """A busy warm backend is preferred over an idle cold backend."""
    cfg = _config()
    ck = "prefix1"
    _warm("c", ck, write=True)
    # Mark c as busy (in-flight).
    stats_mod.start_request("c", "m1")
    stats_mod.start_request("c", "m1")
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] == "c"


def test_warm_idle_beats_warm_busy():
    """Among warm backends, idle ones come first."""
    cfg = _config()
    ck = "prefix1"
    _warm("a", ck, write=True)
    _warm("c", ck, write=True)
    stats_mod.start_request("c", "m1")
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    # a is warm+idle, c is warm+busy -> a first.
    assert order[0] == "a"
    assert "c" in order[:2]


def test_cold_idle_beats_cold_busy():
    """Among cold backends, idle before busy."""
    cfg = _config()
    ck = "cold1"
    stats_mod.start_request("a", "m1")
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    # a is cold+busy, b is cold+idle -> b before a (both tier 1).
    assert order.index("b") < order.index("a")


def test_max_inflight_before_spill_to_cold_idle():
    """When warm-busy exceeds max_inflight, it spills into the cold-busy band
    (it's busy, so it follows genuinely cold idle backends)."""
    cfg = _config(max_inflight_before_spill=1)
    ck = "prefix1"
    _warm("a", ck, write=True)
    stats_mod.start_request("a", "m1")
    stats_mod.start_request("a", "m1")  # in_flight=2 > 1 -> spill
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    # a spills out of warm-busy; b (tier1, cold, idle) comes first.
    assert order[0] == "b"
    # a still appears (after b), within cold-busy.
    assert "a" in order


def test_ttl_expiry_reverts_to_cold():
    """After TTL, warm entry expires -> cold routing resumes."""
    cfg = _config(cache_affinity_ttl_sec=1)
    ck = "prefix1"
    _warm("c", ck, write=True)
    # First call: c warm -> first.
    assert _ids(select_backends(cfg, "m", cache_key=ck))[0] == "c"
    # Fast-forward beyond TTL by manipulating last_seen_ts.
    with stats_mod._lock:
        for e in stats_mod._warmth[ck].values():
            e.last_seen_ts = time.time() - 5
    # After expiry: cold round-robin -> tier1 a/b first, c last.
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] in ("a", "b")
    assert order[-1] == "c"


# ---------------------------------------------------------------- failure / rate-limit


def test_warm_backend_rate_limited_defers():
    """A rate-limited warm backend is deferred (last band)."""
    cfg = _config()
    ck = "prefix1"
    _warm("a", ck, write=True)
    ratelimit.set_cooldown("a", "m1", 60)
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    # a is warm but ratelimited -> not first; b (cold, tier1) first.
    assert order[0] == "b"
    assert order[-1] == "a"


def test_warm_backend_failure_falls_to_next():
    """If the only warm backend fails (simulated by absence), fall to cold."""
    cfg = _config()
    ck = "prefix1"
    _warm("a", ck, write=True)
    # First selection: a warm first.
    order1 = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order1[0] == "a"
    # Simulate a failing by marking it ratelimited; next warm/cold used.
    ratelimit.set_cooldown("a", "m1", 60)
    order2 = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order2[0] == "b"
    assert order2[-1] == "a"


# ---------------------------------------------------------------- cache_supported gating


def test_cache_supported_false_never_warm():
    """A backend with cache_supported=False never gets warmth entries."""
    cfg = _config(cache_supported={"a": False, "b": False, "c": False})
    ck = "prefix1"
    stats_mod.record_cache_activity(
        provider_id="a", backend_model="m1", cache_key=ck,
        cached_tokens=100, cache_write_tokens=0, input_tokens=100,
        cache_supported=False,
    )
    # No warmth -> cold round-robin.
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] in ("a", "b")


def test_non_reporting_provider_success_registers_warmth():
    """A provider that returns no cached_tokens still becomes warm on success
    when cache_supported is True/None (handles non-reporting providers)."""
    cfg = _config(cache_supported={"a": True})
    ck = "prefix1"
    # Simulate a successful response with NO cache tokens reported.
    stats_mod.record_cache_activity(
        provider_id="a", backend_model="m1", cache_key=ck,
        cached_tokens=None, cache_write_tokens=None, input_tokens=100,
        cache_supported=True,
    )
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] == "a"


def test_cache_write_tokens_register_warmth():
    """cache_write_tokens>0 (cache just populated) registers warmth."""
    cfg = _config()
    ck = "prefix1"
    _warm("b", ck, write=True)
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    assert order[0] == "b"


# ---------------------------------------------------------------- provider.order + sort


def test_order_is_only_tiebreaker_cache_wins():
    """provider.order does not override cache affinity; it only ties-breaks."""
    cfg = _config()
    ck = "prefix1"
    _warm("b", ck, write=True)
    prefs = ProviderPrefs(order=["a", "b", "c"], allow_fallbacks=False)
    order = _ids(select_backends(cfg, "m", prefs=prefs, cache_key=ck))
    # b is warm -> first despite order listing a first.
    assert order[0] == "b"


def test_sort_cache_orders_by_hit_rate():
    """sort:cache orders backends by warmth/hit-rate without full stickiness."""
    cfg = _config(cache_affinity_enabled=False)  # sort only, no stickiness
    ck = "prefix1"
    # a has higher hit-rate than b.
    _warm("a", ck, hit=True)
    _warm("a", ck, hit=True)
    _warm("b", ck, miss=True)
    prefs = ProviderPrefs(sort="cache")
    order = _ids(select_backends(cfg, "m", prefs=prefs, cache_key=ck))
    # a (warm, hit_rate~0.67) before b (warm, hit_rate 0).
    assert order.index("a") < order.index("b")


# ---------------------------------------------------------------- feature-off


def test_feature_off_uses_legacy_path():
    """When cache_affinity_enabled is False, behavior is unchanged."""
    cfg = _config(cache_affinity_enabled=False)
    ck = "prefix1"
    _warm("c", ck, write=True)  # would be sticky if enabled
    order = _ids(select_backends(cfg, "m", cache_key=ck))
    # Legacy: tier1 first (a,b), then c. c is NOT promoted.
    assert order[0] in ("a", "b")
    assert order[2] == "c"


def test_no_cache_key_no_affinity():
    """cache_key=None -> normal routing even when affinity enabled."""
    cfg = _config()
    _warm("c", "otherkey", write=True)
    order = _ids(select_backends(cfg, "m", cache_key=None))
    assert order[0] in ("a", "b")
    assert order[2] == "c"

# ---------------------------------------------------------------- warmth registry API


async def test_warmth_endpoint_lists_and_clears(client):
    """GET /admin/warmth exposes the registry; DELETE clears it."""
    stats_mod.record_cache_activity(
        provider_id="mock1", backend_model="mock-reasoner", cache_key="fp1",
        cached_tokens=100, cache_write_tokens=0, input_tokens=50,
        cache_supported=True,
    )
    r = await client.get("/admin/warmth")
    assert r.status_code == 200
    data = r.json()
    assert "fp1" in data
    assert "mock1:mock-reasoner" in data["fp1"]
    assert data["fp1"]["mock1:mock-reasoner"]["hit_count"] == 1

    r2 = await client.delete("/admin/warmth")
    assert r2.status_code == 200
    r3 = await client.get("/admin/warmth")
    assert r3.status_code == 200
    assert r3.json() == {}


async def test_backend_cache_supported_update_via_api(client):
    """PUT /admin/models/{id}/backends/{index} accepts cache_supported."""
    r = await client.put("/admin/models/mock-reasoner/backends/0", json={
        "cache_supported": True,
    })
    assert r.status_code == 200
    r2 = await client.get("/admin/models/mock-reasoner")
    assert r2.status_code == 200
    assert r2.json()["backends"][0]["cache_supported"] is True
    # Unknown fields still rejected.
    r3 = await client.put("/admin/models/mock-reasoner/backends/0", json={
        "bogus_field": 1,
    })
    assert r3.status_code == 422

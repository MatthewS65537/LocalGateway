"""Tests for time-based routing (per-model, OFF by default)."""
from __future__ import annotations

import datetime as _dt

from localgateway.config import (
    BackendConfig,
    GatewayConfig,
    ModelConfig,
    ProviderConfig,
    TimeSlot,
    TimeRoutingConfig,
)
from localgateway.ratelimit import ratelimit
from localgateway.router import _rr, select_backends
from localgateway.time_routing import (
    current_time_in_tz,
    filter_backends_for_slot,
    get_active_slot,
    slot_matches,
)


def _config(**model_overrides):
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
    for k, v in model_overrides.items():
        setattr(cfg.models[0], k, v)
    return cfg


def _ids(it):
    return [s.provider.id for s in it]


def _slot(**kw):
    defaults = dict(name="peak", start_hour=9, end_hour=17)
    defaults.update(kw)
    return TimeSlot(**defaults)


def _now(hour=12, weekday=0):  # weekday 0 = Monday
    return _dt.datetime(2026, 1, 5, hour, 0, 0, tzinfo=_dt.timezone.utc) + _dt.timedelta(days=weekday)


def setup_function(_):
    _rr.clear()
    ratelimit._cooldowns.clear()


# ---------------------------------------------------------------- defaults


def test_time_routing_defaults_off():
    """A fresh ModelConfig has time_routing disabled by default."""
    m = ModelConfig(id="x")
    assert m.time_routing.enabled is False
    assert m.time_routing.slots == []


def test_time_routing_config_backward_compatible():
    """Old config JSON without 'time_routing' parses and routes unchanged."""
    raw = {
        "providers": [{"id": "a", "base_url": "http://a"}],
        "models": [
            {
                "id": "m",
                "backends": [{"provider": "a", "model": "m1", "priority": 1}],
            }
        ],
    }
    cfg = GatewayConfig.model_validate(raw)
    assert cfg.models[0].time_routing.enabled is False
    assert _ids(select_backends(cfg, "m")) == ["a"]


# ---------------------------------------------------------------- slot matching


def test_slot_matches_hour_range():
    slot = _slot(start_hour=9, end_hour=17)
    assert slot_matches(slot, _now(hour=9))
    assert slot_matches(slot, _now(hour=17))
    assert slot_matches(slot, _now(hour=12))
    assert not slot_matches(slot, _now(hour=8))
    assert not slot_matches(slot, _now(hour=18))


def test_slot_matches_wraparound():
    slot = _slot(start_hour=22, end_hour=6)
    assert slot_matches(slot, _now(hour=23))
    assert slot_matches(slot, _now(hour=0))
    assert slot_matches(slot, _now(hour=6))
    assert not slot_matches(slot, _now(hour=12))


def test_slot_matches_day_of_week():
    slot = _slot()
    slot.days_of_week = [0]  # Monday only
    assert slot_matches(slot, _now(weekday=0))
    assert not slot_matches(slot, _now(weekday=1))


def test_slot_matches_all_days_when_empty():
    slot = _slot()
    slot.days_of_week = []
    assert slot_matches(slot, _now(weekday=0))
    assert slot_matches(slot, _now(weekday=3))
    assert slot_matches(slot, _now(weekday=6))


def test_disabled_slot_never_matches():
    slot = _slot(enabled=False, start_hour=0, end_hour=23)
    assert not slot_matches(slot, _now(hour=12))


# ---------------------------------------------------------------- active slot


def test_get_active_slot_first_match():
    slots = [
        _slot(name="day", start_hour=9, end_hour=17),
        _slot(name="night", start_hour=18, end_hour=8),
    ]
    assert get_active_slot(slots, "UTC", now=_now(hour=12)).name == "day"
    assert get_active_slot(slots, "UTC", now=_now(hour=20)).name == "night"


def test_get_active_slot_none_when_no_match():
    slots = [_slot(start_hour=9, end_hour=17)]
    assert get_active_slot(slots, "UTC", now=_now(hour=20)) is None


def test_get_active_slot_none_when_empty():
    assert get_active_slot([], "UTC", now=_now(hour=12)) is None


def test_current_time_in_tz_invalid_falls_back_to_utc():
    tz = current_time_in_tz("Not/AZone")
    assert tz.tzinfo is not None


# ---------------------------------------------------------------- filtering


def test_filter_backends_active_providers():
    backends = [("a", "m1"), ("b", "m1"), ("c", "m1")]
    slot = _slot(active_providers=["a:m1", "c:m1"])
    filtered = filter_backends_for_slot(backends, slot)
    assert filtered == [("a", "m1"), ("c", "m1")]


def test_filter_backends_empty_active_passes_all():
    backends = [("a", "m1"), ("b", "m1")]
    slot = _slot(active_providers=[])
    assert filter_backends_for_slot(backends, slot) == backends


# ---------------------------------------------------------------- router integration


def test_router_disabled_uses_all_backends():
    """Feature OFF (default) → identical to today's behavior."""
    cfg = _config()
    order = _ids(select_backends(cfg, "m"))
    assert set(order[:2]) == {"a", "b"}
    assert order[-1] == "c"


def test_router_no_matching_slot_uses_all_backends():
    """Feature ON but no slot matches → all backends."""
    cfg = _config()
    cfg.models[0].time_routing = TimeRoutingConfig(
        enabled=True,
        timezone="UTC",
        slots=[_slot(start_hour=22, end_hour=6)],  # overnight; now is noon
    )
    order = _ids(select_backends(cfg, "m", now=_now(hour=12)))
    assert set(order[:2]) == {"a", "b"}
    assert order[-1] == "c"


def test_router_active_slot_filters_backends():
    """Feature ON + slot match → only whitelisted backends, tier order kept."""
    cfg = _config()
    cfg.models[0].time_routing = TimeRoutingConfig(
        enabled=True,
        timezone="UTC",
        slots=[_slot(name="peak", start_hour=9, end_hour=17, active_providers=["b:m1", "c:m1"])],
    )
    order = _ids(select_backends(cfg, "m", now=_now(hour=12)))
    # 'a' excluded; b (tier1) first, c (tier2) last.
    assert order == ["b", "c"]


def test_router_slot_filters_across_tiers():
    """Slot can restrict to a lower tier only."""
    cfg = _config()
    cfg.models[0].time_routing = TimeRoutingConfig(
        enabled=True,
        timezone="UTC",
        slots=[_slot(start_hour=9, end_hour=17, active_providers=["c:m1"])],
    )
    order = _ids(select_backends(cfg, "m", now=_now(hour=12)))
    assert order == ["c"]


def test_router_timezone_applied():
    """Slot times are evaluated in the configured timezone."""
    cfg = _config()
    # UTC 12:00 = 07:00 America/New_York (UTC-5). Slot 9-17 UTC doesn't match,
    # but a 7-8 NY slot does.
    cfg.models[0].time_routing = TimeRoutingConfig(
        enabled=True,
        timezone="America/New_York",
        slots=[_slot(name="ny-morning", start_hour=7, end_hour=8, active_providers=["c:m1"])],
    )
    order = _ids(select_backends(cfg, "m", now=_now(hour=12)))
    assert order == ["c"]


def test_router_cache_affinity_applies_after_filter():
    """Cache affinity operates on the filtered set."""
    from localgateway import stats as stats_mod
    cfg = _config()
    cfg.server.cache_affinity_enabled = True
    cfg.models[0].time_routing = TimeRoutingConfig(
        enabled=True,
        timezone="UTC",
        slots=[_slot(name="peak", start_hour=9, end_hour=17, active_providers=["a:m1", "b:m1"])],
    )
    ck = "prefix1"
    # Warm 'b' (tier 1) so cache prefers it within the filtered set.
    stats_mod.record_cache_activity(
        provider_id="b", backend_model="m1", cache_key=ck,
        cached_tokens=0, cache_write_tokens=50, input_tokens=100,
        cache_supported=True,
    )
    order = _ids(select_backends(cfg, "m", cache_key=ck, now=_now(hour=12)))
    assert "c" not in order  # filtered out by slot
    assert order[0] == "b"  # warm wins over cold 'a'
    stats_mod.reset()


# ---------------------------------------------------------------- admin CRUD (config-module level)


def test_admin_slot_crud(tmp_path):
    """Create/update/delete time slots through config save/load round-trips."""
    from localgateway.config import (
        GatewayConfig,
        TimeSlot,
        TimeRoutingConfig,
        _config_path,
        _config_lock,
        set_config_path,
        reload_config,
    )
    import json

    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "providers": [{"id": "a", "base_url": "http://a"}],
        "models": [{"id": "m", "backends": [{"provider": "a", "model": "m1", "priority": 1}]}],
    }))
    set_config_path(p)
    cfg = reload_config()
    m = cfg.models[0]

    # Initially disabled, empty slots
    assert m.time_routing.enabled is False
    assert m.time_routing.slots == []

    # Add a slot
    m.time_routing.slots.append(TimeSlot(
        name="peak", start_hour=9, end_hour=17,
        active_providers=["a:m1"],
    ))
    m.time_routing.enabled = True
    m.time_routing.timezone = "America/New_York"

    # Save and reload
    from localgateway.config import save_config
    save_config(cfg)
    cfg2 = reload_config()
    m2 = cfg2.models[0]
    assert m2.time_routing.enabled is True
    assert len(m2.time_routing.slots) == 1
    assert m2.time_routing.slots[0].start_hour == 9
    assert m2.time_routing.slots[0].active_providers == ["a:m1"]
    assert m2.time_routing.timezone == "America/New_York"

    # Update the slot
    m2.time_routing.slots[0].end_hour = 18
    save_config(cfg2)
    cfg3 = reload_config()
    assert cfg3.models[0].time_routing.slots[0].end_hour == 18

    # Delete the slot
    cfg3.models[0].time_routing.slots.clear()
    cfg3.models[0].time_routing.enabled = False
    save_config(cfg3)
    cfg4 = reload_config()
    assert cfg4.models[0].time_routing.slots == []
    assert cfg4.models[0].time_routing.enabled is False

    # Restore defaults for other tests
    set_config_path("config.json")
    with _config_lock:
        reload_config()

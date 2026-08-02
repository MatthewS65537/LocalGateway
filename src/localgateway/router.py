from __future__ import annotations

import datetime as _dt
import random
import threading
from dataclasses import dataclass
from typing import Any, Iterator, Literal

from .config import GatewayConfig, ModelConfig, BackendConfig, ProviderConfig
from .ratelimit import ratelimit
from . import stats as stats_mod


@dataclass
class SelectedBackend:
    provider: ProviderConfig
    backend: BackendConfig


@dataclass
class ProviderPrefs:
    """OpenRouter-style per-request provider preferences (local subset)."""
    order: list[str] | None = None
    ignore: list[str] | None = None
    allow_fallbacks: bool = True
    sort: Literal["latency", "throughput", "price", "cache"] | None = None

    @classmethod
    def from_request(cls, body: dict | None) -> "ProviderPrefs | None":
        if not body or not isinstance(body.get("provider"), dict):
            return None
        raw = body["provider"]
        order = raw.get("order")
        ignore = raw.get("ignore")
        if order is not None and not isinstance(order, list):
            order = None
        if ignore is not None and not isinstance(ignore, list):
            ignore = None
        allow = raw.get("allow_fallbacks", True)
        if not isinstance(allow, bool):
            allow = True
        sort = raw.get("sort")
        if sort not in ("latency", "throughput", "price", "cache", None):
            sort = None
        return cls(
            order=[str(x) for x in order] if order else None,
            ignore=[str(x) for x in ignore] if ignore else None,
            allow_fallbacks=allow,
            sort=sort,
        )


_rr_lock = threading.Lock()
_rr: dict[str, int] = {}


def _next_rotation(model_id: str) -> int:
    with _rr_lock:
        n = _rr.get(model_id, 0)
        _rr[model_id] = n + 1
        return n


def _sort_key(
    selected: SelectedBackend,
    config: GatewayConfig,
    sort: str,
    cache_key: str | None = None,
) -> tuple:
    snap = stats_mod.snapshot()
    key = f"{selected.provider.id}:{selected.backend.model}"
    st = snap.get(key, {})
    if sort == "latency":
        lat = st.get("avg_latency_ms")
        return (lat is None, lat if lat is not None else 0)
    if sort == "throughput":
        # Prefer higher success rate then lower latency as proxy for throughput
        rate = st.get("success_rate")
        lat = st.get("avg_latency_ms") or 999999
        return (
            rate is None,
            -(rate if rate is not None else 0),
            lat,
        )
    if sort == "price":
        pricing = config.pricing_for(selected.provider.id, selected.backend.model)
        # Sort by input+output combined; missing price last
        total = (pricing.input or 0) + (pricing.output or 0)
        missing = pricing.input == 0 and pricing.output == 0
        return (missing, total)
    if sort == "cache":
        # Prefer warm backends (higher hit-rate) then lower latency.
        ttl = getattr(config.server, "cache_affinity_ttl_sec", 300) or 300
        warm = stats_mod.warm_backends_for(cache_key or "", [key], ttl)
        is_warm = 0 if warm else 1
        warm_rate = 0.0
        if warm:
            wsnap = stats_mod.warmth_snapshot()
            entry = (wsnap.get(cache_key or "") or {}).get(key)
            if entry:
                warm_rate = entry.get("last_hit_rate", 0.0)
        lat = st.get("avg_latency_ms") or 999999
        return (is_warm, -warm_rate, lat)
    return (0,)


def select_backends(
    config: GatewayConfig,
    model_id: str,
    max_tokens: int | None = None,
    input_tokens: int | None = None,
    *,
    prefs: ProviderPrefs | None = None,
    rng: random.Random | None = None,
    cache_key: str | None = None,
    now: _dt.datetime | None = None,
) -> Iterator[SelectedBackend]:
    """Yield backends in fallback order.

    Strategy: TIERED with round-robin load balancing within each tier.
    - Backends are grouped by priority; lower number = higher tier.
    - Routing mode (config.server.routing_mode):
      - "failover": strict tier order (tier 1 first, then 2, etc.)
      - "explore": probabilistic tier selection with decaying weights.
    - Optional per-request ``prefs`` (order / ignore / allow_fallbacks / sort).
    - Within a tier, available backends are rotated round-robin (or sorted).
    - Rate-limited backends are deferred to the end.

    Cache affinity (config.server.cache_affinity_enabled + ``cache_key``):
    When active, selection takes PRECEDENCE over tiers/round-robin/order/sort.
    Priority bands:
      1. warm + idle  2. warm + busy  3. cold + idle  4. cold + busy  5. ratelimited.
    Warm = backend has a non-expired warmth entry for ``cache_key``.
    Idle = in_flight == 0. ``max_inflight_before_spill`` caps warm-busy use.
    Existing policies (tier order, round-robin, sort, ``order``) only break
    ties within a band; cache always wins over ``order``.
    """
    model = config.model_by_id(model_id)
    if model is None or not model.enabled:
        return

    ignore = set(prefs.ignore or []) if prefs else set()
    order = prefs.order if prefs and prefs.order else None
    allow_fallbacks = prefs.allow_fallbacks if prefs else True
    sort = prefs.sort if prefs else None

    cache_affinity = bool(getattr(config.server, "cache_affinity_enabled", False))
    cache_ttl = getattr(config.server, "cache_affinity_ttl_sec", 300) or 300
    max_inflight = getattr(config.server, "max_inflight_before_spill", None)
    affinity_active = cache_affinity and bool(cache_key)

    # ---- time-based routing filter (per-model, OFF by default) ----
    time_routing = getattr(model, "time_routing", None)
    active_slot = None
    if (
        time_routing is not None
        and time_routing.enabled
        and time_routing.slots
    ):
        from .time_routing import get_active_slot
        active_slot = get_active_slot(
            time_routing.slots, time_routing.timezone, now=now
        )

    tiers: dict[int, list[tuple[ProviderConfig, BackendConfig]]] = {}
    for b in model.backends:
        if not b.enabled:
            continue
        if b.provider in ignore:
            continue
        provider = config.provider_by_id(b.provider)
        if provider is None or not provider.enabled:
            continue
        if max_tokens is not None and b.max_output_tokens is not None and b.max_output_tokens < max_tokens:
            continue
        if input_tokens is not None and b.context_length is not None and b.context_length < input_tokens:
            continue
        if active_slot is not None:
            from .time_routing import filter_backends_for_slot
            if not filter_backends_for_slot([(b.provider, b.model)], active_slot):
                continue
        tiers.setdefault(b.priority, []).append((provider, b))

    if not tiers:
        return

    # ``order`` providers, collected for tiebreaking (no longer a synthetic
    # first tier when cache affinity is active — cache always wins over order).
    preferred: list[SelectedBackend] = []
    if order:
        by_provider: dict[str, list[tuple[ProviderConfig, BackendConfig]]] = {}
        for key in sorted(tiers.keys()):
            for p, b in tiers[key]:
                by_provider.setdefault(p.id, []).append((p, b))
        for pid in order:
            for p, b in by_provider.get(pid, []):
                preferred.append(SelectedBackend(provider=p, backend=b))

        if preferred and not allow_fallbacks and not affinity_active:
            # Only preferred providers; still respect rate limits order.
            # Cache affinity off here (affinity_active is False in this branch).
            avail = [s for s in preferred if ratelimit.is_available(s.provider.id, s.backend.model)]
            limited = [s for s in preferred if not ratelimit.is_available(s.provider.id, s.backend.model)]
            if sort:
                avail.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
                limited.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
            yield from avail
            yield from limited
            return

    rotation = _next_rotation(model.id)
    sorted_tier_keys = sorted(tiers.keys())

    available_by_tier: dict[int, list[SelectedBackend]] = {}
    ratelimited_by_tier: dict[int, list[SelectedBackend]] = {}

    preferred_keys = {(s.provider.id, s.backend.model) for s in preferred}

    for key in sorted_tier_keys:
        group = tiers[key]
        # When order is set and allow_fallbacks, skip already-preferred entries
        # in tiers (they're emitted via the preferred path).
        if preferred_keys and not affinity_active:
            group = [(p, b) for p, b in group if (p.id, b.model) not in preferred_keys]
        avail = [(p, b) for p, b in group if ratelimit.is_available(b.provider, b.model)]
        limited = [(p, b) for p, b in group if not ratelimit.is_available(b.provider, b.model)]
        avail_sel = [SelectedBackend(provider=p, backend=b) for p, b in avail]
        limited_sel = [SelectedBackend(provider=p, backend=b) for p, b in limited]
        if sort:
            avail_sel.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
            limited_sel.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
        elif avail_sel:
            r = rotation % len(avail_sel)
            avail_sel = avail_sel[r:] + avail_sel[:r]
        available_by_tier[key] = avail_sel
        ratelimited_by_tier[key] = limited_sel

    # ---- Cache-affinity reordering (takes precedence over everything) ----
    if affinity_active:
        yield from _emit_cache_affinity(
            config=config,
            cache_key=cache_key or "",
            cache_ttl=cache_ttl,
            max_inflight=max_inflight,
            preferred=preferred,
            preferred_keys=preferred_keys,
            available_by_tier=available_by_tier,
            ratelimited_by_tier=ratelimited_by_tier,
            sorted_tier_keys=sorted_tier_keys,
            order=order,
        )
        return

    # ---- Legacy (cache affinity off): preserve existing emission order ----
    # Emit preferred first (rate-limit aware)
    if preferred:
        avail_p = [s for s in preferred if ratelimit.is_available(s.provider.id, s.backend.model)]
        limited_p = [s for s in preferred if not ratelimit.is_available(s.provider.id, s.backend.model)]
        if sort:
            avail_p.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
            limited_p.sort(key=lambda s: _sort_key(s, config, sort, cache_key))
        yield from avail_p
        # limited preferred deferred with other ratelimited

    mode = getattr(config.server, "routing_mode", "failover") or "failover"
    decay = getattr(config.server, "routing_decay", 0.4) or 0.4

    if mode == "explore" and len(sorted_tier_keys) > 1 and not order:
        rng = rng or random
        tier_order = _sample_tier_order(sorted_tier_keys, decay, rng)
    else:
        tier_order = sorted_tier_keys

    for key in tier_order:
        yield from available_by_tier.get(key, [])

    if preferred:
        yield from [s for s in preferred if not ratelimit.is_available(s.provider.id, s.backend.model)]

    for key in sorted_tier_keys:
        yield from ratelimited_by_tier.get(key, [])


def _backend_key(s: SelectedBackend) -> str:
    return f"{s.provider.id}:{s.backend.model}"


def _apply_tiebreak(
    items: list[SelectedBackend],
    order: list[str] | None,
) -> list[SelectedBackend]:
    """Stable-sort by provider ``order`` (preferred providers first)."""
    if not order:
        return items
    rank = {pid: i for i, pid in enumerate(order)}

    def pos(s: SelectedBackend) -> int:
        return rank.get(s.provider.id, len(order))

    # stable sort preserves prior (tier/round-robin) order among equal ranks
    return sorted(items, key=pos)


def _emit_cache_affinity(
    *,
    config: GatewayConfig,
    cache_key: str,
    cache_ttl: float,
    max_inflight: int | None,
    preferred: list[SelectedBackend],
    preferred_keys: set[tuple[str, str]],
    available_by_tier: dict[int, list[SelectedBackend]],
    ratelimited_by_tier: dict[int, list[SelectedBackend]],
    sorted_tier_keys: list[int],
    order: list[str] | None,
) -> Iterator[SelectedBackend]:
    """Emit backends in the 5-band cache-affinity priority order.

    Bands: warm+idle, warm+busy, cold+idle, cold+busy, then ratelimited.
    Within each band, candidates are ordered by tier then round-robin/round
    already baked into available_by_tier, then by ``order`` as a tiebreaker.
    """
    snap = stats_mod.snapshot()

    # Flatten available candidates in tier order; this is the base tiebreaker.
    flat: list[SelectedBackend] = []
    if preferred:
        # Preferred providers participate; keep their order first within band.
        flat.extend(preferred)
    for key in sorted_tier_keys:
        for s in available_by_tier.get(key, []):
            if preferred_keys and (s.provider.id, s.backend.model) in preferred_keys:
                continue
            flat.append(s)

    candidate_keys = [_backend_key(s) for s in flat]
    warm_keys = set(stats_mod.warm_backends_for(cache_key, candidate_keys, cache_ttl))

    warm: list[SelectedBackend] = []
    cold: list[SelectedBackend] = []
    for s in flat:
        (warm if _backend_key(s) in warm_keys else cold).append(s)

    def inflight(s: SelectedBackend) -> int:
        st = snap.get(_backend_key(s), {})
        return int(st.get("in_flight") or 0)

    def split_idle_busy(items: list[SelectedBackend]) -> tuple[list[SelectedBackend], list[SelectedBackend]]:
        idle, busy = [], []
        for s in items:
            (idle if inflight(s) == 0 else busy).append(s)
        busy.sort(key=inflight)  # least-busy first
        return idle, busy

    warm_idle, warm_busy = split_idle_busy(warm)
    cold_idle, cold_busy = split_idle_busy(cold)

    # ``order`` tiebreaker applied within each band (stable).
    warm_idle = _apply_tiebreak(warm_idle, order)
    warm_busy = _apply_tiebreak(warm_busy, order)
    cold_idle = _apply_tiebreak(cold_idle, order)
    cold_busy = _apply_tiebreak(cold_busy, order)

    # Cap warm-busy: spill beyond max_inflight into cold-busy (they're busy,
    # that's why they spilled — so they follow genuinely cold idle backends).
    if max_inflight is not None and warm_busy:
        keep, spill = [], []
        for s in warm_busy:
            (keep if inflight(s) < max_inflight else spill).append(s)
        warm_busy = keep
        cold_busy = spill + cold_busy
        cold_busy = _apply_tiebreak(cold_busy, order)

    yield from warm_idle
    yield from warm_busy
    yield from cold_idle
    yield from cold_busy

    # Ratelimited backends last (preserving preferred-then-tier order).
    if preferred:
        for s in preferred:
            if not ratelimit.is_available(s.provider.id, s.backend.model):
                if (s.provider.id, s.backend.model) in preferred_keys:
                    yield s
    for key in sorted_tier_keys:
        for s in ratelimited_by_tier.get(key, []):
            if preferred_keys and (s.provider.id, s.backend.model) in preferred_keys:
                continue
            yield s


def _sample_tier_order(
    tier_keys: list[int],
    decay: float,
    rng: random.Random,
) -> list[int]:
    """Sample a tier order with decaying probability for the first pick."""
    weights = [decay**i for i in range(len(tier_keys))]
    total = sum(weights)
    probs = [w / total for w in weights]

    r = rng.random()
    cumulative = 0.0
    start_idx = 0
    for i, p in enumerate(probs):
        cumulative += p
        if r < cumulative:
            start_idx = i
            break

    start_key = tier_keys[start_idx]
    order = [start_key]
    order.extend(k for k in tier_keys if k != start_key)
    return order

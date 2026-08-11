from __future__ import annotations

import datetime as _dt
import random
import threading
from dataclasses import dataclass
from typing import Any, Iterator, Literal

from .config import GatewayConfig, ModelConfig, BackendConfig, ProviderConfig
from .ratelimit import ratelimit
from . import stats as stats_mod
from . import circuitbreaker


@dataclass
class SelectedBackend:
    provider: ProviderConfig
    backend: BackendConfig
    # Why this backend was chosen — set by the router at emission time so the
    # request path can expose it as an X-Routing-Reason response header. Values:
    # "warm-idle", "warm-busy", "cold-idle", "cold-busy", "ratelimited-fallback",
    # "tier-1"/"tier-2"/..., "order-pref". A sort suffix (e.g. " sort:throughput")
    # is appended when a stats-driven sort is active.
    reason: str = ""


@dataclass
class ProviderPrefs:
    """OpenRouter-style per-request provider preferences (local subset)."""
    order: list[str] | None = None
    ignore: list[str] | None = None
    allow_fallbacks: bool = True
    sort: Literal["latency", "throughput", "price", "cache", "value"] | None = None

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
        if sort not in ("latency", "throughput", "price", "cache", "value", None):
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


def _blended_price(config: GatewayConfig, provider_id: str, model: str) -> float | None:
    """Input+output $/token for value computations; None when unpriced."""
    pricing = config.pricing_for(provider_id, model)
    total = (pricing.input or 0) + (pricing.output or 0)
    if pricing.input == 0 and pricing.output == 0:
        return None
    return total


def _sort_key(
    selected: SelectedBackend,
    config: GatewayConfig,
    sort: str,
    cache_key: str | None = None,
    tps_map: dict[str, float] | None = None,
    snap: dict | None = None,
    warm_lookup: dict[str, float] | None = None,
) -> tuple:
    # P3: snapshot() and the warmth lookup are hoisted above the sort by the
    # caller — the old key function rebuilt the whole registry per candidate,
    # making every sorted request O(N²) (N full snapshot() calls, each taking
    # the global lock and materializing the entire stats dict).
    snap = snap if snap is not None else stats_mod.snapshot()
    key = f"{selected.provider.id}:{selected.backend.model}"
    st = snap.get(key, {})
    if sort == "latency":
        lat = st.get("avg_latency_ms")
        return (lat is None, lat if lat is not None else 0)
    if sort == "throughput":
        # Prefer measured TPS p50 from the usage DB (includes probe
        # measurements); fall back to the success-rate+latency proxy when the
        # model has no measurements at all yet.
        if tps_map:
            tps = tps_map.get(key)
            return (tps is None, -(tps or 0.0))
        rate = st.get("success_rate")
        lat = st.get("avg_latency_ms") or 999999
        return (
            rate is None,
            -(rate if rate is not None else 0),
            lat,
        )
    if sort == "price":
        total = _blended_price(config, selected.provider.id, selected.backend.model)
        return (total is None, total or 0)
    if sort == "value":
        # Best throughput per dollar: measured TPS p50 ÷ blended price.
        # Unknown TPS or unknown price sorts last.
        tps = (tps_map or {}).get(key)
        price = _blended_price(config, selected.provider.id, selected.backend.model)
        if tps is None or price is None:
            return (True, 0.0)
        value = tps / max(price, 1e-12)
        return (False, -value)
    if sort == "cache":
        # Prefer warm backends (higher hit-rate) then lower latency.
        is_warm = 0 if (warm_lookup or {}).get(key) else 1
        warm_rate = (warm_lookup or {}).get(key, 0.0)
        lat = st.get("avg_latency_ms") or 999999
        return (is_warm, -warm_rate, lat)
    return (0,)


def _sort_context(
    config: GatewayConfig,
    sort: str,
    cache_key: str | None,
    candidates: list[SelectedBackend],
) -> tuple[dict, dict[str, float]]:
    """One-time snapshot + warmth map for a sorted pass (P3 — O(N), not O(N²))."""
    snap = stats_mod.snapshot()
    warm_lookup: dict[str, float] = {}
    if sort == "cache" and candidates:
        ttl = getattr(config.server, "cache_affinity_ttl_sec", 300) or 300
        keys = [_backend_key(s) for s in candidates]
        warm = stats_mod.warm_backends_for(cache_key or "", keys, ttl)
        wsnap = stats_mod.warmth_snapshot()
        bucket = (wsnap.get(cache_key or "") or {})
        for k in warm:
            entry = bucket.get(k) or {}
            warm_lookup[k] = entry.get("last_hit_rate", 0.0)
    return snap, warm_lookup


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

    # Keep the circuit breaker in sync with the (hot-reloaded) config.
    circuitbreaker.configure_from_config(config)

    ignore = set(prefs.ignore or []) if prefs else set()
    order = prefs.order if prefs and prefs.order else None
    allow_fallbacks = prefs.allow_fallbacks if prefs else True
    sort = prefs.sort if prefs else None

    cache_affinity = bool(getattr(config.server, "cache_affinity_enabled", False))
    cache_ttl = getattr(config.server, "cache_affinity_ttl_sec", 300) or 300
    max_inflight = getattr(config.server, "max_inflight_before_spill", None)
    affinity_active = cache_affinity and bool(cache_key)

    # Measured TPS p50 per backend (usage DB + probes), for stats-driven
    # routing. Loaded when a stats-driven sort is requested or explore-mode
    # weighting is on; empty until traffic/probes produce measurements.
    stats_routing = bool(getattr(config.server, "stats_routing_enabled", True))
    routing_mode = getattr(config.server, "routing_mode", "failover") or "failover"
    tps_map: dict[str, float] = {}
    if stats_routing and (sort in ("throughput", "value") or routing_mode == "explore"):
        try:
            from .usage import get_backend_tps_map
            tps_map = get_backend_tps_map(model.id)
        except Exception:
            tps_map = {}

    def _routable(provider_id: str, backend_model: str) -> bool:
        """Available = not rate-limited AND circuit closed."""
        return ratelimit.is_available(provider_id, backend_model) and not circuitbreaker.is_open(provider_id, backend_model)

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
        if max_tokens is not None:
            # B12: the model-level max_output_tokens was dead config — only the
            # per-backend cap was enforced. Effective cap = min(backend, model).
            model_cap = getattr(model, "max_output_tokens", None)
            effective = b.max_output_tokens
            if model_cap is not None and (effective is None or model_cap < effective):
                effective = model_cap
            if effective is not None and effective < max_tokens:
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
            avail = [s for s in preferred if _routable(s.provider.id, s.backend.model)]
            limited = [s for s in preferred if not _routable(s.provider.id, s.backend.model)]
            if sort:
                snap, warm = _sort_context(config, sort, cache_key, avail + limited)
                avail.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
                limited.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
            _tag_reason(avail, "order-pref" + _sort_suffix(sort))
            _tag_reason(limited, "ratelimited-fallback")
            yield from avail
            yield from limited
            return

    rotation = _next_rotation(model.id)
    sorted_tier_keys = sorted(tiers.keys())
    rng = rng or random

    available_by_tier: dict[int, list[SelectedBackend]] = {}
    ratelimited_by_tier: dict[int, list[SelectedBackend]] = {}

    preferred_keys = {(s.provider.id, s.backend.model) for s in preferred}

    for key in sorted_tier_keys:
        group = tiers[key]
        # When order is set and allow_fallbacks, skip already-preferred entries
        # in tiers (they're emitted via the preferred path).
        if preferred_keys and not affinity_active:
            group = [(p, b) for p, b in group if (p.id, b.model) not in preferred_keys]
        avail = [(p, b) for p, b in group if _routable(b.provider, b.model)]
        limited = [(p, b) for p, b in group if not _routable(b.provider, b.model)]
        avail_sel = [SelectedBackend(provider=p, backend=b) for p, b in avail]
        limited_sel = [SelectedBackend(provider=p, backend=b) for p, b in limited]
        if sort:
            snap, warm = _sort_context(config, sort, cache_key, avail_sel + limited_sel)
            avail_sel.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
            limited_sel.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
        elif avail_sel and tps_map and routing_mode == "explore" and rng is not None:
            avail_sel = _weighted_rotation(avail_sel, tps_map, rotation, rng)
        elif avail_sel:
            # Static config-driven weights (F8): when backends declare non-uniform
            # weights, draw the first candidate proportional to its weight (canary
            # splits, drain-to-zero, quota preference). Falls back to plain RR.
            weights = [getattr(s.backend, "weight", 1.0) for s in avail_sel]
            if any(w != 1.0 for w in weights):
                avail_sel = _static_weight_rotation(avail_sel, weights, rotation, rng)
            else:
                r = rotation % len(avail_sel)
                avail_sel = avail_sel[r:] + avail_sel[:r]
        available_by_tier[key] = avail_sel
        ratelimited_by_tier[key] = limited_sel

    # ---- Cache-affinity reordering (takes precedence over everything) ----
    if affinity_active:
        suffix = _sort_suffix(sort)
        for s in _emit_cache_affinity(
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
        ):
            if suffix and s.reason and not s.reason.startswith("ratelimited"):
                s.reason += suffix
            yield s
        return

    # ---- Legacy (cache affinity off): preserve existing emission order ----
    # Emit preferred first (rate-limit aware)
    suffix = _sort_suffix(sort)
    if preferred:
        avail_p = [s for s in preferred if _routable(s.provider.id, s.backend.model)]
        limited_p = [s for s in preferred if not _routable(s.provider.id, s.backend.model)]
        if sort:
            snap, warm = _sort_context(config, sort, cache_key, avail_p + limited_p)
            avail_p.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
            limited_p.sort(key=lambda s: _sort_key(s, config, sort, cache_key, tps_map, snap, warm))
        _tag_reason(avail_p, "order-pref" + suffix)
        _tag_reason(limited_p, "ratelimited-fallback")
        yield from avail_p
        # limited preferred deferred with other ratelimited

    mode = routing_mode
    decay = getattr(config.server, "routing_decay", 0.4) or 0.4

    if mode == "explore" and len(sorted_tier_keys) > 1 and not order:
        tier_order = _sample_tier_order(sorted_tier_keys, decay, rng)
    else:
        tier_order = sorted_tier_keys

    for key in tier_order:
        tier_backends = available_by_tier.get(key, [])
        _tag_reason(tier_backends, f"tier-{key}" + suffix)
        yield from tier_backends

    if preferred:
        leftover = [s for s in preferred if not _routable(s.provider.id, s.backend.model)]
        _tag_reason(leftover, "ratelimited-fallback")
        yield from leftover

    for key in sorted_tier_keys:
        rl = ratelimited_by_tier.get(key, [])
        _tag_reason(rl, "ratelimited-fallback")
        yield from rl


def _backend_key(s: SelectedBackend) -> str:
    return f"{s.provider.id}:{s.backend.model}"


def _tag_reason(items: list[SelectedBackend], reason: str) -> None:
    """Set the routing reason on each backend in-place."""
    for s in items:
        s.reason = reason


def _sort_suffix(sort: str | None) -> str:
    """Suffix appended to the reason when a stats-driven sort is active."""
    return f" sort:{sort}" if sort else ""


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
        # Preferred providers participate (only routable ones); keep their
        # order first within band.
        flat.extend(
            s for s in preferred
            if ratelimit.is_available(s.provider.id, s.backend.model)
            and not circuitbreaker.is_open(s.provider.id, s.backend.model)
        )
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

    # Tag each backend with the reason it was chosen (band name). The sort
    # suffix is added by the caller via _sort_suffix when sort is active.
    _tag_reason(warm_idle, "warm-idle")
    _tag_reason(warm_busy, "warm-busy")
    _tag_reason(cold_idle, "cold-idle")
    _tag_reason(cold_busy, "cold-busy")

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

    # Ratelimited / circuit-open backends last (preferred-then-tier order).
    if preferred:
        for s in preferred:
            if not ratelimit.is_available(s.provider.id, s.backend.model) or circuitbreaker.is_open(s.provider.id, s.backend.model):
                if (s.provider.id, s.backend.model) in preferred_keys:
                    s.reason = "ratelimited-fallback"
                    yield s
    for key in sorted_tier_keys:
        for s in ratelimited_by_tier.get(key, []):
            if preferred_keys and (s.provider.id, s.backend.model) in preferred_keys:
                continue
            s.reason = "ratelimited-fallback"
            yield s


def _weighted_rotation(
    items: list[SelectedBackend],
    tps_map: dict[str, float],
    rotation: int,
    rng: random.Random,
) -> list[SelectedBackend]:
    """Performance-weighted round-robin: the first candidate is drawn with
    probability proportional to its measured TPS p50 (unmeasured backends get
    the average weight so explore still discovers them); the rest follow in
    plain round-robin order. Falls back to plain rotation when nothing is
    measured yet."""
    n = len(items)
    if n <= 1:
        return items
    weights: list[float] = []
    measured = [tps_map[f"{s.provider.id}:{s.backend.model}"] for s in items if f"{s.provider.id}:{s.backend.model}" in tps_map]
    if not measured:
        r = rotation % n
        return items[r:] + items[:r]
    avg = sum(measured) / len(measured)
    for s in items:
        tps = tps_map.get(f"{s.provider.id}:{s.backend.model}")
        weights.append(tps if tps is not None else avg)
    total = sum(weights)
    pick = rng.random() * total
    acc = 0.0
    start = 0
    for i, w in enumerate(weights):
        acc += w
        if pick < acc:
            start = i
            break
    return items[start:] + items[:start]


def _static_weight_rotation(
    items: list[SelectedBackend],
    weights: list[float],
    rotation: int,
    rng: random.Random,
) -> list[SelectedBackend]:
    """Config-driven weighted round-robin (F8): the first candidate is drawn
    with probability proportional to its static ``BackendConfig.weight``; the
    rest follow in round-robin order. Weight 0 = never picked first (drain).
    Falls back to plain rotation when all weights are equal/zero."""
    n = len(items)
    if n <= 1:
        return items
    total = sum(max(w, 0.0) for w in weights)
    if total <= 0:
        r = rotation % n
        return items[r:] + items[:r]
    pick = rng.random() * total
    acc = 0.0
    start = 0
    for i, w in enumerate(weights):
        acc += max(w, 0.0)
        if pick < acc:
            start = i
            break
    return items[start:] + items[:start]


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

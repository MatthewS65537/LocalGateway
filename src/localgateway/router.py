from __future__ import annotations

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
    sort: Literal["latency", "throughput", "price"] | None = None

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
        if sort not in ("latency", "throughput", "price", None):
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
    return (0,)


def select_backends(
    config: GatewayConfig,
    model_id: str,
    max_tokens: int | None = None,
    input_tokens: int | None = None,
    *,
    prefs: ProviderPrefs | None = None,
    rng: random.Random | None = None,
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
    """
    model = config.model_by_id(model_id)
    if model is None or not model.enabled:
        return

    ignore = set(prefs.ignore or []) if prefs else set()
    order = prefs.order if prefs and prefs.order else None
    allow_fallbacks = prefs.allow_fallbacks if prefs else True
    sort = prefs.sort if prefs else None

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
        tiers.setdefault(b.priority, []).append((provider, b))

    if not tiers:
        return

    # Prefer providers listed in order: treat as a synthetic first tier
    preferred: list[SelectedBackend] = []
    if order:
        by_provider: dict[str, list[tuple[ProviderConfig, BackendConfig]]] = {}
        for key in sorted(tiers.keys()):
            for p, b in tiers[key]:
                by_provider.setdefault(p.id, []).append((p, b))
        for pid in order:
            for p, b in by_provider.get(pid, []):
                preferred.append(SelectedBackend(provider=p, backend=b))

        if preferred and not allow_fallbacks:
            # Only preferred providers; still respect rate limits order
            avail = [s for s in preferred if ratelimit.is_available(s.provider.id, s.backend.model)]
            limited = [s for s in preferred if not ratelimit.is_available(s.provider.id, s.backend.model)]
            if sort:
                avail.sort(key=lambda s: _sort_key(s, config, sort))
                limited.sort(key=lambda s: _sort_key(s, config, sort))
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
        # When order is set and allow_fallbacks, skip already-preferred entries in tiers
        if preferred_keys:
            group = [(p, b) for p, b in group if (p.id, b.model) not in preferred_keys]
        avail = [(p, b) for p, b in group if ratelimit.is_available(b.provider, b.model)]
        limited = [(p, b) for p, b in group if not ratelimit.is_available(b.provider, b.model)]
        avail_sel = [SelectedBackend(provider=p, backend=b) for p, b in avail]
        limited_sel = [SelectedBackend(provider=p, backend=b) for p, b in limited]
        if sort:
            avail_sel.sort(key=lambda s: _sort_key(s, config, sort))
            limited_sel.sort(key=lambda s: _sort_key(s, config, sort))
        elif avail_sel:
            r = rotation % len(avail_sel)
            avail_sel = avail_sel[r:] + avail_sel[:r]
        available_by_tier[key] = avail_sel
        ratelimited_by_tier[key] = limited_sel

    # Emit preferred first (rate-limit aware)
    if preferred:
        avail_p = [s for s in preferred if ratelimit.is_available(s.provider.id, s.backend.model)]
        limited_p = [s for s in preferred if not ratelimit.is_available(s.provider.id, s.backend.model)]
        if sort:
            avail_p.sort(key=lambda s: _sort_key(s, config, sort))
            limited_p.sort(key=lambda s: _sort_key(s, config, sort))
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

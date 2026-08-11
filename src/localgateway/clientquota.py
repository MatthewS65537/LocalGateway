"""Inbound client quotas: per-key RPM limiting and budget enforcement.

The gateway carefully handles UPSTREAM 429s (ratelimit.py); this module is
the inbound counterpart for API keys (``server.api_keys``). All state is
in-memory and process-local.

- RPM: exact sliding-window counter per key.
- Budgets: daily/monthly spend read from the usage DB, cached briefly
  (seconds-scale granularity — a burst within the cache window can overshoot
  by a few requests before the hard stop engages).
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .config import ApiKeyConfig

_WINDOW_S = 60.0
_SPEND_CACHE_TTL = 5.0
_BURN_CACHE_TTL = 60.0

_lock = threading.Lock()
_hits: dict[str, deque] = {}
_spend_cache: dict[str, tuple[float, dict]] = {}
_burn_cache: dict[str, tuple[float, float]] = {}


def check_rpm(key: ApiKeyConfig) -> float | None:
    """Register a request for this key. Returns seconds to retry-after when
    the key is over its RPM limit, else None (allowed)."""
    if not key.rpm or key.rpm <= 0 or not key.id:
        return None
    now = time.monotonic()
    with _lock:
        dq = _hits.setdefault(key.id, deque())
        while dq and now - dq[0] > _WINDOW_S:
            dq.popleft()
        if len(dq) >= key.rpm:
            return round(_WINDOW_S - (now - dq[0]), 1)
        dq.append(now)
    return None


def current_rpm(key_id: str) -> int:
    """Requests in the current window (for admin introspection)."""
    now = time.monotonic()
    with _lock:
        dq = _hits.get(key_id)
        if not dq:
            return 0
        while dq and now - dq[0] > _WINDOW_S:
            dq.popleft()
        return len(dq)


def get_spend(key_id: str) -> dict:
    """Daily/monthly spend for a key, cached a few seconds."""
    if not key_id:
        return {"day_cost": 0.0, "day_requests": 0, "month_cost": 0.0, "month_requests": 0}
    now = time.monotonic()
    with _lock:
        hit = _spend_cache.get(key_id)
        if hit and now - hit[0] < _SPEND_CACHE_TTL:
            return hit[1]
    from .usage import get_key_spend
    spend = get_key_spend(key_id)
    with _lock:
        _spend_cache[key_id] = (now, spend)
    return spend


def _burn_rate(key_id: str) -> float:
    """Average daily burn for a key, cached 60s (P2 — no per-request SQLite)."""
    now = time.monotonic()
    with _lock:
        hit = _burn_cache.get(key_id)
        if hit and now - hit[0] < _BURN_CACHE_TTL:
            return hit[1]
    from .usage import get_key_daily_burn
    burn = get_key_daily_burn(key_id, days=7)
    with _lock:
        _burn_cache[key_id] = (now, burn)
    return burn


def invalidate_spend(key_id: str | None = None) -> None:
    """Drop cached spend (called after logging a request with a cost)."""
    with _lock:
        if key_id is None:
            _spend_cache.clear()
            _burn_cache.clear()
        else:
            _spend_cache.pop(key_id, None)
            _burn_cache.pop(key_id, None)


def check_budget(key: ApiKeyConfig) -> tuple[str, float, float] | None:
    """Returns (period, spent, limit) when a budget is exceeded, else None."""
    if not key.id:
        return None
    if not key.daily_budget_usd and not key.monthly_budget_usd:
        return None
    spend = get_spend(key.id)
    if key.daily_budget_usd and spend["day_cost"] >= key.daily_budget_usd:
        return ("daily", spend["day_cost"], key.daily_budget_usd)
    if key.monthly_budget_usd and spend["month_cost"] >= key.monthly_budget_usd:
        return ("monthly", spend["month_cost"], key.monthly_budget_usd)
    return None


def budget_warnings(key: ApiKeyConfig) -> list[tuple[str, float, float]]:
    """Budgets at ≥80% utilization (for soft-warning headers)."""
    out: list[tuple[str, float, float]] = []
    if not key.id:
        return out
    spend = get_spend(key.id)
    if key.daily_budget_usd:
        ratio = spend["day_cost"] / key.daily_budget_usd
        if 0.8 <= ratio < 1.0:
            out.append(("daily", spend["day_cost"], key.daily_budget_usd))
    if key.monthly_budget_usd:
        ratio = spend["month_cost"] / key.monthly_budget_usd
        if 0.8 <= ratio < 1.0:
            out.append(("monthly", spend["month_cost"], key.monthly_budget_usd))
    return out


def budget_eta(key: ApiKeyConfig) -> dict | None:
    """Project budget exhaustion from the trailing-7-day burn rate (F4).

    Returns {"period", "eta_days", "eta_date", "burn_rate", "remaining"}
    for the first budget that has a meaningful burn, else None. ``eta_days``
    is float('inf') when the key is on pace to never exhaust (burn ~0).

    P2: previously this called get_key_spend (bypassing its own 5s cache)
    AND get_key_daily_burn — 3 uncached synchronous SQLite connections on
    every chat request with a budget. Both now use caches (5s spend, 60s
    burn), removing the per-request DB hits from the hot path.
    """
    if not key.id:
        return None
    if not key.daily_budget_usd and not key.monthly_budget_usd:
        return None
    spend = get_spend(key.id)
    burn = _burn_rate(key.id)
    if burn <= 0:
        return None

    candidates: list[tuple[str, float, float]] = []
    if key.daily_budget_usd:
        remaining = max(key.daily_budget_usd - spend["day_cost"], 0.0)
        # Daily budget: burn rate is already $/day, so ETA = remaining / burn.
        # But "remaining" for daily resets at midnight; use the burn rate to
        # project when today's budget crosses, scaled by remaining fraction.
        eta_days = remaining / burn if burn > 0 else float("inf")
        candidates.append(("daily", eta_days, remaining))
    if key.monthly_budget_usd:
        remaining = max(key.monthly_budget_usd - spend["month_cost"], 0.0)
        eta_days = remaining / burn if burn > 0 else float("inf")
        candidates.append(("monthly", eta_days, remaining))

    # Report the most urgent (smallest eta_days).
    period, eta_days, remaining = min(candidates, key=lambda c: c[1])

    import datetime as _dt
    if float("inf") == eta_days:
        eta_date = "never"
    else:
        eta_date = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=eta_days)).strftime("%Y-%m-%d")

    return {
        "period": period,
        "eta_days": round(eta_days, 1) if eta_days != float("inf") else None,
        "eta_date": eta_date,
        "burn_rate": round(burn, 6),
        "remaining": round(remaining, 6),
    }


def reset_state() -> None:
    """Test hook."""
    with _lock:
        _hits.clear()
        _spend_cache.clear()
        _burn_cache.clear()

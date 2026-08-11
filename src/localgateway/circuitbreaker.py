"""Per-backend circuit breaker.

Tracks consecutive failures per backend and, when enabled
(``server.circuit_breaker_enabled``), opens a circuit after
``circuit_breaker_threshold`` consecutive failures. An open circuit excludes
the backend from routing for an exponentially growing backoff (base
``circuit_breaker_backoff_s``, doubling per consecutive trip, capped at 30
minutes). The first request after the backoff is a half-open trial: success
closes the circuit, failure re-trips it.

Only genuine backend failures count (5xx, timeouts, connection errors,
mid-stream interruptions) — never client errors (4xx) or 429s, which already
have their own cooldown path in ratelimit.py.

State is process-local (like stats.py) and resets on worker restart, which is
acceptable: a freshly started worker re-earns trust quickly.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

MAX_BACKOFF_S = 1800.0  # 30 min cap


@dataclass
class _Circuit:
    consecutive_failures: int = 0
    open_until: float = 0.0      # monotonic; 0 = closed
    trips: int = 0               # consecutive trips (drives backoff growth)
    last_error: str | None = None
    last_trip_ts: float | None = None


_lock = threading.Lock()
_circuits: dict[str, _Circuit] = {}

# Set by config-aware wrappers; read by the raw record/is_open helpers.
_enabled = False
_threshold = 3
_backoff_s = 60.0


def configure(enabled: bool, threshold: int = 3, backoff_s: float = 60.0) -> None:
    """Apply config. Called on each load so hot-reloads take effect."""
    global _enabled, _threshold, _backoff_s
    _enabled = bool(enabled)
    _threshold = max(1, int(threshold or 3))
    _backoff_s = max(0.05, float(backoff_s or 60.0))


def configure_from_config(cfg) -> None:
    s = getattr(cfg, "server", None)
    if s is None:
        return
    configure(
        getattr(s, "circuit_breaker_enabled", False),
        getattr(s, "circuit_breaker_threshold", 3),
        getattr(s, "circuit_breaker_backoff_s", 60.0),
    )


def _key(provider_id: str, model: str) -> str:
    return f"{provider_id}:{model}"


def _get(key: str) -> _Circuit:
    c = _circuits.get(key)
    if c is None:
        c = _Circuit()
        _circuits[key] = c
    return c


def record_outcome(provider_id: str, model: str, success: bool, error: str | None = None) -> bool:
    """Record a request outcome. Returns True when this call TRIPPED the circuit.

    No-op bookkeeping still runs when the breaker is disabled (so enabling it
    later starts from accurate counts), but tripping only happens when enabled.
    """
    key = _key(provider_id, model)
    now = time.monotonic()
    tripped = False
    with _lock:
        c = _get(key)
        if success:
            c.consecutive_failures = 0
            c.trips = 0
            c.open_until = 0.0
            return False
        c.consecutive_failures += 1
        c.last_error = (error or "")[:300]
        if _enabled and c.consecutive_failures >= _threshold and now >= c.open_until:
            c.trips += 1
            backoff = min(_backoff_s * (2 ** (c.trips - 1)), MAX_BACKOFF_S)
            c.open_until = now + backoff
            c.last_trip_ts = time.time()
            tripped = True
    if tripped:
        try:
            from . import logs
            logs.error(
                f"circuit OPEN on {key} ({_threshold} consecutive failures; backoff {min(_backoff_s * (2 ** (_get(key).trips - 1)), MAX_BACKOFF_S):.0f}s)",
                provider=provider_id,
            )
        except Exception:
            pass
        try:
            from . import alerts
            alerts.send(
                "circuit_open",
                f"Circuit open on {key}",
                {"backend": key, "error": (error or "")[:300]},
            )
        except Exception:
            pass
        try:
            from . import events
            events.bump_state_version()
            events.publish("circuit", {"backend": key, "action": "trip",
                                       "error": (error or "")[:300]})
        except Exception:
            pass
    return tripped


def is_open(provider_id: str, model: str) -> bool:
    """True when the backend's circuit is open (exclude from routing)."""
    if not _enabled:
        return False
    key = _key(provider_id, model)
    with _lock:
        c = _circuits.get(key)
        if c is None or c.open_until <= 0:
            return False
        if time.monotonic() >= c.open_until:
            return False  # backoff expired → half-open trial allowed
        return True


def reset(provider_id: str | None = None, model: str | None = None) -> int:
    """Manually close circuits. With provider+model, resets one backend;
    otherwise resets all. Returns entries cleared."""
    with _lock:
        if provider_id and model:
            key = _key(provider_id, model)
            if key in _circuits:
                del _circuits[key]
                _post_reset_event(key)
                return 1
            return 0
        n = len(_circuits)
        keys = list(_circuits.keys())
        _circuits.clear()
    for k in keys:
        _post_reset_event(k)
    return n


def _post_reset_event(key: str) -> None:
    try:
        from . import events
        events.bump_state_version()
        events.publish("circuit", {"backend": key, "action": "reset"})
    except Exception:
        pass


def snapshot() -> dict[str, dict]:
    """Serializable view for /admin endpoints and the dashboard."""
    now = time.monotonic()
    with _lock:
        out: dict[str, dict] = {}
        for key, c in _circuits.items():
            open_remaining = max(0.0, c.open_until - now) if c.open_until > now else 0.0
            out[key] = {
                "consecutive_failures": c.consecutive_failures,
                "open": bool(_enabled and open_remaining > 0),
                "open_remaining_s": round(open_remaining, 1),
                "trips": c.trips,
                "last_error": c.last_error,
                "last_trip_ts": c.last_trip_ts,
            }
        return out


def reset_state() -> None:
    """Test hook: clear everything, including config."""
    global _enabled, _threshold, _backoff_s
    with _lock:
        _circuits.clear()
    _enabled = False
    _threshold = 3
    _backoff_s = 60.0

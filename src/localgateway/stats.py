from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

WINDOW = 50


@dataclass
class _BackendStats:
    latencies: deque = field(default_factory=lambda: deque(maxlen=WINDOW))
    ttfts: deque = field(default_factory=lambda: deque(maxlen=WINDOW))
    successes: int = 0
    failures: int = 0
    last_error: str | None = None
    last_error_ts: float | None = None
    last_success_ts: float | None = None
    in_flight: int = 0


@dataclass
class WarmthEntry:
    last_seen_ts: float
    hit_count: int = 0
    write_count: int = 0
    miss_count: int = 0
    last_hit_rate: float = 0.0  # 0..1


_lock = threading.Lock()
_stats: dict[str, _BackendStats] = {}
# cache_key -> { backend_key -> WarmthEntry }
_warmth: dict[str, dict[str, WarmthEntry]] = {}


def _key(provider_id: str, backend_model: str) -> str:
    return f"{provider_id}:{backend_model}"


def _get(key: str) -> _BackendStats:
    st = _stats.get(key)
    if st is None:
        st = _BackendStats()
        _stats[key] = st
    return st


def start_request(provider_id: str, backend_model: str) -> None:
    key = _key(provider_id, backend_model)
    with _lock:
        _get(key).in_flight += 1


def end_request(provider_id: str, backend_model: str) -> None:
    key = _key(provider_id, backend_model)
    with _lock:
        st = _get(key)
        if st.in_flight > 0:
            st.in_flight -= 1


def record_success(
    provider_id: str,
    backend_model: str,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
) -> None:
    key = _key(provider_id, backend_model)
    with _lock:
        st = _get(key)
        st.successes += 1
        st.last_success_ts = time.time()
        if latency_ms is not None:
            st.latencies.append(latency_ms)
        if ttft_ms is not None:
            st.ttfts.append(ttft_ms)


def record_failure(provider_id: str, backend_model: str, error: str | None = None) -> None:
    key = _key(provider_id, backend_model)
    with _lock:
        st = _get(key)
        st.failures += 1
        st.last_error = (error or "")[:300]
        st.last_error_ts = time.time()


def record_cache_activity(
    provider_id: str,
    backend_model: str,
    cache_key: str | None,
    cached_tokens: int | None,
    cache_write_tokens: int | None,
    input_tokens: int | None,
    cache_supported: bool | None,
) -> None:
    """Register cache warmth for a backend keyed by the request fingerprint.

    Warmth is registered on any successful response from a cache-capable
    backend (cache_supported True or auto-detected via observed cache
    tokens). Observed cached_tokens / cache_write_tokens reinforce the entry
    and refresh its TTL.
    """
    if not cache_key:
        return
    backend_key = _key(provider_id, backend_model)

    # Determine whether this backend participates in cache affinity.
    supports = cache_supported
    if supports is None:
        # Auto-detect: a backend that ever reports cache tokens is cache-capable.
        supports = bool(cached_tokens or cache_write_tokens)
    if supports is False:
        return

    now = time.time()
    cached = cached_tokens or 0
    cache_write = cache_write_tokens or 0
    inp = input_tokens or 0

    with _lock:
        bucket = _warmth.setdefault(cache_key, {})
        entry = bucket.get(backend_key)
        if entry is None:
            entry = WarmthEntry(last_seen_ts=now)
            bucket[backend_key] = entry
        entry.last_seen_ts = now
        if cached > 0:
            entry.hit_count += 1
        elif cache_write > 0:
            entry.write_count += 1
        elif inp > 0:
            entry.miss_count += 1
        total = entry.hit_count + entry.miss_count
        entry.last_hit_rate = (entry.hit_count / total) if total else 0.0


def warm_backends_for(
    cache_key: str,
    candidates: list[str],
    ttl_sec: float,
) -> list[str]:
    """Return warm candidate backend keys for a cache_key, ordered by
    recency then hit-rate. Non-expired entries only. Lazy TTL sweep."""
    if not cache_key or not candidates:
        return []
    now = time.time()
    cand_set = set(candidates)
    with _lock:
        bucket = _warmth.get(cache_key)
        if not bucket:
            return []
        # Lazy sweep of expired entries for this key.
        expired = [bk for bk, e in bucket.items() if now - e.last_seen_ts > ttl_sec]
        for bk in expired:
            del bucket[bk]
        if not bucket:
            del _warmth[cache_key]
            return []
        warm = [
            (bk, e) for bk, e in bucket.items() if bk in cand_set
        ]
        warm.sort(key=lambda be: (-be[1].last_hit_rate, -be[1].last_seen_ts))
        return [bk for bk, _ in warm]


def clear_warmth() -> None:
    with _lock:
        _warmth.clear()


def _avg(dq: deque) -> float | None:
    if not dq:
        return None
    return round(sum(dq) / len(dq), 1)


def snapshot() -> dict[str, dict]:
    with _lock:
        out: dict[str, dict] = {}
        for key, st in _stats.items():
            total = st.successes + st.failures
            out[key] = {
                "requests": total,
                "successes": st.successes,
                "failures": st.failures,
                "success_rate": round(st.successes / total * 100, 1) if total else None,
                "avg_latency_ms": _avg(st.latencies),
                "avg_ttft_ms": _avg(st.ttfts),
                "last_error": st.last_error,
                "last_error_ts": st.last_error_ts,
                "last_success_ts": st.last_success_ts,
                "in_flight": st.in_flight,
            }
        return out


def warmth_snapshot() -> dict[str, dict]:
    """Return a serializable view of the warmth registry for observability."""
    with _lock:
        out: dict[str, dict] = {}
        for cache_key, bucket in _warmth.items():
            out[cache_key] = {
                bk: {
                    "last_seen_ts": e.last_seen_ts,
                    "hit_count": e.hit_count,
                    "write_count": e.write_count,
                    "miss_count": e.miss_count,
                    "last_hit_rate": round(e.last_hit_rate, 3),
                }
                for bk, e in bucket.items()
            }
        return out


def reset() -> None:
    with _lock:
        _stats.clear()
        _warmth.clear()

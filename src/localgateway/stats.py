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


_lock = threading.Lock()
_stats: dict[str, _BackendStats] = {}


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


def reset() -> None:
    with _lock:
        _stats.clear()

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_db_path: Path = Path("data/usage.db")
_db_lock = threading.Lock()
_initialized = False


def set_db_path(path: str | Path) -> None:
    global _db_path
    _db_path = Path(path)


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init() -> None:
    global _initialized
    if _initialized:
        return
    with _db_lock:
        if _initialized:
            return
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    logical_model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    backend_model TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost REAL,
                    latency_ms INTEGER,
                    stream INTEGER
                )
                """
            )
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(usage)")}
            if "ttft_ms" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN ttft_ms INTEGER")
            if "reasoning_tokens" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN reasoning_tokens INTEGER")
            if "tps" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN tps REAL")
            if "cached_tokens" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN cached_tokens INTEGER")
            if "cache_write_tokens" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN cache_write_tokens INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider)"
            )
        _initialized = True


def log_request(
    *,
    logical_model: str,
    provider: str,
    backend_model: str,
    success: bool,
    error: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cached_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cost: float | None = None,
    latency_ms: int | None = None,
    ttft_ms: int | None = None,
    tps: float | None = None,
    stream: bool = False,
) -> None:
    _init()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO usage
                  (ts, logical_model, provider, backend_model, success, error,
                   input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens, cost, latency_ms, ttft_ms, tps, stream)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    logical_model,
                    provider,
                    backend_model,
                    1 if success else 0,
                    error,
                    input_tokens,
                    output_tokens,
                    reasoning_tokens,
                    cached_tokens,
                    cache_write_tokens,
                    cost,
                    latency_ms,
                    ttft_ms,
                    tps,
                    1 if stream else 0,
                ),
            )


_AGG_COLS = """
                  COUNT(*) as requests,
                  SUM(success) as successes,
                  SUM(input_tokens) as input_tokens,
                  SUM(output_tokens) as output_tokens,
                  SUM(reasoning_tokens) as reasoning_tokens,
                  SUM(cached_tokens) as cached_tokens,
                  SUM(cache_write_tokens) as cache_write_tokens,
                  AVG(CASE WHEN success = 1 AND ttft_ms IS NOT NULL THEN ttft_ms END) as avg_ttft_ms,
                  AVG(CASE WHEN success = 1 AND latency_ms IS NOT NULL THEN latency_ms END) as avg_latency_ms,
                  SUM(cost) as cost
"""


def _row(r):
    return {
        "requests": r["requests"] or 0,
        "successes": r["successes"] or 0,
        "input_tokens": r["input_tokens"] or 0,
        "output_tokens": r["output_tokens"] or 0,
        "reasoning_tokens": r["reasoning_tokens"] or 0,
        "cached_tokens": r["cached_tokens"] or 0,
        "cache_write_tokens": r["cache_write_tokens"] or 0,
        "avg_ttft_ms": round(r["avg_ttft_ms"], 1) if r["avg_ttft_ms"] is not None else None,
        "avg_latency_ms": round(r["avg_latency_ms"], 1) if r["avg_latency_ms"] is not None else None,
        "cost": round(r["cost"] or 0.0, 6),
    }


def get_usage_summary(hours: int = 24) -> dict:
    _init()
    cutoff = time.time() - hours * 3600
    with _db_lock:
        with _connect() as conn:
            total = conn.execute(
                f"SELECT {_AGG_COLS} FROM usage WHERE ts >= ?", (cutoff,)
            ).fetchone()

            by_provider = conn.execute(
                f"SELECT provider, {_AGG_COLS} FROM usage WHERE ts >= ? GROUP BY provider",
                (cutoff,),
            ).fetchall()

            by_model = conn.execute(
                f"SELECT logical_model, {_AGG_COLS} FROM usage WHERE ts >= ? GROUP BY logical_model",
                (cutoff,),
            ).fetchall()

            by_backend = conn.execute(
                f"""SELECT provider || ':' || backend_model as backend, {_AGG_COLS}
                    FROM usage WHERE ts >= ? GROUP BY backend ORDER BY requests DESC""",
                (cutoff,),
            ).fetchall()

    return {
        "hours": hours,
        "total": _row(total),
        "by_provider": {r["provider"]: _row(r) for r in by_provider},
        "by_model": {r["logical_model"]: _row(r) for r in by_model},
        "by_backend": {r["backend"]: _row(r) for r in by_backend},
    }


PERCENTILES = {"p50": 0.50, "p90": 0.90, "p99": 0.99}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return round(s[f], 2)
    return round(s[f] + (s[c] - s[f]) * (k - f), 2)


def get_model_aggregates(hours: int = 168) -> dict[str, dict]:
    """Per-model request/token/cost aggregates plus P50 TPS over the window."""
    _init()
    cutoff = time.time() - hours * 3600
    with _db_lock:
        with _connect() as conn:
            agg = conn.execute(
                """
                SELECT logical_model,
                       COUNT(*) as requests,
                       SUM(success) as successes,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost) as cost
                FROM usage WHERE ts >= ? GROUP BY logical_model
                """,
                (cutoff,),
            ).fetchall()
            tps_rows = conn.execute(
                "SELECT logical_model, tps FROM usage WHERE ts >= ? AND success = 1 AND tps IS NOT NULL",
                (cutoff,),
            ).fetchall()

    tps_by_model: dict[str, list[float]] = {}
    for r in tps_rows:
        tps_by_model.setdefault(r["logical_model"], []).append(r["tps"])

    out: dict[str, dict] = {}
    for r in agg:
        m = r["logical_model"]
        out[m] = {
            "requests": r["requests"] or 0,
            "successes": r["successes"] or 0,
            "tokens": (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
            "cost": round(r["cost"] or 0.0, 6),
            "tps_p50": percentile(tps_by_model.get(m, []), 0.5),
            "tps_p90": percentile(tps_by_model.get(m, []), 0.1),
            "tps_p99": percentile(tps_by_model.get(m, []), 0.01),
        }
    return out


def get_backend_percentiles(model_id: str, hours: int = 168, p: float = 0.5) -> dict[str, dict]:
    """Per-backend percentile stats (TTFT/TPS/latency) for one model."""
    _init()
    cutoff = time.time() - hours * 3600
    with _db_lock:
        with _connect() as conn:
            counts = conn.execute(
                """
                SELECT provider || ':' || backend_model as backend,
                       COUNT(*) as requests, SUM(success) as successes
                FROM usage WHERE ts >= ? AND logical_model = ?
                GROUP BY backend
                """,
                (cutoff, model_id),
            ).fetchall()
            rows = conn.execute(
                """
                SELECT provider || ':' || backend_model as backend, ttft_ms, tps, latency_ms
                FROM usage
                WHERE ts >= ? AND logical_model = ? AND success = 1
                """,
                (cutoff, model_id),
            ).fetchall()

    buckets: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        b = buckets.setdefault(r["backend"], {"ttft": [], "tps": [], "latency": []})
        if r["ttft_ms"] is not None:
            b["ttft"].append(r["ttft_ms"])
        if r["tps"] is not None:
            b["tps"].append(r["tps"])
        if r["latency_ms"] is not None:
            b["latency"].append(r["latency_ms"])

    out: dict[str, dict] = {}
    for r in counts:
        key = r["backend"]
        b = buckets.get(key, {"ttft": [], "tps": [], "latency": []})
        req = r["requests"] or 0
        out[key] = {
            "requests": req,
            "success_rate": round((r["successes"] or 0) / req * 100, 2) if req else None,
            "ttft_ms": percentile(b["ttft"], p),
            "tps": percentile(b["tps"], 1.0 - p),
            "tps_p50": percentile(b["tps"], 0.5),
            "tps_p90": percentile(b["tps"], 0.1),
            "tps_p99": percentile(b["tps"], 0.01),
            "latency_ms": percentile(b["latency"], p),
        }
    return out


def get_backend_series(model_id: str, hours: int = 168) -> dict:
    """Hourly-bucketed avg TPS/TTFT per backend, for charts."""
    _init()
    cutoff = time.time() - hours * 3600
    bucket_s = 3600 if hours <= 72 else 6 * 3600
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT CAST(ts / {bucket_s} AS INTEGER) * {bucket_s} as bucket,
                       provider || ':' || backend_model as backend,
                       AVG(tps) as avg_tps, AVG(ttft_ms) as avg_ttft, COUNT(*) as n
                FROM usage
                WHERE ts >= ? AND logical_model = ? AND success = 1 AND tps IS NOT NULL
                GROUP BY bucket, backend ORDER BY bucket
                """,
                (cutoff, model_id),
            ).fetchall()

    series: dict[str, list[dict]] = {}
    for r in rows:
        series.setdefault(r["backend"], []).append({
            "t": int(r["bucket"]),
            "tps": round(r["avg_tps"], 1),
            "ttft_ms": round(r["avg_ttft"], 1) if r["avg_ttft"] is not None else None,
            "n": r["n"],
        })
    return {"bucket_seconds": bucket_s, "series": series}
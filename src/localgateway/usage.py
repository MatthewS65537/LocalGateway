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
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
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
            if "is_probe" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN is_probe INTEGER DEFAULT 0")
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
    is_probe: bool = False,
) -> None:
    _init()
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO usage
                  (ts, logical_model, provider, backend_model, success, error,
                   input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens, cost, latency_ms, ttft_ms, tps, stream, is_probe)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if is_probe else 0,
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


# Exclude probe requests (is_probe=1) from all user-facing usage aggregates so
# background health probes don't distort token counts, cost, or TPS percentiles.
PROBE_EXCLUDE = " AND COALESCE(is_probe, 0) = 0 "


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
                f"SELECT {_AGG_COLS} FROM usage WHERE ts >= ?{PROBE_EXCLUDE}", (cutoff,)
            ).fetchone()

            by_provider = conn.execute(
                f"SELECT provider, {_AGG_COLS} FROM usage WHERE ts >= ?{PROBE_EXCLUDE} GROUP BY provider",
                (cutoff,),
            ).fetchall()

            by_model = conn.execute(
                f"SELECT logical_model, {_AGG_COLS} FROM usage WHERE ts >= ?{PROBE_EXCLUDE} GROUP BY logical_model",
                (cutoff,),
            ).fetchall()

            by_backend = conn.execute(
                f"""SELECT provider || ':' || backend_model as backend, {_AGG_COLS}
                    FROM usage WHERE ts >= ?{PROBE_EXCLUDE} GROUP BY backend ORDER BY requests DESC""",
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
                f"""
                SELECT logical_model,
                       COUNT(*) as requests,
                       SUM(success) as successes,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost) as cost
                FROM usage WHERE ts >= ?{PROBE_EXCLUDE} GROUP BY logical_model
                """,
                (cutoff,),
            ).fetchall()
            tps_rows = conn.execute(
                f"SELECT logical_model, tps FROM usage WHERE ts >= ?{PROBE_EXCLUDE} AND success = 1 AND tps IS NOT NULL",
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
                f"""
                SELECT provider || ':' || backend_model as backend,
                       COUNT(*) as requests, SUM(success) as successes
                FROM usage WHERE ts >= ?{PROBE_EXCLUDE} AND logical_model = ?
                GROUP BY backend
                """,
                (cutoff, model_id),
            ).fetchall()
            rows = conn.execute(
                f"""
                SELECT provider || ':' || backend_model as backend, ttft_ms, tps, latency_ms
                FROM usage
                WHERE ts >= ?{PROBE_EXCLUDE} AND logical_model = ? AND success = 1
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


def get_model_totals(model_id: str, hours: int = 168) -> dict:
    """Aggregate token/cost totals for one logical model over a window."""
    _init()
    cutoff = time.time() - hours * 3600
    with _db_lock:
        with _connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) as requests,
                       SUM(success) as successes,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost) as cost
                FROM usage
                WHERE ts >= ?{PROBE_EXCLUDE} AND logical_model = ?
                """,
                (cutoff, model_id),
            ).fetchone()
    req = row["requests"] or 0
    return {
        "requests": req,
        "success_rate": round((row["successes"] or 0) / req * 100, 2) if req else None,
        "input_tokens": row["input_tokens"] or 0,
        "output_tokens": row["output_tokens"] or 0,
        "tokens": (row["input_tokens"] or 0) + (row["output_tokens"] or 0),
        "cost": row["cost"] or 0.0,
    }


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
                WHERE ts >= ?{PROBE_EXCLUDE} AND logical_model = ? AND success = 1 AND tps IS NOT NULL
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


def clear_usage() -> None:
    """Delete all usage rows (keep schema)."""
    _init()
    with _db_lock:
        with _connect() as conn:
            conn.execute("DELETE FROM usage")


def enforce_retention(usage_days: int | None, log_lines: int | None) -> None:
    """Delete old usage rows and cap log table size, then vacuum.

    Call periodically (e.g. on worker startup). Values <= 0 keep everything.
    """
    from . import logs as _logs
    _init()
    _logs._init()
    with _db_lock:
        with _connect() as conn:
            if usage_days and usage_days > 0:
                cutoff = time.time() - usage_days * 86400
                conn.execute("DELETE FROM usage WHERE ts < ?", (cutoff,))
            if log_lines and log_lines > 0:
                # Keep newest log_lines by id.
                conn.execute(
                    f"DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT {int(log_lines)})"
                )
            conn.commit()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            conn.execute("VACUUM")


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def rename_model(old_id: str, new_id: str) -> int:
    """Rename a logical model across all tables. Returns total rows updated."""
    _init()
    from . import logs as _logs
    _logs._init()
    total = 0
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE usage SET logical_model = ? WHERE logical_model = ?",
                (new_id, old_id),
            )
            total += cur.rowcount
            if _table_exists(conn, "logs"):
                cur = conn.execute(
                    "UPDATE logs SET model = ? WHERE model = ?",
                    (new_id, old_id),
                )
                total += cur.rowcount
            if _table_exists(conn, "probes"):
                cur = conn.execute(
                    "UPDATE probes SET model_id = ? WHERE model_id = ?",
                    (new_id, old_id),
                )
                total += cur.rowcount
            conn.commit()
    return total


def rename_provider(old_id: str, new_id: str) -> int:
    """Rename a provider across all tables. Returns total rows updated."""
    _init()
    from . import logs as _logs
    _logs._init()
    total = 0
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE usage SET provider = ? WHERE provider = ?",
                (new_id, old_id),
            )
            total += cur.rowcount
            if _table_exists(conn, "logs"):
                cur = conn.execute(
                    "UPDATE logs SET provider = ? WHERE provider = ?",
                    (new_id, old_id),
                )
                total += cur.rowcount
            if _table_exists(conn, "probes"):
                cur = conn.execute(
                    "UPDATE probes SET provider = ? WHERE provider = ?",
                    (new_id, old_id),
                )
                total += cur.rowcount
            conn.commit()
    return total
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .dbwriter import BackgroundWriter

_db_path: Path = Path("data/usage.db")
_db_lock = threading.Lock()
_initialized = False
_writer: BackgroundWriter | None = None


def set_db_path(path: str | Path) -> None:
    global _db_path, _writer
    _db_path = Path(path)
    # Start the background writer for non-blocking INSERTs on the request path.
    if _writer is not None:
        _writer.stop()
    _writer = BackgroundWriter(_db_path)
    _writer.start()


def stop_writer() -> None:
    """Flush and stop the background writer (called on shutdown)."""
    global _writer
    if _writer is not None:
        _writer.stop()
        _writer = None


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
            if "api_key_id" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN api_key_id TEXT")
            if "end_user" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN end_user TEXT")
            if "request_id" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN request_id TEXT")
            if "cache_hit" not in existing:
                conn.execute("ALTER TABLE usage ADD COLUMN cache_hit INTEGER DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage(api_key_id, ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_req ON usage(request_id)"
            )
            # N2: anomaly detection table (shared between worker + supervisor)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS anomalies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    expected REAL NOT NULL,
                    actual REAL NOT NULL,
                    z_score REAL,
                    acknowledged INTEGER DEFAULT 0,
                    UNIQUE(scope, scope_id, date)
                )
                """
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
    api_key_id: str | None = None,
    end_user: str | None = None,
    request_id: str | None = None,
    cache_hit: bool = False,
) -> None:
    _init()
    # P1: enqueue the INSERT to the background writer so the event loop never
    # blocks on SQLite. Reads (aggregates, summaries) are still synchronous
    # and see committed data within ~100ms (the batch flush interval).
    if _writer is not None:
        _writer.enqueue(
            """
            INSERT INTO usage
              (ts, logical_model, provider, backend_model, success, error,
               input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens, cost, latency_ms, ttft_ms, tps, stream, is_probe,
               api_key_id, end_user, request_id, cache_hit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                api_key_id,
                end_user,
                request_id,
                1 if cache_hit else 0,
            ),
        )
        return
    # Fallback: synchronous write (tests or pre-start)
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO usage
                  (ts, logical_model, provider, backend_model, success, error,
                   input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens, cost, latency_ms, ttft_ms, tps, stream, is_probe,
                   api_key_id, end_user, request_id, cache_hit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    api_key_id,
                    end_user,
                    request_id,
                    1 if cache_hit else 0,
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

            by_key = conn.execute(
                f"""SELECT api_key_id as k, {_AGG_COLS}
                    FROM usage WHERE ts >= ? AND api_key_id IS NOT NULL{PROBE_EXCLUDE} GROUP BY k""",
                (cutoff,),
            ).fetchall()

            by_user = conn.execute(
                f"""SELECT end_user as u, {_AGG_COLS}
                    FROM usage WHERE ts >= ? AND end_user IS NOT NULL AND end_user != ''{PROBE_EXCLUDE} GROUP BY u""",
                (cutoff,),
            ).fetchall()

    return {
        "hours": hours,
        "total": _row(total),
        "by_provider": {r["provider"]: _row(r) for r in by_provider},
        "by_model": {r["logical_model"]: _row(r) for r in by_model},
        "by_backend": {r["backend"]: _row(r) for r in by_backend},
        "by_key": {r["k"]: _row(r) for r in by_key},
        "by_user": {r["u"]: _row(r) for r in by_user},
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
            "tps_p90": percentile(tps_by_model.get(m, []), 0.9),
            "tps_p99": percentile(tps_by_model.get(m, []), 0.99),
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
            "tps_p90": percentile(b["tps"], 0.9),
            "tps_p99": percentile(b["tps"], 0.99),
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


def get_daily_series(hours: int = 168) -> dict:
    """Daily-bucketed cost + tokens per provider, for usage-page charts."""
    _init()
    cutoff = time.time() - hours * 3600
    bucket_s = 86400 if hours > 48 else 3600
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT CAST(ts / {bucket_s} AS INTEGER) * {bucket_s} as bucket,
                       provider,
                       SUM(cost) as cost,
                       SUM(input_tokens) + SUM(output_tokens) as tokens,
                       COUNT(*) as requests
                FROM usage
                WHERE ts >= ?{PROBE_EXCLUDE}
                GROUP BY bucket, provider ORDER BY bucket
                """,
                (cutoff,),
            ).fetchall()
    series: dict[str, list[dict]] = {}
    for r in rows:
        series.setdefault(r["provider"], []).append({
            "t": int(r["bucket"]),
            "cost": round(r["cost"] or 0.0, 6),
            "tokens": r["tokens"] or 0,
            "requests": r["requests"] or 0,
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

    P6: this is fully synchronous and used to run directly on the event loop —
    a full VACUUM rewrites the DB and blocked every in-flight request for
    seconds, once at startup and once a day (and dbwriter batches could hit
    "database is locked" and silently drop rows while it ran). Callers now run
    it in a thread (asyncio.to_thread); VACUUM is skipped unless the DB shrank
    materially, since checkpointing already reclaims WAL space.
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
            # Skip VACUUM unless >5% of the DB can actually be reclaimed —
            # it's a full rewrite that can stall concurrent writers.
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
                if freelist and page_count and freelist / page_count > 0.05:
                    conn.execute("VACUUM")
            except sqlite3.Error:
                pass


# ---- routing stats + governance helpers ----

_tps_map_cache: dict[tuple[str, int], tuple[float, dict[str, float]]] = {}
_TPS_MAP_TTL = 30.0


def get_backend_tps_map(model_id: str, hours: int = 6) -> dict[str, float]:
    """Measured TPS (p50) per backend for one model, for stats-driven routing.

    Includes probe rows (they are direct throughput measurements and usually
    the freshest data). Cached briefly — this runs on the request hot path.
    """
    key = (model_id, hours)
    now = time.time()
    hit = _tps_map_cache.get(key)
    if hit and now - hit[0] < _TPS_MAP_TTL:
        return hit[1]
    _init()
    cutoff = now - hours * 3600
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT provider || ':' || backend_model as backend, tps
                FROM usage
                WHERE ts >= ? AND logical_model = ? AND success = 1 AND tps IS NOT NULL
                """,
                (cutoff, model_id),
            ).fetchall()
    by_backend: dict[str, list[float]] = {}
    for r in rows:
        by_backend.setdefault(r["backend"], []).append(r["tps"])
    out = {b: percentile(v, 0.5) for b, v in by_backend.items()}
    out = {b: v for b, v in out.items() if v is not None}
    _tps_map_cache[key] = (now, out)
    return out


def invalidate_tps_map() -> None:
    _tps_map_cache.clear()


def get_key_spend(key_id: str) -> dict:
    """Spend totals for one API key: today (UTC) and this month (UTC)."""
    _init()
    lt = time.localtime()
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    with _db_lock:
        with _connect() as conn:
            day = conn.execute(
                f"SELECT SUM(cost) as c, COUNT(*) as n FROM usage WHERE api_key_id = ? AND ts >= ?{PROBE_EXCLUDE}",
                (key_id, day_start),
            ).fetchone()
            month = conn.execute(
                f"SELECT SUM(cost) as c, COUNT(*) as n FROM usage WHERE api_key_id = ? AND ts >= ?{PROBE_EXCLUDE}",
                (key_id, month_start),
            ).fetchone()
    return {
        "day_cost": round(day["c"] or 0.0, 6),
        "day_requests": day["n"] or 0,
        "month_cost": round(month["c"] or 0.0, 6),
        "month_requests": month["n"] or 0,
    }


def get_all_key_spend() -> dict[str, dict]:
    """Spend totals for every API key seen in usage (plus config keys)."""
    _init()
    lt = time.localtime()
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT api_key_id,
                       SUM(CASE WHEN ts >= ? THEN cost ELSE 0 END) as day_cost,
                       SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) as day_requests,
                       SUM(cost) as month_cost,
                       COUNT(*) as month_requests
                FROM usage
                WHERE api_key_id IS NOT NULL AND ts >= ?{PROBE_EXCLUDE}
                GROUP BY api_key_id
                """,
                (day_start, day_start, month_start),
            ).fetchall()
    return {
        r["api_key_id"]: {
            "day_cost": round(r["day_cost"] or 0.0, 6),
            "day_requests": r["day_requests"] or 0,
            "month_cost": round(r["month_cost"] or 0.0, 6),
            "month_requests": r["month_requests"] or 0,
        }
        for r in rows
    }


def get_key_daily_burn(key_id: str, days: int = 7) -> float:
    """Average daily cost for a key over the trailing N days (UTC midnight
    buckets). Returns 0.0 when there's no spend. Used by burn-rate forecasting
    (F4) to project budget exhaustion."""
    _init()
    cutoff = time.time() - days * 86400
    with _db_lock:
        with _connect() as conn:
            row = conn.execute(
                f"""
                SELECT SUM(cost) as total, COUNT(DISTINCT CAST(ts / 86400 AS INTEGER)) as active_days
                FROM usage
                WHERE api_key_id = ? AND ts >= ?{PROBE_EXCLUDE}
                """,
                (key_id, cutoff),
            ).fetchone()
    total = row["total"] or 0.0
    active_days = row["active_days"] or 0
    # B8: divide by the key's actual active days, not the full window — a key
    # created yesterday that spent $10 burns $10/day, not $1.43/day. The old
    # behavior under-reported exhaustion up to 7× for new keys, breaking
    # X-Budget-ETA and the eta<3d alert in the dangerous direction.
    if active_days <= 0:
        return 0.0
    return round(total / active_days, 6)


def get_user_spend(hours: int = 720) -> dict[str, dict]:
    """Per end-user (OpenAI `user` field) cost/request totals."""
    _init()
    cutoff = time.time() - hours * 3600
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT end_user, COUNT(*) as requests, SUM(cost) as cost,
                       SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens
                FROM usage
                WHERE ts >= ? AND end_user IS NOT NULL AND end_user != ''{PROBE_EXCLUDE}
                GROUP BY end_user ORDER BY cost DESC
                """,
                (cutoff,),
            ).fetchall()
    return {
        r["end_user"]: {
            "requests": r["requests"] or 0,
            "cost": round(r["cost"] or 0.0, 6),
            "tokens": (r["input_tokens"] or 0) + (r["output_tokens"] or 0),
        }
        for r in rows
    }


EXPORT_COLUMNS = [
    "ts", "logical_model", "provider", "backend_model", "success", "error",
    "input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
    "cache_write_tokens", "cost", "latency_ms", "ttft_ms", "tps", "stream",
    "is_probe", "api_key_id", "end_user", "request_id", "cache_hit",
]


def export_rows(hours: int = 720, include_probes: bool = False) -> list[dict]:
    """All usage rows in a window, newest first, for CSV/JSON export.

    Probes are excluded by default (they're health checks, not user traffic)
    so billing analysis downloads aren't polluted. Set include_probes=True to
    include them (the is_probe column is present for filtering either way).
    """
    _init()
    cutoff = time.time() - hours * 3600
    probe_clause = "" if include_probes else " AND COALESCE(is_probe, 0) = 0"
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM usage WHERE ts >= ?{probe_clause} ORDER BY ts DESC",
                (cutoff,),
            ).fetchall()
    return [{c: r[c] for c in EXPORT_COLUMNS} for r in rows]


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
    invalidate_tps_map()
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
    invalidate_tps_map()
    return total


# ---------- N2: cost anomaly detection ----------

ANOMALY_BASELINE_DAYS = 14
ANOMALY_Z_THRESHOLD = 3.0
ANOMALY_MULT_THRESHOLD = 3.0
ANOMALY_MIN_SPEND = 0.10


def daily_anomaly_scan() -> list[dict]:
    """Detect cost anomalies by comparing yesterday's spend against a trailing
    14-day baseline, per key and per model.

    Uses complete UTC epoch-days only (avoids partial-day noise). Flags when
    ``actual > max(mean + z*std, mean * mult, min_spend)``. Results are stored
    in the ``anomalies`` table and returned as a list of dicts.
    """
    _init()
    import math
    now = time.time()
    today_start = int(now) // 86400 * 86400
    yesterday_start = today_start - 86400
    baseline_start = today_start - ANOMALY_BASELINE_DAYS * 86400

    found: list[dict] = []

    with _db_lock:
        with _connect() as conn:
            for scope, group_col in [("key", "api_key_id"), ("model", "logical_model")]:
                rows = conn.execute(
                    f"""
                    SELECT {group_col} as sid,
                           CAST(ts / 86400 AS INTEGER) as day,
                           SUM(cost) as daily_cost
                    FROM usage
                    WHERE ts >= ? AND ts < ?
                      AND {group_col} IS NOT NULL AND {group_col} != ''
                      {PROBE_EXCLUDE}
                    GROUP BY {group_col}, day
                    """,
                    (baseline_start, yesterday_start),
                ).fetchall()

                baselines: dict[str, list[float]] = {}
                for r in rows:
                    baselines.setdefault(r["sid"], []).append(r["daily_cost"] or 0)

                y_rows = conn.execute(
                    f"""
                    SELECT {group_col} as sid, SUM(cost) as total
                    FROM usage
                    WHERE ts >= ? AND ts < ?
                      AND {group_col} IS NOT NULL AND {group_col} != ''
                      {PROBE_EXCLUDE}
                    GROUP BY {group_col}
                    """,
                    (yesterday_start, today_start),
                ).fetchall()

                date_str = time.strftime("%Y-%m-%d", time.gmtime(yesterday_start))
                for r in y_rows:
                    sid = r["sid"]
                    actual = r["total"] or 0
                    daily = baselines.get(sid, [])
                    if len(daily) < 3:
                        continue
                    mean = sum(daily) / len(daily)
                    if mean < ANOMALY_MIN_SPEND:
                        continue
                    variance = sum((x - mean) ** 2 for x in daily) / len(daily)
                    std = math.sqrt(variance) if variance > 0 else 0
                    z = (actual - mean) / std if std > 0 else (float("inf") if actual > mean else 0)
                    threshold = max(mean + ANOMALY_Z_THRESHOLD * std, mean * ANOMALY_MULT_THRESHOLD)
                    if actual > threshold:
                        anomaly = {
                            "scope": scope,
                            "scope_id": sid,
                            "date": date_str,
                            "expected": round(mean, 6),
                            "actual": round(actual, 6),
                            "z_score": round(z, 2) if z != float("inf") else 999.0,
                        }
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO anomalies (ts, scope, scope_id, date, expected, actual, z_score)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (now, anomaly["scope"], anomaly["scope_id"], anomaly["date"],
                             anomaly["expected"], anomaly["actual"], anomaly["z_score"]),
                        )
                        if cur.rowcount > 0:
                            found.append(anomaly)
            if found:
                conn.commit()

    try:
        from . import alerts
        for a in found:
            alerts.send(
                "cost_anomaly",
                f"Cost anomaly: {a['scope']} '{a['scope_id']}' spent ${a['actual']:.4f} "
                f"on {a['date']} (expected ${a['expected']:.4f}, z={a['z_score']})",
                {"scope": a["scope"], "scope_id": a["scope_id"], "date": a["date"],
                 "expected": a["expected"], "actual": a["actual"], "z_score": a["z_score"]},
                dedup_key=f"anomaly:{a['scope']}:{a['scope_id']}:{a['date']}",
            )
    except Exception:
        pass

    return found


def get_active_anomalies() -> list[dict]:
    """Return unacknowledged anomalies (for the dashboard banner)."""
    _init()
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT id, ts, scope, scope_id, date, expected, actual, z_score
                   FROM anomalies WHERE acknowledged = 0
                   ORDER BY ts DESC LIMIT 20"""
            ).fetchall()
            return [dict(r) for r in rows]


def ack_anomaly(anomaly_id: int) -> bool:
    """Acknowledge (dismiss) an anomaly. Returns True if a row was updated."""
    _init()
    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE anomalies SET acknowledged = 1 WHERE id = ?", (anomaly_id,)
            )
            conn.commit()
            return cur.rowcount > 0
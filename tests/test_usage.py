import sqlite3
import time

from localgateway import usage


def _fresh(tmp_path):
    usage._initialized = False
    usage.set_db_path(str(tmp_path / "u.db"))
    usage.stop_writer()  # force sync writes for deterministic test reads


def test_migration_adds_new_columns(tmp_path):
    db = tmp_path / "u.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, logical_model TEXT,
            provider TEXT, backend_model TEXT, success INTEGER, error TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cost REAL,
            latency_ms INTEGER, stream INTEGER)"""
    )
    conn.commit()
    conn.close()
    usage._initialized = False
    usage.set_db_path(str(db))
    usage.log_request(
        logical_model="m", provider="p", backend_model="b", success=True,
        ttft_ms=5, tps=100.0, reasoning_tokens=3,
    )
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage)")}
    assert {"ttft_ms", "reasoning_tokens", "tps"} <= cols
    conn.close()


def test_percentile():
    assert usage.percentile([], 0.5) is None
    assert usage.percentile([10], 0.99) == 10
    assert usage.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert usage.percentile(list(range(1, 101)), 0.9) == 90.1


def test_summary_includes_reasoning_and_backend(tmp_path):
    _fresh(tmp_path)
    usage.log_request(
        logical_model="m", provider="p1", backend_model="b1", success=True,
        input_tokens=10, output_tokens=20, reasoning_tokens=5, cost=0.5,
        latency_ms=100, ttft_ms=10, tps=200.0, stream=True,
    )
    usage.log_request(
        logical_model="m", provider="p1", backend_model="b1", success=False,
        error="boom", latency_ms=50,
    )
    s = usage.get_usage_summary(hours=24)
    assert s["total"]["requests"] == 2
    assert s["total"]["reasoning_tokens"] == 5
    assert s["by_backend"]["p1:b1"]["requests"] == 2
    assert s["by_backend"]["p1:b1"]["avg_ttft_ms"] == 10


def test_backend_percentiles(tmp_path):
    _fresh(tmp_path)
    for i, (ttft, tps, lat) in enumerate([(10, 100, 500), (20, 200, 600), (30, 300, 700)]):
        usage.log_request(
            logical_model="m", provider="p", backend_model="b", success=True,
            input_tokens=1, output_tokens=1, latency_ms=lat, ttft_ms=ttft, tps=float(tps),
        )
    stats = usage.get_backend_percentiles("m", hours=24, p=0.5)
    row = stats["p:b"]
    assert row["requests"] == 3
    assert row["success_rate"] == 100.0
    assert row["ttft_ms"] == 20
    assert row["tps"] == 200
    assert row["latency_ms"] == 600


def test_backend_series_buckets(tmp_path):
    _fresh(tmp_path)
    now = time.time()
    usage.log_request(
        logical_model="m", provider="p", backend_model="b", success=True,
        input_tokens=1, output_tokens=10, latency_ms=100, ttft_ms=10, tps=100.0,
    )
    series = usage.get_backend_series("m", hours=24)
    assert series["bucket_seconds"] == 3600
    pts = series["series"]["p:b"]
    assert len(pts) == 1
    assert pts[0]["tps"] == 100.0
    assert pts[0]["t"] <= now


def test_model_aggregates(tmp_path):
    _fresh(tmp_path)
    usage.log_request(
        logical_model="m", provider="p", backend_model="b", success=True,
        input_tokens=10, output_tokens=30, cost=0.1, latency_ms=1000, tps=30.0,
    )
    agg = usage.get_model_aggregates(hours=24)
    row = agg["m"]
    assert row["tokens"] == 40
    assert row["tps_p50"] == 30.0
    assert row["requests"] == 1


def test_tps_percentiles_are_ordered_and_high(tmp_path):
    """Regression for B1: tps_p90/tps_p99 were inverted (computed with 0.1/0.01,
    i.e. the slow tail). They must be the *fast* tail, so p99 >= p90 >= p50."""
    _fresh(tmp_path)
    # 20 samples with a wide spread of TPS values.
    for i in range(20):
        usage.log_request(
            logical_model="m", provider="p", backend_model="b", success=True,
            input_tokens=1, output_tokens=1, tps=float(i * 10),  # 0..190
        )
    agg = usage.get_model_aggregates(hours=24)["m"]
    assert agg["tps_p50"] == 95.0
    # p90 = ~171 (fast tail); the old bug returned ~9 (p10).
    assert agg["tps_p90"] >= agg["tps_p50"]
    assert agg["tps_p99"] >= agg["tps_p90"]
    assert agg["tps_p90"] > 150.0
    assert agg["tps_p99"] > 180.0

    bp = usage.get_backend_percentiles("m", hours=24)["p:b"]
    assert bp["tps_p90"] >= bp["tps_p50"]
    assert bp["tps_p99"] >= bp["tps_p90"]
    assert bp["tps_p90"] > 150.0
    assert bp["tps_p99"] > 180.0

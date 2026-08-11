"""Phase D — N2 cost anomaly detection tests."""
from __future__ import annotations

import sqlite3
import time

import pytest

from localgateway import usage
from pathlib import Path


def _fresh(tmp_path):
    db = tmp_path / "test.db"
    usage._db_path = Path(db)
    usage._initialized = False
    usage._init()
    return db


def _seed_day(db, scope, scope_id, days_ago, cost):
    """Insert a usage row at a specific day offset."""
    ts = time.time() - days_ago * 86400
    conn = sqlite3.connect(str(db))
    if scope == "key":
        conn.execute(
            "INSERT INTO usage (ts, logical_model, provider, backend_model, success, cost, api_key_id, is_probe) "
            "VALUES (?, 'm1', 'p1', 'm1-up', 1, ?, ?, 0)",
            (ts, cost, scope_id),
        )
    else:
        conn.execute(
            "INSERT INTO usage (ts, logical_model, provider, backend_model, success, cost, is_probe) "
            "VALUES (?, ?, 'p1', ? , 1, ?, 0)",
            (ts, scope_id, scope_id + "-up", cost),
        )
    conn.commit()
    conn.close()


def test_anomaly_scan_detects_spike(tmp_path):
    """A sudden 10x spend spike on yesterday triggers an anomaly."""
    db = _fresh(tmp_path)
    key_id = "test-key"
    # 14 baseline days at $1/day
    for d in range(2, 16):
        _seed_day(db, "key", key_id, d, 1.0)
    # Yesterday: $20 (20x baseline)
    _seed_day(db, "key", key_id, 1, 20.0)

    anomalies = usage.daily_anomaly_scan()
    assert len(anomalies) >= 1
    key_anom = [a for a in anomalies if a["scope"] == "key" and a["scope_id"] == key_id]
    assert len(key_anom) == 1
    assert key_anom[0]["actual"] == 20.0
    assert key_anom[0]["expected"] < 2.0  # mean ~1.0


def test_anomaly_scan_no_spike(tmp_path):
    """Normal spend (within baseline range) does not trigger."""
    db = _fresh(tmp_path)
    key_id = "test-key"
    for d in range(1, 16):
        _seed_day(db, "key", key_id, d, 1.0)

    anomalies = usage.daily_anomaly_scan()
    assert len(anomalies) == 0


def test_anomaly_scan_low_spend_no_false_positive(tmp_path):
    """Very low spend ($0.001) doesn't trigger even with a big multiplier."""
    db = _fresh(tmp_path)
    key_id = "cheap-key"
    for d in range(2, 16):
        _seed_day(db, "key", key_id, d, 0.001)
    _seed_day(db, "key", key_id, 1, 0.01)  # 10x but still under $0.10 floor

    anomalies = usage.daily_anomaly_scan()
    assert len(anomalies) == 0


def test_anomaly_scan_insufficient_baseline(tmp_path):
    """Fewer than 3 baseline days does not trigger."""
    db = _fresh(tmp_path)
    key_id = "new-key"
    _seed_day(db, "key", key_id, 3, 1.0)
    _seed_day(db, "key", key_id, 2, 1.0)
    _seed_day(db, "key", key_id, 1, 100.0)  # huge spike but only 2 baseline days

    anomalies = usage.daily_anomaly_scan()
    assert len(anomalies) == 0


def test_anomaly_dedup_same_day(tmp_path):
    """Running the scan twice for the same day doesn't create duplicates."""
    db = _fresh(tmp_path)
    key_id = "test-key"
    for d in range(2, 16):
        _seed_day(db, "key", key_id, d, 1.0)
    _seed_day(db, "key", key_id, 1, 20.0)

    first = usage.daily_anomaly_scan()
    assert len(first) >= 1
    second = usage.daily_anomaly_scan()
    # Should find no NEW anomalies (existing ones are dedup'd by INSERT OR IGNORE)
    key_new = [a for a in second if a["scope"] == "key" and a["scope_id"] == key_id]
    assert len(key_new) == 0


def test_get_active_anomalies_and_ack(tmp_path):
    """Active anomalies are retrievable and dismissible."""
    db = _fresh(tmp_path)
    key_id = "test-key"
    for d in range(2, 16):
        _seed_day(db, "key", key_id, d, 1.0)
    _seed_day(db, "key", key_id, 1, 20.0)

    usage.daily_anomaly_scan()
    active = usage.get_active_anomalies()
    assert len(active) >= 1

    aid = active[0]["id"]
    ok = usage.ack_anomaly(aid)
    assert ok is True

    active2 = usage.get_active_anomalies()
    assert len(active2) == len(active) - 1


def test_anomaly_scan_model_scope(tmp_path):
    """Per-model spike detection works alongside per-key."""
    db = _fresh(tmp_path)
    model_id = "expensive-model"
    for d in range(2, 16):
        _seed_day(db, "model", model_id, d, 2.0)
    _seed_day(db, "model", model_id, 1, 50.0)

    anomalies = usage.daily_anomaly_scan()
    model_anom = [a for a in anomalies if a["scope"] == "model" and a["scope_id"] == model_id]
    assert len(model_anom) == 1
    assert model_anom[0]["actual"] == 50.0

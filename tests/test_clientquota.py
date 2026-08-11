"""Dedicated tests for inbound client quotas (clientquota.py).

Covers the RPM sliding window (burst, block, retry-after), budget hard-stop
(daily + monthly), the 80% soft-warning threshold, spend-cache behavior, and
reset_state. These paths were previously only exercised indirectly through
the governance middleware.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from localgateway import clientquota
from localgateway.config import ApiKeyConfig
from localgateway.usage import set_db_path


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """Each test gets a fresh in-memory-ish DB + cleared quota state."""
    clientquota.reset_state()
    db = tmp_path / "q.db"
    set_db_path(str(db))
    from localgateway import usage as _u, logs as _l
    _u.stop_writer()
    _l.stop_writer()
    _u._initialized = False
    _l._initialized = False
    yield
    clientquota.reset_state()


def _key(key_id="k1", rpm=None, daily=None, monthly=None):
    return ApiKeyConfig(id=key_id, key="secret", rpm=rpm,
                        daily_budget_usd=daily, monthly_budget_usd=monthly)


def test_rpm_allows_under_limit():
    k = _key(rpm=3)
    for _ in range(3):
        assert clientquota.check_rpm(k) is None
    # 4th is over the limit: returns retry-after seconds.
    retry = clientquota.check_rpm(k)
    assert retry is not None and 0 < retry <= 60


def test_rpm_none_means_unlimited():
    k = _key(rpm=None)
    for _ in range(100):
        assert clientquota.check_rpm(k) is None


def test_rpm_no_id_means_unlimited():
    k = ApiKeyConfig(id="", key="x", rpm=10)
    assert clientquota.check_rpm(k) is None


def test_rpm_window_slides():
    """After the 60s window elapses, requests are allowed again."""
    k = _key(rpm=1)
    assert clientquota.check_rpm(k) is None
    assert clientquota.check_rpm(k) is not None
    # Manually expire the deque entry.
    clientquota._hits[k.id][0] -= 61
    assert clientquota.check_rpm(k) is None


def test_current_rpm():
    k = _key(rpm=10)
    clientquota.check_rpm(k)
    clientquota.check_rpm(k)
    assert clientquota.current_rpm(k.id) == 2
    assert clientquota.current_rpm("nope") == 0


def _seed_cost(key_id, cost):
    """Insert a usage row with a given cost so budget checks see it.

    Does NOT invalidate the spend cache: the _isolate fixture clears quota
    state per test, so the first get_spend() is a cache miss and reads fresh.
    Tests that need to force a cache refresh call invalidate_spend() directly.
    """
    from localgateway import usage as _u
    _u._init()
    conn = sqlite3.connect(str(_u._db_path))
    conn.execute(
        "INSERT INTO usage (ts, logical_model, provider, backend_model, success, cost, api_key_id) "
        "VALUES (?, 'm', 'p', 'b', 1, ?, ?)",
        (time.time(), cost, key_id),
    )
    conn.commit()
    conn.close()


def test_budget_daily_exceeded():
    k = _key(daily=10.0)
    _seed_cost(k.id, 10.5)
    exceeded = clientquota.check_budget(k)
    assert exceeded is not None
    period, spent, limit = exceeded
    assert period == "daily"
    assert limit == 10.0
    assert spent >= 10.0


def test_budget_monthly_exceeded():
    k = _key(monthly=100.0)
    _seed_cost(k.id, 101.0)
    exceeded = clientquota.check_budget(k)
    assert exceeded is not None
    assert exceeded[0] == "monthly"


def test_budget_not_exceeded():
    k = _key(daily=50.0, monthly=500.0)
    _seed_cost(k.id, 5.0)
    assert clientquota.check_budget(k) is None


def test_budget_no_budgets_returns_none():
    k = _key()
    assert clientquota.check_budget(k) is None


def test_budget_warning_at_80pct():
    k = _key(daily=10.0)
    _seed_cost(k.id, 8.5)
    warns = clientquota.budget_warnings(k)
    assert len(warns) == 1
    period, spent, limit = warns[0]
    assert period == "daily"
    assert 0.8 <= spent / limit < 1.0


def test_budget_warning_below_80pct_is_empty():
    k = _key(daily=10.0)
    _seed_cost(k.id, 7.0)
    assert clientquota.budget_warnings(k) == []


def test_spend_cache():
    k = _key(daily=100.0)
    _seed_cost(k.id, 5.0)
    s1 = clientquota.get_spend(k.id)
    assert s1["day_cost"] >= 5.0
    _seed_cost(k.id, 3.0)
    # Cached: still sees the old value (TTL not expired).
    s2 = clientquota.get_spend(k.id)
    assert s2["day_cost"] == s1["day_cost"]
    clientquota.invalidate_spend(k.id)
    s3 = clientquota.get_spend(k.id)
    assert s3["day_cost"] >= 8.0


def test_get_spend_empty_id():
    s = clientquota.get_spend("")
    assert s["day_cost"] == 0.0 and s["month_cost"] == 0.0


def test_reset_state():
    k = _key(rpm=2)
    clientquota.check_rpm(k)
    clientquota.check_rpm(k)
    assert clientquota.current_rpm(k.id) == 2
    clientquota.reset_state()
    assert clientquota.current_rpm(k.id) == 0

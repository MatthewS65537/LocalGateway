"""Dedicated tests for the BackgroundWriter (dbwriter.py).

Covers batch flushing, the flush sentinel, shutdown drain, the synchronous
fallback path, batch-boundary behavior, and per-row retry on a bad row — paths
that were previously only exercised indirectly through the usage/logs flows.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from localgateway.dbwriter import BackgroundWriter


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _make_table(db):
    """Create the test table before the writer starts (avoids a race where the
    worker thread processes the queue before the table exists)."""
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()


def test_batch_flush_at_size(tmp_path):
    """A batch of exactly _BATCH_SIZE flushes immediately without waiting."""
    from localgateway.dbwriter import _BATCH_SIZE
    db = tmp_path / "bw.db"
    _make_table(db)
    w = BackgroundWriter(db)
    w.start()
    for i in range(_BATCH_SIZE):
        w.enqueue("INSERT INTO t VALUES (?)", (i,))
    w.flush()
    w.stop(timeout=5)
    conn = _connect(db)
    rows = conn.execute("SELECT v FROM t ORDER BY v").fetchall()
    conn.close()
    assert len(rows) == _BATCH_SIZE
    assert [r["v"] for r in rows] == list(range(_BATCH_SIZE))


def test_flush_sentinel_commit(tmp_path):
    """The __flush__ sentinel forces a commit of pending rows."""
    db = tmp_path / "bw.db"
    w = BackgroundWriter(db)
    w.start()
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    for i in range(5):
        w.enqueue("INSERT INTO t VALUES (?)", (i,))
    w.flush()
    time.sleep(0.3)
    conn = _connect(db)
    rows = conn.execute("SELECT COUNT(*) as n FROM t").fetchone()
    conn.close()
    assert rows["n"] == 5


def test_stop_drains_pending(tmp_path):
    """stop() flushes all queued items before the worker thread exits."""
    db = tmp_path / "bw.db"
    _make_table(db)
    w = BackgroundWriter(db)
    w.start()
    for i in range(20):
        w.enqueue("INSERT INTO t VALUES (?)", (i,))
    w.stop(timeout=5)
    conn = _connect(db)
    rows = conn.execute("SELECT COUNT(*) as n FROM t").fetchone()
    conn.close()
    assert rows["n"] == 20


def test_sync_fallback_when_not_running(tmp_path):
    """enqueue before start() writes synchronously (the fallback path)."""
    db = tmp_path / "bw.db"
    w = BackgroundWriter(db)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()
    conn.close()
    w.enqueue("INSERT INTO t VALUES (?)", (42,))
    conn = _connect(db)
    rows = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert rows["v"] == 42


def test_per_row_retry_on_bad_row(tmp_path):
    """A malformed row doesn't lose the whole batch: good rows still land."""
    db = tmp_path / "bw.db"
    _make_table(db)
    w = BackgroundWriter(db)
    w.start()
    w.enqueue("INSERT INTO t VALUES (?)", (1,))
    w.enqueue("INSERT INTO nope VALUES (?, ?)", (2, 3))
    w.enqueue("INSERT INTO t VALUES (?)", (4,))
    w.stop(timeout=5)
    conn = _connect(db)
    rows = [r["v"] for r in conn.execute("SELECT v FROM t ORDER BY v").fetchall()]
    conn.close()
    assert rows == [1, 4]


def test_idempotent_start_stop(tmp_path):
    """Double-start and double-stop are safe no-ops."""
    db = tmp_path / "bw.db"
    w = BackgroundWriter(db)
    w.start()
    w.start()
    w.stop()
    w.stop()

"""Background SQLite writer: offloads INSERTs from the event loop to a worker
thread so request handling never blocks on disk I/O.

Usage:
    writer = BackgroundWriter(db_path)
    writer.start()
    writer.enqueue("INSERT INTO ... VALUES (?, ...)", (val1, ...))
    writer.stop()  # flush + join

Reads should use their own connections (WAL mode allows concurrent readers).
The writer batches commits (every 100ms or 50 items) for throughput.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path

_BATCH_SIZE = 50
_FLUSH_INTERVAL = 0.1  # seconds


class BackgroundWriter:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._queue: queue.Queue[tuple[str, tuple] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._conn: sqlite3.Connection | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="lg-dbwriter")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)  # sentinel to wake the worker
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def enqueue(self, sql: str, params: tuple) -> None:
        if self._running:
            self._queue.put((sql, params))
        else:
            # Fallback: synchronous write (e.g. during shutdown or tests)
            self._write_sync([(sql, params)])

    def flush(self) -> None:
        """Enqueue a flush sentinel; the worker commits any pending batch."""
        self._queue.put(("__flush__", ()))

    def _run(self) -> None:
        # WAL mode is set by the caller's _init(); don't repeat it here to
        # avoid "database is locked" when multiple connections race on startup.
        try:
            self._conn = sqlite3.connect(self._db_path, timeout=10)
            self._conn.row_factory = sqlite3.Row
        except Exception:
            self._conn = None
            return
        batch: list[tuple[str, tuple]] = []
        # Drain until the None sentinel — NOT until _running flips. stop() sets
        # _running=False before putting the sentinel, so a `while self._running`
        # loop would exit early and drop queued items (a real bug surfaced by
        # the B6 test suite). The sentinel guarantees a clean exit after drain.
        while True:
            try:
                item = self._queue.get(timeout=_FLUSH_INTERVAL)
                if item is None:
                    break  # stop sentinel — exit after final flush
                if item[0] == "__flush__":
                    if batch:
                        self._flush(batch)
                        batch = []
                    continue
                batch.append(item)
                if len(batch) >= _BATCH_SIZE:
                    self._flush(batch)
                    batch = []
            except queue.Empty:
                if batch:
                    self._flush(batch)
                    batch = []
                if not self._running:
                    break  # safety exit when stop() is called with an empty queue
        # Final flush of any pending batch before the thread exits.
        if batch:
            self._flush(batch)
        if self._conn:
            self._conn.close()
            self._conn = None

    def _flush(self, batch: list[tuple[str, tuple]]) -> None:
        if not self._conn or not batch:
            return
        try:
            for sql, params in batch:
                self._conn.execute(sql, params)
            self._conn.commit()
        except Exception:
            # A bad row shouldn't lose the whole batch; retry individually.
            self._conn.rollback()
            for sql, params in batch:
                try:
                    self._conn.execute(sql, params)
                    self._conn.commit()
                except Exception:
                    pass

    def _write_sync(self, batch: list[tuple[str, tuple]]) -> None:
        """Synchronous fallback when the worker isn't running."""
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for sql, params in batch:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

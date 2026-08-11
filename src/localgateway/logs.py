from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .dbwriter import BackgroundWriter

_db_path: Path = Path("data/usage.db")
_lock = threading.Lock()
_initialized = False
_writer: BackgroundWriter | None = None

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def set_db_path(path: str | Path) -> None:
    global _db_path, _writer
    _db_path = Path(path)
    if _writer is not None:
        _writer.stop()
    _writer = BackgroundWriter(_db_path)
    _writer.start()


def stop_writer() -> None:
    global _writer
    if _writer is not None:
        _writer.stop()
        _writer = None


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init() -> None:
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    model TEXT,
                    provider TEXT,
                    status_code INTEGER,
                    latency_ms INTEGER,
                    meta TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)")
        _initialized = True


def log(
    level: str,
    message: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    status_code: int | None = None,
    latency_ms: int | None = None,
    **meta,
) -> None:
    _init()
    # Compact JSON (no spaces) so the request_id LIKE filter in get_logs matches
    # deterministically: %"request_id":"<id>"%.
    meta_str = json.dumps(meta, separators=(",", ":")) if meta else None
    # P1: enqueue to the background writer so logging never blocks the event loop.
    if _writer is not None:
        _writer.enqueue(
            "INSERT INTO logs (ts, level, message, model, provider, status_code, latency_ms, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), level.upper(), message, model, provider, status_code, latency_ms, meta_str),
        )
        return
    with _lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO logs (ts, level, message, model, provider, status_code, latency_ms, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), level.upper(), message, model, provider, status_code, latency_ms, meta_str),
            )


def debug(message: str, **kw) -> None:
    log("DEBUG", message, **kw)


def info(message: str, **kw) -> None:
    log("INFO", message, **kw)


def warn(message: str, **kw) -> None:
    log("WARN", message, **kw)


def error(message: str, **kw) -> None:
    log("ERROR", message, **kw)


def get_logs(
    limit: int = 200,
    level: str | None = None,
    search: str | None = None,
    since_id: int | None = None,
    request_id: str | None = None,
) -> list[dict]:
    _init()
    query = "SELECT * FROM logs"
    conditions: list[str] = []
    params: list = []
    if level:
        conditions.append("level = ?")
        params.append(level.upper())
    if search:
        conditions.append("(message LIKE ? OR model LIKE ? OR provider LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if request_id:
        # F2: filter by request_id stored in the meta JSON column. Uses LIKE
        # since request_id is embedded in a JSON blob (10k-line retention makes
        # this cheap; a dedicated column could be added if scale demands).
        conditions.append("meta LIKE ?")
        params.append(f'%"request_id":"{request_id}"%')
    if since_id is not None:
        conditions.append("id > ?")
        params.append(since_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _lock:
        with _connect() as conn:
            rows = conn.execute(query, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:
                pass
        out.append(d)
    return out


def clear_logs() -> None:
    _init()
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM logs")

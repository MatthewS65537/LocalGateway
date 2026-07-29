from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_db_path: Path = Path("data/usage.db")
_lock = threading.Lock()
_initialized = False

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def set_db_path(path: str | Path) -> None:
    global _db_path
    _db_path = Path(path)


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
    meta_str = json.dumps(meta) if meta else None
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

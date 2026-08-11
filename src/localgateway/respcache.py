"""Exact response cache (F5 stage 1).

Stores and replays complete API responses keyed on a full-body fingerprint
(sha256 of model + all messages + temperature/max_tokens/etc). Opt-in per model
via ``ModelConfig.cache_responses = True``; globally gated by
``server.response_cache_enabled``.

Cache hits:
  - Return the stored body/chunks instantly with ``X-Cache-Hit: true``.
  - Log a usage row with ``cost=0.0`` and ``cache_hit=1`` (counted toward RPM,
    never toward budgets — the honest accounting).
  - Never cache requests with ``tools`` or ``response_format`` (non-deterministic).

Storage lives in the same SQLite DB as usage/logs. Writes (store, hit-bump,
eviction) go through the shared ``BackgroundWriter`` so the request path never
blocks on disk I/O; lookups use a small in-memory read-through LRU.

TTL semantics (B2): ``created`` is immutable (set once at insert) and is what
TTL expiry is measured against — absolute TTL, so an entry requested at least
once per TTL window can never outlive its TTL. LRU recency is tracked
separately in ``last_access`` and used only for eviction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .dbwriter import BackgroundWriter

_db_path: Path = Path("data/usage.db")
_db_lock = threading.Lock()
_initialized = False
_writer: BackgroundWriter | None = None

# In-memory read-through LRU so the hot cache-hit path never touches SQLite.
_MEM_CAPACITY = 256
_mem: OrderedDict[str, tuple] = OrderedDict()

# Params that affect the output and must be part of the cache key. Anything not
# in this set is ignored (a change in e.g. stream doesn't change the answer).
_KEY_FIELDS = (
    "model", "messages", "temperature", "top_p", "top_k", "max_tokens",
    "max_completion_tokens", "frequency_penalty", "presence_penalty",
    "repetition_penalty", "stop", "seed", "n", "response_format",
    "logit_bias", "logprobs", "top_logprobs", "reasoning_effort", "verbosity",
    # Responses API:
    "input", "instructions", "tools",
)

# Never cache requests with these fields — they're non-deterministic or
# stateful and a cached response would be wrong.
_NON_CACHEABLE = ("tools", "tool_choice", "response_format")


def set_db_path(path: str | Path) -> None:
    global _db_path, _initialized, _writer
    _db_path = Path(path)
    _initialized = False
    _mem.clear()
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


def full_fingerprint(request_body: dict) -> str:
    """SHA-256 over the output-affecting subset of the request body.

    Unlike ``cache.fingerprint()`` (which excludes the final user turn for
    warmth tracking), this includes the full message array — two requests with
    different last-turn prompts must NOT share a cached response."""
    h = hashlib.sha256()
    for field in _KEY_FIELDS:
        if field in request_body:
            h.update(field.encode())
            h.update(b"\0")
            h.update(json.dumps(request_body[field], sort_keys=True, ensure_ascii=False).encode())
            h.update(b"\0")
    return h.hexdigest()


def is_cacheable(request_body: dict) -> bool:
    """True when the request is safe to cache (no tools, no response_format,
    and temperature is absent or ≤ 0.1 — high-temperature requests are
    non-deterministic and caching them would surprise users)."""
    for field in _NON_CACHEABLE:
        if field in request_body and request_body[field]:
            # tools/response_format present → don't cache (non-deterministic /
            # structured-output shape may differ even for identical input).
            # Exception: response_format={"type":"text"} is harmless. But keep
            # it simple — skip caching when any of these are set.
            rf = request_body.get(field)
            if field == "response_format" and isinstance(rf, dict) and rf.get("type") == "text":
                continue
            return False
    temp = request_body.get("temperature")
    if isinstance(temp, (int, float)) and temp > 0.1:
        return False
    return True


def _init() -> None:
    global _initialized
    if _initialized:
        return
    with _db_lock:
        if _initialized:
            return
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resp_cache (
                key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                body BLOB NOT NULL,
                stream INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                ttl REAL NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                last_access REAL NOT NULL DEFAULT 0
            )
            """
        )
        existing = {r[1] for r in conn.execute("PRAGMA table_info(resp_cache)")}
        if "last_access" not in existing:
            # Migration for pre-B2 DBs: recency column + backfill from created.
            conn.execute("ALTER TABLE resp_cache ADD COLUMN last_access REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE resp_cache SET last_access = created")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_lru ON resp_cache(last_access)")
        conn.commit()
        conn.close()
        _initialized = True


def _enqueue(sql: str, params: tuple = ()) -> None:
    if _writer is not None and _writer._running:
        _writer.enqueue(sql, params)
    else:
        _write_sync(sql, params)


def _write_sync(sql: str, params: tuple = ()) -> None:
    """Synchronous fallback when no writer is running (tests / shutdown)."""
    _init()
    try:
        conn = sqlite3.connect(str(_db_path), timeout=10)
        conn.execute(sql, params)
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def _mem_put(key: str, entry: tuple) -> None:
    _mem[key] = entry
    _mem.move_to_end(key)
    while len(_mem) > _MEM_CAPACITY:
        _mem.popitem(last=False)


def lookup(key: str, ttl_sec: int) -> tuple[bytes, bool, int] | None:
    """Look up a cached response. Returns (body, stream, hits) or None on miss.

    Expiry is measured against the immutable ``created`` timestamp (absolute
    TTL — B2). Recency (``last_access``) is only for LRU eviction and is
    refreshed asynchronously so the hit path never blocks on SQLite."""
    _init()
    now = time.time()
    entry = _mem.get(key)
    if entry is not None:
        body, stream, hits, created, stored_ttl, _la = entry
        effective_ttl = min(stored_ttl, ttl_sec) if stored_ttl else ttl_sec
        if effective_ttl > 0 and now - created > effective_ttl:
            # Expired — evict from memory + schedule a SQLite delete.
            del _mem[key]
            _enqueue("DELETE FROM resp_cache WHERE key = ?", (key,))
            return None
        _mem.move_to_end(key)
        _enqueue("UPDATE resp_cache SET hits = hits + 1, last_access = ? WHERE key = ?", (now, key))
        return bytes(body), bool(stream), hits + 1
    # Memory miss → one cheap WAL read (sub-ms on the small table).
    with _db_lock:
        try:
            conn = sqlite3.connect(str(_db_path), timeout=10)
            row = conn.execute(
                "SELECT body, stream, hits, created, ttl, last_access FROM resp_cache WHERE key = ?",
                (key,),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        body, stream, hits, created, stored_ttl, _la = row
        effective_ttl = min(stored_ttl, ttl_sec) if stored_ttl else ttl_sec
        if effective_ttl > 0 and now - created > effective_ttl:
            _enqueue("DELETE FROM resp_cache WHERE key = ?", (key,))
            return None
        _mem_put(key, (bytes(body), bool(stream), hits, created, stored_ttl, now))
        _enqueue("UPDATE resp_cache SET hits = hits + 1, last_access = ? WHERE key = ?", (now, key))
        return bytes(body), bool(stream), hits + 1


def store(key: str, model: str, body: bytes, stream: bool, ttl_sec: int, max_entries: int) -> None:
    """Store a response. Evicts the least-recently-accessed entry when
    max_entries is exceeded. All writes happen on the background writer."""
    _init()
    now = time.time()
    _mem_put(key, (bytes(body), bool(stream), 0, now, ttl_sec, now))
    _enqueue(
        "INSERT OR REPLACE INTO resp_cache (key, model, body, stream, created, ttl, hits, last_access) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (key, model, body, 1 if stream else 0, now, ttl_sec, now),
    )
    # SQLite-side LRU eviction (computed inside SQL so it runs off-loop):
    # delete the excess oldest-by-last_access rows when COUNT exceeds max.
    if max_entries > 0:
        _enqueue(
            "DELETE FROM resp_cache WHERE key IN ("
            "SELECT key FROM resp_cache ORDER BY last_access ASC "
            "LIMIT (SELECT MAX(0, (SELECT COUNT(*) FROM resp_cache) - ?)))",
            (max_entries,),
        )


def clear() -> int:
    """Drop all cached responses. Returns the count deleted. Admin-only, so a
    synchronous delete is acceptable."""
    _init()
    _mem.clear()
    with _db_lock:
        try:
            conn = sqlite3.connect(str(_db_path), timeout=10)
            n = conn.execute("DELETE FROM resp_cache").rowcount
            conn.commit()
            conn.close()
            return n
        except sqlite3.Error:
            return 0


def stats() -> dict:
    """Cache size + hit counts for the admin UI."""
    _init()
    with _db_lock:
        try:
            conn = sqlite3.connect(str(_db_path), timeout=10)
            row = conn.execute(
                "SELECT COUNT(*) as n, SUM(hits) as hits FROM resp_cache"
            ).fetchone()
            conn.close()
            return {"entries": row[0] or 0, "total_hits": row[1] or 0}
        except sqlite3.Error:
            return {"entries": 0, "total_hits": 0}

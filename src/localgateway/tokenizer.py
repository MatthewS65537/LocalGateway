"""Token counting with an optional real tokenizer.

Uses tiktoken when installed (``server.tokenizer = "auto"``, the default) and
falls back to the chars/4 heuristic otherwise. All public functions are pure
and dependency-free at import time — tiktoken is imported lazily on first use
so the gateway never hard-requires it.
"""
from __future__ import annotations

import threading

_tiktoken_enc = None
_tiktoken_tried = False
_lock = threading.Lock()


def _get_encoder():
    """Lazy tiktoken encoder (cl100k_base — close enough for routing/estimates
    across GPT/Claude/Llama families). None when tiktoken isn't installed."""
    global _tiktoken_enc, _tiktoken_tried
    with _lock:
        if _tiktoken_tried:
            return _tiktoken_enc
        _tiktoken_tried = True
        try:
            import tiktoken  # type: ignore
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken_enc = None
        return _tiktoken_enc


def tokenizer_name(mode: str = "auto") -> str:
    if mode == "heuristic":
        return "heuristic"
    return "tiktoken:cl100k_base" if _get_encoder() is not None else "heuristic"


def count_text(text: str, mode: str = "auto") -> int:
    """Token count for a raw string."""
    if not text:
        return 0
    if mode != "heuristic":
        enc = _get_encoder()
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
    return max(1, len(text) // 4)


def _message_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def count_messages(messages: list, mode: str = "auto") -> int | None:
    """Token count for an OpenAI messages array (None when empty/invalid).

    Adds the standard per-message framing overhead (~4 tokens) used by chat
    models so context-length checks don't undercount.
    """
    if not isinstance(messages, list) or not messages:
        return None
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = _message_text(msg)
        if not text:
            continue
        total += count_text(text, mode) + 4
        if msg.get("name"):
            total += 1
    return total or None


def count_embedding_input(inp, mode: str = "auto") -> int | None:
    """Token count for an embeddings `input` (string or list of strings)."""
    if isinstance(inp, str):
        return count_text(inp, mode) if inp else None
    if isinstance(inp, list):
        total = 0
        for item in inp:
            if isinstance(item, str):
                total += count_text(item, mode)
            elif isinstance(item, list):
                # list of token ids (already tokenized)
                total += len(item)
        return total or None
    return None

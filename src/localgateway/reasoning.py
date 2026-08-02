from __future__ import annotations

import json

from .config import ProviderConfig
from .sse import SSEParser, SSEEvent, render_event

_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def normalize_delta(delta: dict) -> bool:
    """Dual-emit reasoning fields in a chat delta/message dict, in place.

    Returns True if the dict was modified.
    """
    if not isinstance(delta, dict):
        return False

    value = None
    for key in _REASONING_KEYS:
        v = delta.get(key)
        if v:
            if isinstance(v, list):
                # Anthropic-style thinking blocks: concatenate text blocks
                parts = []
                for block in v:
                    if isinstance(block, dict) and block.get("type") in (None, "thinking", "text"):
                        t = block.get("thinking") or block.get("text")
                        if t:
                            parts.append(t)
                    elif isinstance(block, str):
                        parts.append(block)
                if parts:
                    value = "".join(parts)
            elif isinstance(v, str):
                value = v
            if value:
                break

    if not value:
        return False

    changed = False
    for target in ("reasoning_content", "reasoning"):
        if not delta.get(target):
            delta[target] = value
            changed = True
    return changed


def _normalize_obj(obj: dict) -> bool:
    changed = False
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for field in ("delta", "message"):
                if normalize_delta(choice.get(field)):
                    changed = True
    # Some providers put reasoning at the top level of the message object
    if normalize_delta(obj):
        changed = True
    return changed


def normalize_response_body(provider: ProviderConfig, body: bytes) -> bytes:
    """Normalize reasoning fields in a non-streaming JSON response."""
    if b"reasoning" not in body and b"thinking" not in body:
        return body
    try:
        obj = json.loads(body)
    except Exception:
        return body
    if not isinstance(obj, dict) or not _normalize_obj(obj):
        return body
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def transform_stream_chunk(provider: ProviderConfig, chunk: bytes, parser: SSEParser) -> bytes:
    """Normalize reasoning fields across SSE chunks for a provider.

    Chunks are buffered to event boundaries (via the caller-owned parser) and
    only re-serialized when a reasoning field was actually rewritten; other
    events are re-emitted verbatim.
    """
    events = parser.feed(chunk)
    if not events:
        return b""

    out = bytearray()
    for ev in events:
        if ev.done:
            out += render_event(ev)
            continue
        if "reasoning" not in ev.data and "thinking" not in ev.data:
            out += render_event(ev)
            continue
        try:
            obj = json.loads(ev.data)
        except Exception:
            out += render_event(ev)
            continue
        if isinstance(obj, dict) and _normalize_obj(obj):
            out += (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")
        else:
            out += render_event(ev)
    return bytes(out)


def flush_stream_chunk(provider: ProviderConfig, parser: SSEParser) -> bytes:
    """Render any trailing bytes buffered in the parser at end of stream."""
    ev = parser.flush()
    if ev is None:
        return b""
    if ev.done:
        return render_event(ev)
    try:
        obj = json.loads(ev.data)
    except Exception:
        return render_event(ev)
    if isinstance(obj, dict) and _normalize_obj(obj):
        return (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")
    return render_event(ev)

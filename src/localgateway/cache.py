from __future__ import annotations

import hashlib
from typing import Any

WARMTH_TTL_DEFAULT = 300
_PREFIX_TRUNCATE = 2000


def _msg_text(msg: Any) -> str:
    """Extract a stable text representation of a single message."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
                else:
                    parts.append(str(part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def fingerprint(request_body: dict) -> str | None:
    """Compute a stable cache-key fingerprint for a request.

    The fingerprint covers the system prompt plus all "sent" messages — i.e.
    every message except the final (new) user turn. This is the cacheable
    prefix a provider would reuse on the next turn. For single-turn requests
    with no prior history, the full prompt is hashed.

    Returns a 16-char hex string, or None when no usable prefix exists.
    """
    messages = request_body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    system_parts: list[str] = []
    if isinstance(request_body.get("system"), str):
        system_parts.append(request_body["system"])

    # Separate system messages from the conversation turns.
    convo: list[dict] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            system_parts.append(_msg_text(m))
        else:
            convo.append(m)

    # "Sent" prefix = everything except the final user turn.
    prefix_msgs = list(convo)
    if convo and isinstance(convo[-1], dict) and convo[-1].get("role") == "user":
        prefix_msgs = convo[:-1]

    # Single-turn edge: if excluding the last message leaves only system (or
    # nothing), include the full conversation so the request still gets a key.
    if not prefix_msgs:
        prefix_msgs = list(convo)

    if not system_parts and not prefix_msgs:
        return None

    pieces: list[str] = []
    has_content = False
    for s in system_parts:
        pieces.append(s[:_PREFIX_TRUNCATE])
        if s.strip():
            has_content = True
    for m in prefix_msgs:
        role = m.get("role", "") if isinstance(m, dict) else ""
        text = _msg_text(m)[:_PREFIX_TRUNCATE]
        pieces.append(f"{role}:{text}")
        if text.strip():
            has_content = True

    if not has_content:
        return None
    blob = "\n\u0000\n".join(pieces)
    return hashlib.sha1(blob.encode("utf-8", errors="replace")).hexdigest()[:16]
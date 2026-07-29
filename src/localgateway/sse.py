from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class SSEEvent:
    data: str
    done: bool = False


@dataclass
class StreamUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None


def usage_from_dict(usage: dict) -> StreamUsage:
    out = StreamUsage()
    if not isinstance(usage, dict):
        return out
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    if isinstance(pt, int):
        out.input_tokens = pt
    if isinstance(ct, int):
        out.output_tokens = ct
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        rt = details.get("reasoning_tokens")
        if isinstance(rt, int):
            out.reasoning_tokens = rt
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens")
        if isinstance(cached, int):
            out.cached_tokens = cached
        cw = prompt_details.get("cache_write_tokens") or prompt_details.get("cache_read_tokens")
        if isinstance(cw, int):
            out.cache_write_tokens = cw
    cr = usage.get("cache_read_input_tokens")
    if isinstance(cr, int):
        out.cached_tokens = cr
    cw = usage.get("cache_creation_input_tokens")
    if isinstance(cw, int):
        out.cache_write_tokens = cw
    return out


class SSEParser:
    """Incremental SSE parser. Feed arbitrary byte chunks; get complete events.

    Handles events split across chunk boundaries, CRLF line endings, and
    keep-alive comment lines (which are dropped).
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if chunk:
            self._buf += chunk
            if b"\r" in self._buf:
                self._buf = self._buf.replace(b"\r\n", b"\n")
        events: list[SSEEvent] = []
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            ev = self._parse_block(raw)
            if ev is not None:
                events.append(ev)
        return events

    def flush(self) -> SSEEvent | None:
        """Parse any trailing bytes left in the buffer (stream ended without a
        final blank line)."""
        if not self._buf.strip():
            self._buf = b""
            return None
        raw, self._buf = self._buf, b""
        return self._parse_block(raw)

    @staticmethod
    def _parse_block(raw: bytes) -> SSEEvent | None:
        text = raw.decode("utf-8", errors="replace")
        data_lines: list[str] = []
        for line in text.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        return SSEEvent(data=data, done=(data.strip() == "[DONE]"))


def render_event(ev: SSEEvent) -> bytes:
    """Serialize an event back to wire format."""
    if ev.done:
        return b"data: [DONE]\n\n"
    return (
        "".join(f"data: {line}\n" for line in ev.data.split("\n")) + "\n"
    ).encode("utf-8")


class StreamAccumulator:
    """Parses a stream incrementally, tracking usage, TTFT, and [DONE]."""

    def __init__(self, start: float | None = None) -> None:
        self.parser = SSEParser()
        self.usage = StreamUsage()
        self.saw_done = False
        self._start = start if start is not None else time.monotonic()
        self.ttft_ms: int | None = None
        self._content_text: list[str] = []
        self._first_byte_ms: int | None = None

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if self._first_byte_ms is None and chunk.strip():
            self._first_byte_ms = int((time.monotonic() - self._start) * 1000)
        events = self.parser.feed(chunk)
        for ev in events:
            self._observe(ev)
        return events

    def finish(self) -> list[SSEEvent]:
        ev = self.parser.flush()
        if ev is not None:
            self._observe(ev)
            return [ev]
        return []

    def estimated_output_tokens(self) -> int | None:
        """Rough token estimate from accumulated content (~4 chars/token)."""
        text = "".join(self._content_text)
        if not text:
            return None
        return max(1, len(text) // 4)

    def _observe(self, ev: SSEEvent) -> None:
        if ev.done:
            self.saw_done = True
            return
        try:
            obj = json.loads(ev.data)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self.usage = _merge_usage(self.usage, usage_from_dict(usage))
        if self.ttft_ms is None:
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                if isinstance(delta, dict):
                    content = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                    if content:
                        self._content_text.append(content)
                        self.ttft_ms = int((time.monotonic() - self._start) * 1000)


def _merge_usage(a: StreamUsage, b: StreamUsage) -> StreamUsage:
    return StreamUsage(
        input_tokens=b.input_tokens if b.input_tokens is not None else a.input_tokens,
        output_tokens=b.output_tokens if b.output_tokens is not None else a.output_tokens,
        reasoning_tokens=b.reasoning_tokens if b.reasoning_tokens is not None else a.reasoning_tokens,
        cached_tokens=b.cached_tokens if b.cached_tokens is not None else a.cached_tokens,
        cache_write_tokens=b.cache_write_tokens if b.cache_write_tokens is not None else a.cache_write_tokens,
    )

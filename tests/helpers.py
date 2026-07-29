from __future__ import annotations

import json

import httpx


async def sse_collect(client: httpx.AsyncClient, body: dict) -> list[dict | str]:
    """POST a streaming chat request; return parsed SSE payloads ('[DONE]' kept as str)."""
    events: list[dict | str] = []
    async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        buf = ""
        async for text in resp.aiter_text():
            buf += text
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.split("\n"):
                    if line.startswith("data: "):
                        data = line[6:]
                        events.append("[DONE]" if data.strip() == "[DONE]" else json.loads(data))
    return events

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator, Union

import httpx

from .config import GatewayConfig, ProviderConfig, PricingEntry
from .ratelimit import ratelimit
from .sse import StreamAccumulator, StreamUsage, usage_from_dict


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_IDLE_TIMEOUT = 60.0


@dataclass
class ProviderResult:
    success: bool
    status_code: int
    body: bytes
    headers: dict[str, str]
    error: str | None
    provider_id: str
    backend_model: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: int
    retryable: bool


@dataclass
class StreamChunk:
    """Raw bytes ready to forward to the client."""
    data: bytes


@dataclass
class StreamDone:
    usage: StreamUsage
    latency_ms: int
    ttft_ms: int | None
    sent_done: bool


@dataclass
class StreamFailed:
    error: str
    status_code: int
    retryable: bool
    mid_stream: bool


StreamEvent = Union[StreamChunk, StreamDone, StreamFailed]


def _extract_usage(body: bytes) -> StreamUsage:
    import json
    try:
        data = json.loads(body)
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage_from_dict(usage)
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content") or ""
                if content:
                    return StreamUsage(output_tokens=max(1, len(content) // 4))
    except Exception:
        pass
    return StreamUsage()


def _inject_stream_options(body: dict) -> dict:
    """Ensure the upstream request asks for usage in the stream."""
    so = body.get("stream_options")
    if not isinstance(so, dict):
        so = {}
    if "include_usage" not in so:
        so = {**so, "include_usage": True}
    body["stream_options"] = so
    return body


async def call_provider(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    backend_model: str,
    request_body: dict,
    *,
    stream: bool = False,
) -> ProviderResult:
    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider.headers)

    body = dict(request_body)
    body["model"] = backend_model
    body["stream"] = stream

    t0 = time.monotonic()
    try:
        resp = await client.post(
            url,
            headers=headers,
            json=body,
            timeout=provider.timeout,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        retryable = resp.status_code in RETRYABLE_STATUS

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            ratelimit.set_cooldown(provider.id, backend_model, retry_after)

        if resp.status_code != 200:
            error_msg = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
            return ProviderResult(
                success=False,
                status_code=resp.status_code,
                body=b"",
                headers=dict(resp.headers),
                error=error_msg,
                provider_id=provider.id,
                backend_model=backend_model,
                input_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                cached_tokens=None,
                cache_write_tokens=None,
                latency_ms=latency_ms,
                retryable=retryable,
            )

        usage = _extract_usage(resp.content)
        return ProviderResult(
            success=True,
            status_code=200,
            body=resp.content,
            headers=dict(resp.headers),
            error=None,
            provider_id=provider.id,
            backend_model=backend_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            latency_ms=latency_ms,
            retryable=False,
        )

    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return ProviderResult(
            success=False,
            status_code=0,
            body=b"",
            headers={},
            error="timeout",
            provider_id=provider.id,
            backend_model=backend_model,
            input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            cached_tokens=None,
            cache_write_tokens=None,
            latency_ms=latency_ms,
            retryable=True,
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return ProviderResult(
            success=False,
            status_code=0,
            body=b"",
            headers={},
            error=str(e)[:500],
            provider_id=provider.id,
            backend_model=backend_model,
            input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            cached_tokens=None,
            cache_write_tokens=None,
            latency_ms=latency_ms,
            retryable=True,
        )


async def call_provider_stream(
    client: httpx.AsyncClient,
    provider: ProviderConfig,
    backend_model: str,
    request_body: dict,
) -> AsyncIterator[StreamEvent]:
    """Stream from a provider, yielding structured events.

    - StreamChunk: bytes to forward to the client.
    - StreamDone: stream completed successfully.
    - StreamFailed: error; `mid_stream=False` means fallback is still possible.
    """
    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider.headers)

    body = _inject_stream_options(dict(request_body))
    body["model"] = backend_model
    body["stream"] = True

    idle = provider.stream_idle_timeout or DEFAULT_IDLE_TIMEOUT
    t0 = time.monotonic()
    acc = StreamAccumulator(start=t0)
    first = False

    try:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=body,
            timeout=provider.timeout,
        ) as resp:
            if resp.status_code != 200:
                error_text = b""
                async for chunk in resp.aiter_bytes():
                    error_text += chunk
                error_msg = error_text.decode("utf-8", errors="replace")[:500]

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after) if retry_after else None
                    except ValueError:
                        retry_after = None
                    ratelimit.set_cooldown(provider.id, backend_model, retry_after)

                yield StreamFailed(
                    error=error_msg,
                    status_code=resp.status_code,
                    retryable=resp.status_code in RETRYABLE_STATUS,
                    mid_stream=False,
                )
                return

            aiter = resp.aiter_bytes().__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield StreamFailed(
                        error=f"idle timeout ({idle:g}s) waiting for chunks",
                        status_code=0,
                        retryable=True,
                        mid_stream=first,
                    )
                    return
                if not chunk:
                    continue
                first = True
                acc.feed(chunk)
                yield StreamChunk(data=chunk)

            for ev in acc.finish():
                from .sse import render_event
                yield StreamChunk(data=render_event(ev))

            final_usage = acc.usage
            if final_usage.output_tokens is None:
                est = acc.estimated_output_tokens()
                if est is not None:
                    final_usage = StreamUsage(
                        input_tokens=final_usage.input_tokens,
                        output_tokens=est,
                        reasoning_tokens=final_usage.reasoning_tokens,
                        cached_tokens=final_usage.cached_tokens,
                        cache_write_tokens=final_usage.cache_write_tokens,
                    )

            final_ttft = acc.ttft_ms
            if final_ttft is None:
                final_ttft = acc._first_byte_ms

            yield StreamDone(
                usage=final_usage,
                latency_ms=int((time.monotonic() - t0) * 1000),
                ttft_ms=final_ttft,
                sent_done=acc.saw_done,
            )

    except httpx.TimeoutException:
        yield StreamFailed(error="timeout", status_code=0, retryable=True, mid_stream=first)
    except Exception as e:
        yield StreamFailed(error=str(e)[:500], status_code=0, retryable=True, mid_stream=first)

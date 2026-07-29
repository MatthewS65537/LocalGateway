from __future__ import annotations

import json
import time
from typing import AsyncGenerator

from .provider import (
    call_provider,
    call_provider_stream,
    ProviderResult,
    StreamChunk,
    StreamDone,
    StreamFailed,
)
from .router import select_backends, SelectedBackend, ProviderPrefs


def _shift_usage_to_cached(body: bytes) -> bytes:
    """When a provider has no cached-input pricing, shift all input tokens
    to cached read tokens in the relayed usage payload so the user sees the
    same accounting the gateway uses internally."""
    try:
        data = json.loads(body)
    except Exception:
        return body
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return body
    pt = usage.get("prompt_tokens")
    if not isinstance(pt, int) or pt == 0:
        return body
    usage["prompt_tokens"] = 0
    pd = usage.get("prompt_tokens_details")
    if not isinstance(pd, dict):
        pd = {}
        usage["prompt_tokens_details"] = pd
    existing_cached = pd.get("cached_tokens") or 0
    pd["cached_tokens"] = existing_cached + pt
    cr = usage.get("cache_read_input_tokens")
    usage["cache_read_input_tokens"] = (cr or 0) + pt
    return json.dumps(data).encode("utf-8")


def _shift_stream_usage_to_cached(chunk: bytes) -> bytes:
    """Same as _shift_usage_to_cached but for a single SSE data chunk."""
    try:
        text = chunk.decode("utf-8")
    except Exception:
        return chunk
    if '"usage"' not in text:
        return chunk
    lines = []
    changed = False
    for line in text.split("\n"):
        if line.startswith("data: "):
            payload = line[6:]
            if payload.strip() == "[DONE]":
                lines.append(line)
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                lines.append(line)
                continue
            if isinstance(obj, dict) and "usage" in obj:
                new_body = _shift_usage_to_cached(json.dumps(obj).encode("utf-8"))
                if new_body != json.dumps(obj).encode("utf-8"):
                    changed = True
                    lines.append("data: " + new_body.decode("utf-8"))
                    continue
        lines.append(line)
    if not changed:
        return chunk
    return ("\n".join(lines) + "\n").encode("utf-8")
from .config import GatewayConfig, load_config
from .ratelimit import ratelimit
from .usage import log_request
from .config import PricingEntry
from . import logs
from . import stats


def _strip_gateway_fields(request_body: dict) -> dict:
    """Remove LocalGateway/OpenRouter control fields before upstream POST."""
    out = dict(request_body)
    out.pop("provider", None)
    return out


def _compute_cost(
    config: GatewayConfig,
    provider_id: str,
    backend_model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    pricing = config.pricing_for(provider_id, backend_model)
    input_price = pricing.input or 0.0
    output_price = pricing.output or 0.0
    cache_read_price = pricing.cache_read if pricing.cache_read is not None else input_price
    cache_write_price = pricing.cache_write if pricing.cache_write is not None else input_price

    cached = cached_tokens or 0
    cache_write = cache_write_tokens or 0
    regular_input = max(input_tokens - cached - cache_write, 0)

    cost = (
        regular_input * input_price +
        cached * cache_read_price +
        cache_write * cache_write_price +
        output_tokens * output_price
    )
    return cost


def compute_tps(
    output_tokens: int | None,
    latency_ms: int | None,
    ttft_ms: int | None = None,
) -> float | None:
    """Tokens per second for the generation phase (excludes TTFT).

    Throughput reflects decode speed, not prefill/queue latency, so we
    subtract TTFT from the total latency when available.
    """
    if not output_tokens or not latency_ms:
        return None
    gen_ms = latency_ms - (ttft_ms or 0)
    if gen_ms <= 0:
        gen_ms = latency_ms
    return round(output_tokens / (gen_ms / 1000.0), 2)


def _error_sse(message: str, code: str) -> bytes:
    body = json.dumps({"error": {"message": message, "type": "api_error", "code": code}})
    return f"data: {body}\n\n".encode()


def _requested_max_tokens(request_body: dict) -> int | None:
    mt = request_body.get("max_completion_tokens")
    if mt is None:
        mt = request_body.get("max_tokens")
    try:
        return int(mt) if mt is not None else None
    except (TypeError, ValueError):
        return None


# Params safe to inherit from model.default_params. Client values always win.
_DEFAULT_PARAM_KEYS = frozenset({
    "temperature", "top_p", "top_k", "min_p",
    "max_tokens", "max_completion_tokens",
    "frequency_penalty", "presence_penalty", "repetition_penalty",
    "stop", "seed", "n",
    "response_format", "tools", "tool_choice",
    "parallel_tool_calls", "logit_bias", "logprobs", "top_logprobs",
    "reasoning_effort", "verbosity",
})

_DEFAULT_PARAM_BLOCKED = frozenset({
    "model", "messages", "stream", "stream_options", "provider", "user",
})


def apply_default_params(request_body: dict, defaults: dict | None) -> dict:
    """Merge model default_params under the client body. Client keys win.

    Never overrides model/messages/stream/provider. Only whitelisted keys from
    defaults are applied when the client omitted them.
    """
    if not defaults:
        return request_body
    out = dict(request_body)
    for key, value in defaults.items():
        if key in _DEFAULT_PARAM_BLOCKED:
            continue
        if key not in _DEFAULT_PARAM_KEYS:
            continue
        if key not in out or out[key] is None:
            out[key] = value
    return out


def _provider_headers(provider_id: str, backend_model: str, generation_id: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Provider": provider_id,
        "X-Backend": f"{provider_id}/{backend_model}",
    }
    if generation_id:
        headers["X-Generation-Id"] = generation_id
    return headers


def _estimate_input_tokens(request_body: dict) -> int | None:
    """Rough estimate of input token count from messages (~4 chars/token)."""
    messages = request_body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    total_chars = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
    return max(1, total_chars // 4) if total_chars else None


def _resolve_backends(
    config: GatewayConfig,
    logical_model: str,
    max_tokens: int | None,
    input_tokens: int | None = None,
    prefs: ProviderPrefs | None = None,
) -> tuple[list[SelectedBackend], dict | None, int]:
    """Select backends for a request. Returns (backends, error_dict, status).
    error_dict is set when the request cannot be routed."""
    model = config.model_by_id(logical_model)
    if model is None or not model.enabled:
        return [], {
            "message": f"Model '{logical_model}' not found or has no configured backends",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }, 404

    backends = list(
        select_backends(
            config, logical_model, max_tokens=max_tokens, input_tokens=input_tokens, prefs=prefs
        )
    )
    if backends:
        return backends, None, 0

    if max_tokens is not None:
        limits = [
            b.max_output_tokens
            for b in model.backends
            if b.enabled
            and (p := config.provider_by_id(b.provider)) and p.enabled
            and b.max_output_tokens is not None
        ]
        max_avail = max(limits) if limits else None
        detail = f" (max available: {max_avail})" if max_avail is not None else ""
        return [], {
            "message": (
                f"Requested max_tokens={max_tokens} exceeds the output limit of every "
                f"backend for '{logical_model}'{detail}"
            ),
            "type": "invalid_request_error",
            "code": "output_limit_exceeded",
        }, 400

    return [], {
        "message": f"Model '{logical_model}' not found or has no configured backends",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }, 404


async def handle_request(
    client,
    request_body: dict,
    stream: bool = False,
) -> tuple[bytes, int, dict[str, str]]:
    """Handle a non-streaming chat completion request with fallback.

    Returns (response_body, status_code, headers).
    """
    config = load_config()
    requested_model = request_body.get("model", "")
    model_cfg = config.model_by_id(requested_model)
    logical_model = model_cfg.id if model_cfg else requested_model
    prefs = ProviderPrefs.from_request(request_body)
    request_body = apply_default_params(
        request_body, model_cfg.default_params if model_cfg else None
    )
    upstream_body = _strip_gateway_fields(request_body)
    max_tokens = _requested_max_tokens(request_body)
    input_tokens = _estimate_input_tokens(request_body)

    backends, err, status = _resolve_backends(
        config, logical_model, max_tokens, input_tokens, prefs=prefs
    )
    if err is not None:
        return (
            json.dumps({"error": err}).encode(),
            status,
            {"Content-Type": "application/json"},
        )

    errors: list[str] = []
    for selected in backends:
        stats.start_request(selected.provider.id, selected.backend.model)
        result = await call_provider(
            client,
            selected.provider,
            selected.backend.model,
            upstream_body,
            stream=False,
        )
        stats.end_request(selected.provider.id, selected.backend.model)

        if result.success:
            stats.record_success(
                result.provider_id,
                result.backend_model,
                latency_ms=result.latency_ms,
            )
            body = _maybe_normalize_reasoning(config, selected.provider, result.body)
            pricing = config.pricing_for(result.provider_id, result.backend_model)
            if pricing.cache_read is None:
                body = _shift_usage_to_cached(body)
            log_input = result.input_tokens
            log_cached = result.cached_tokens
            if pricing.cache_read is None and result.input_tokens:
                log_cached = (result.cached_tokens or 0) + result.input_tokens
                log_input = 0
            cost = _compute_cost(
                config,
                result.provider_id,
                result.backend_model,
                result.input_tokens,
                result.output_tokens,
                result.cached_tokens,
                result.cache_write_tokens,
            )
            log_request(
                logical_model=logical_model,
                provider=result.provider_id,
                backend_model=result.backend_model,
                success=True,
                input_tokens=log_input,
                output_tokens=result.output_tokens,
                reasoning_tokens=result.reasoning_tokens,
                cached_tokens=log_cached,
                cache_write_tokens=result.cache_write_tokens,
                cost=cost,
                latency_ms=result.latency_ms,
                tps=compute_tps(result.output_tokens, result.latency_ms),
                stream=False,
            )
            logs.info(
                "request ok",
                model=logical_model,
                provider=result.provider_id,
                status_code=200,
                latency_ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                reasoning_tokens=result.reasoning_tokens,
            )
            return (
                body,
                200,
                _provider_headers(result.provider_id, result.backend_model),
            )

        errors.append(f"[{selected.provider.id}:{selected.backend.model}] {result.error}")
        stats.record_failure(selected.provider.id, selected.backend.model, result.error)

        if not result.retryable:
            log_request(
                logical_model=logical_model,
                provider=result.provider_id,
                backend_model=result.backend_model,
                success=False,
                error=result.error,
                latency_ms=result.latency_ms,
                stream=False,
            )
            logs.error(
                f"request failed (non-retryable): {result.error}",
                model=logical_model,
                provider=result.provider_id,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
            )
            return (
                json.dumps({
                    "error": {
                        "message": result.error or "Unknown error",
                        "type": "api_error",
                        "code": f"http_{result.status_code}",
                    }
                }).encode(),
                result.status_code if result.status_code else 502,
                {"Content-Type": "application/json"},
            )

        log_request(
            logical_model=logical_model,
            provider=result.provider_id,
            backend_model=result.backend_model,
            success=False,
            error=result.error,
            latency_ms=result.latency_ms,
            stream=False,
        )
        logs.warn(
            f"backend failed, falling back: {result.error}",
            model=logical_model,
            provider=result.provider_id,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
        )

    logs.error(
        f"all backends failed: {'; '.join(errors)}",
        model=logical_model,
    )
    return (
        json.dumps({
            "error": {
                "message": f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
                "type": "api_error",
                "code": "all_backends_failed",
            }
        }).encode(),
        502,
        {"Content-Type": "application/json"},
    )


async def handle_request_stream(
    client,
    request_body: dict,
) -> AsyncGenerator:
    """Handle a streaming chat completion request with pre-first-chunk fallback.

    First yield is always a dict ``{"headers": {...}}`` so the HTTP layer can
    set X-Provider before body bytes. Subsequent yields are SSE ``bytes``.
    Pre-first-chunk failures fall back; mid-stream failures emit an error event.
    """
    config = load_config()
    requested_model = request_body.get("model", "")
    model_cfg = config.model_by_id(requested_model)
    logical_model = model_cfg.id if model_cfg else requested_model
    prefs = ProviderPrefs.from_request(request_body)
    request_body = apply_default_params(
        request_body, model_cfg.default_params if model_cfg else None
    )
    upstream_body = _strip_gateway_fields(request_body)
    max_tokens = _requested_max_tokens(request_body)
    input_tokens = _estimate_input_tokens(request_body)

    backends, err, status = _resolve_backends(
        config, logical_model, max_tokens, input_tokens, prefs=prefs
    )
    if err is not None:
        yield {"headers": {"Content-Type": "text/event-stream"}}
        yield _error_sse(err["message"], err["code"])
        yield b"data: [DONE]\n\n"
        return

    # Provisional headers from first candidate; upgraded when first chunk commits
    first = backends[0] if backends else None
    provisional = (
        _provider_headers(first.provider.id, first.backend.model)
        if first else {"Content-Type": "text/event-stream"}
    )
    yield {"headers": provisional}

    errors: list[str] = []
    for selected in backends:
        t0 = time.monotonic()
        first_yielded = False
        ttft_ms: int | None = None
        transform_parser = _new_transform_parser()
        stats.start_request(selected.provider.id, selected.backend.model)
        pricing = config.pricing_for(selected.provider.id, selected.backend.model)

        async for event in call_provider_stream(
            client,
            selected.provider,
            selected.backend.model,
            upstream_body,
        ):
            if isinstance(event, StreamDone):
                stats.record_success(
                    selected.provider.id,
                    selected.backend.model,
                    latency_ms=event.latency_ms,
                    ttft_ms=event.ttft_ms,
                )
                usage = event.usage
                cost = _compute_cost(
                    config,
                    selected.provider.id,
                    selected.backend.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    usage.cache_write_tokens,
                )
                log_input = usage.input_tokens
                log_cached = usage.cached_tokens
                if pricing.cache_read is None and usage.input_tokens:
                    log_cached = (usage.cached_tokens or 0) + usage.input_tokens
                    log_input = 0
                log_request(
                    logical_model=logical_model,
                    provider=selected.provider.id,
                    backend_model=selected.backend.model,
                    success=True,
                    input_tokens=log_input,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cached_tokens=log_cached,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost=cost,
                    latency_ms=event.latency_ms,
                    ttft_ms=event.ttft_ms,
                    tps=compute_tps(usage.output_tokens, event.latency_ms, event.ttft_ms),
                    stream=True,
                )
                logs.info(
                    "stream ok",
                    model=logical_model,
                    provider=selected.provider.id,
                    status_code=200,
                    latency_ms=event.latency_ms,
                    ttft_ms=event.ttft_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                )
                tail = _flush_transform(selected.provider, transform_parser)
                if tail:
                    yield tail
                if not event.sent_done:
                    yield b"data: [DONE]\n\n"
                stats.end_request(selected.provider.id, selected.backend.model)
                return

            if isinstance(event, StreamFailed):
                latency_ms = int((time.monotonic() - t0) * 1000)
                if not event.mid_stream:
                    errors.append(
                        f"[{selected.provider.id}:{selected.backend.model}] {event.error}"
                    )
                    stats.record_failure(selected.provider.id, selected.backend.model, event.error)
                    log_request(
                        logical_model=logical_model,
                        provider=selected.provider.id,
                        backend_model=selected.backend.model,
                        success=False,
                        error=event.error,
                        latency_ms=latency_ms,
                        stream=True,
                    )
                    if not event.retryable:
                        logs.error(
                            f"stream failed (non-retryable): {event.error}",
                            model=logical_model,
                            provider=selected.provider.id,
                            status_code=event.status_code,
                            latency_ms=latency_ms,
                        )
                        yield _error_sse(event.error, f"http_{event.status_code}")
                        yield b"data: [DONE]\n\n"
                        stats.end_request(selected.provider.id, selected.backend.model)
                        return
                    logs.warn(
                        f"stream backend failed, falling back: {event.error}",
                        model=logical_model,
                        provider=selected.provider.id,
                        status_code=event.status_code,
                        latency_ms=latency_ms,
                    )
                    stats.end_request(selected.provider.id, selected.backend.model)
                    break  # try next backend

                # Mid-stream failure: cannot fall back. Surface the error.
                stats.record_failure(selected.provider.id, selected.backend.model, event.error)
                log_request(
                    logical_model=logical_model,
                    provider=selected.provider.id,
                    backend_model=selected.backend.model,
                    success=False,
                    error=event.error,
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    stream=True,
                )
                logs.error(
                    f"stream failed mid-stream: {event.error}",
                    model=logical_model,
                    provider=selected.provider.id,
                    latency_ms=latency_ms,
                )
                yield _error_sse(event.error, "stream_interrupted")
                yield b"data: [DONE]\n\n"
                stats.end_request(selected.provider.id, selected.backend.model)
                return

            # StreamChunk
            if not first_yielded:
                first_yielded = True
                ttft_ms = int((time.monotonic() - t0) * 1000)
            out = _transform_chunk(config, selected.provider, event.data, transform_parser)
            if out:
                if pricing.cache_read is None:
                    out = _shift_stream_usage_to_cached(out)
                yield out

    # All backends failed before first chunk
    logs.error(
        f"all backends failed: {'; '.join(errors)}",
        model=logical_model,
    )
    yield _error_sse(
        f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
        "all_backends_failed",
    )
    yield b"data: [DONE]\n\n"


def _maybe_normalize_reasoning(config: GatewayConfig, provider, body: bytes) -> bytes:
    """Phase 2 hook: normalize reasoning fields in non-stream responses."""
    from .reasoning import normalize_response_body
    return normalize_response_body(provider, body)


def _new_transform_parser():
    from .sse import SSEParser
    return SSEParser()


def _transform_chunk(config: GatewayConfig, provider, chunk: bytes, parser) -> bytes:
    """Phase 2 hook: normalize reasoning fields in SSE chunks."""
    from .reasoning import transform_stream_chunk
    return transform_stream_chunk(provider, chunk, parser)


def _flush_transform(provider, parser) -> bytes:
    from .reasoning import flush_stream_chunk
    return flush_stream_chunk(provider, parser)

from __future__ import annotations

import json
import time
import uuid
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
from .cache import fingerprint
from .config import GatewayConfig, PricingEntry, load_config
from .ratelimit import ratelimit
from .usage import log_request
from . import logs
from . import stats
from . import circuitbreaker
from . import tokenizer
from . import respcache
from .reasoning import normalize_response_body, transform_stream_chunk, flush_stream_chunk
from .sse import SSEParser


# Request correlation IDs: minted once per request, shared across all fallback
# attempts, echoed as X-Request-Id, and persisted in both the usage row and the
# log row meta. Lets a single request's attempt timeline be reconstructed
# (foundation for tracing and response-cache hit accounting).
_REQ_ID_LEN = 12


def new_request_id() -> str:
    """Mint a fresh request ID (12 hex chars)."""
    return uuid.uuid4().hex[:_REQ_ID_LEN]


def resolve_request_id(inbound: str | None) -> str:
    """Honor a client-supplied X-Request-Id when present and well-formed
    (alphanumeric/dash/dot/underscore, <=64 chars); otherwise mint a fresh one.
    Lets upstream callers correlate a gateway request with their own trace."""
    if inbound:
        clean = inbound.strip()[:64]
        if clean and all(c.isalnum() or c in "-_." for c in clean):
            return clean
    return new_request_id()


def _shift_usage_obj(obj: dict) -> dict | None:
    """Shift input tokens to cached in a parsed usage object.

    Returns the modified dict or None when no shift is needed (no usage,
    or prompt_tokens is 0). P7: extracted so the stream path avoids a
    json.dumps → json.loads → json.dumps round-trip per usage event.
    """
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    pt = usage.get("prompt_tokens")
    if not isinstance(pt, int) or pt == 0:
        return None
    usage["prompt_tokens"] = 0
    pd = usage.get("prompt_tokens_details")
    if not isinstance(pd, dict):
        pd = {}
        usage["prompt_tokens_details"] = pd
    existing_cached = pd.get("cached_tokens") or 0
    pd["cached_tokens"] = existing_cached + pt
    cr = usage.get("cache_read_input_tokens")
    usage["cache_read_input_tokens"] = (cr or 0) + pt
    return obj


def _shift_usage_to_cached(body: bytes) -> bytes:
    """When a provider has no cached-input pricing, shift all input tokens
    to cached read tokens in the relayed usage payload so the user sees the
    same accounting the gateway uses internally."""
    try:
        data = json.loads(body)
    except Exception:
        return body
    result = _shift_usage_obj(data)
    if result is None:
        return body
    return json.dumps(result).encode("utf-8")


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
            if isinstance(obj, dict):
                result = _shift_usage_obj(obj)
                if result is not None:
                    changed = True
                    lines.append("data: " + json.dumps(result))
                    continue
        lines.append(line)
    if not changed:
        return chunk
    return ("\n".join(lines) + "\n").encode("utf-8")


def _record_circuit_failure(provider_id: str, backend_model: str, status_code: int | None, error: str | None) -> None:
    """Feed the circuit breaker with genuine backend failures only: 5xx,
    timeouts/connection errors (status 0). Never 4xx (client's fault) or 429
    (handled by the rate-limit cooldown path)."""
    if status_code is None:
        return
    if status_code == 0 or status_code >= 500:
        circuitbreaker.record_outcome(provider_id, backend_model, False, error)


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
    # max_completion_tokens (chat), max_tokens (chat/legacy), max_output_tokens
    # (Responses API). First present wins; chat bodies never set the last one.
    mt = request_body.get("max_completion_tokens")
    if mt is None:
        mt = request_body.get("max_tokens")
    if mt is None:
        mt = request_body.get("max_output_tokens")
    try:
        return int(mt) if mt is not None else None
    except (TypeError, ValueError):
        return None


def _context_too_small_error(
    config: GatewayConfig, model_cfg, input_tokens: int | None, max_tokens: int | None
) -> dict | None:
    """Error dict when prompt+completion can't fit ANY enabled backend's
    context window, else None. Distinguishes context-filter empties from
    "model not found" so clients get an actionable 400."""
    if input_tokens is None or model_cfg is None:
        return None
    contexts = [
        b.context_length
        for b in model_cfg.backends
        if b.enabled and b.context_length is not None
        and (p := config.provider_by_id(b.provider)) and p.enabled
    ]
    needed = input_tokens + (max_tokens or 0)
    if contexts and needed > max(contexts):
        return {
            "message": (
                f"Prompt + requested completion ({needed} tokens) exceeds the "
                f"largest backend context for '{model_cfg.id}' ({max(contexts)})"
            ),
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
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


def _provider_headers(provider_id: str, backend_model: str, reason: str = "", request_id: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "X-Provider": provider_id,
        "X-Backend": f"{provider_id}/{backend_model}",
    }
    if reason:
        h["X-Routing-Reason"] = reason
    if request_id:
        h["X-Request-Id"] = request_id
    return h


def _estimate_input_tokens(request_body: dict, config: GatewayConfig | None = None) -> int | None:
    """Input token estimate: tiktoken when installed (server.tokenizer=auto),
    else the chars/4 heuristic. Drives context-length backend filtering."""
    mode = "auto"
    if config is not None:
        mode = getattr(config.server, "tokenizer", "auto") or "auto"
    return tokenizer.count_messages(request_body.get("messages"), mode=mode)


def _input_estimate_needed(model_cfg, use_cache: bool) -> bool:
    """P4: the tiktoken estimate is ~5-20ms of CPU on large prompts. It only
    affects behavior when a backend enforces context_length (router filtering)
    or the response cache logs hits with it — otherwise the result is
    discarded. Gate on those conditions instead of paying it on every request."""
    if use_cache:
        return True
    if model_cfg is None:
        return False
    return any(
        b.enabled and getattr(b, "context_length", None) is not None
        for b in model_cfg.backends
    )


def _cache_enabled(config: GatewayConfig, model_cfg) -> bool:
    """True when response caching is globally enabled AND opted in on this model."""
    return (getattr(config.server, "response_cache_enabled", False)
            and getattr(model_cfg, "cache_responses", False) if model_cfg else False)


def _resolve_backends(
    config: GatewayConfig,
    logical_model: str,
    max_tokens: int | None,
    input_tokens: int | None = None,
    prefs: ProviderPrefs | None = None,
    cache_key: str | None = None,
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
            config, logical_model, max_tokens=max_tokens, input_tokens=input_tokens,
            prefs=prefs, cache_key=cache_key,
        )
    )
    if backends:
        return backends, None, 0

    ctx_err = _context_too_small_error(config, model, input_tokens, max_tokens)
    if ctx_err is not None:
        return [], ctx_err, 400

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
    api_key_id: str | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> tuple[bytes, int, dict[str, str]]:
    """Handle a non-streaming chat completion request with fallback.

    Returns (response_body, status_code, headers).
    """
    config = load_config()
    requested_model = request_body.get("model", "")
    model_cfg = config.model_by_id(requested_model)
    logical_model = model_cfg.id if model_cfg else requested_model
    end_user = request_body.get("user")
    if not isinstance(end_user, str) or not end_user:
        end_user = None
    prefs = ProviderPrefs.from_request(request_body)
    request_body = apply_default_params(
        request_body, model_cfg.default_params if model_cfg else None
    )
    upstream_body = _strip_gateway_fields(request_body)
    max_tokens = _requested_max_tokens(request_body)
    # F5: exact response cache — check before routing when the model opts in.
    use_cache = _cache_enabled(config, model_cfg) and respcache.is_cacheable(request_body)
    # P4: gate the hot-path CPU work on the features that actually need it.
    # fingerprint() hashes the whole message array even with cache affinity
    # OFF and no conversation id (the default), and tiktoken-encoding a 100KB
    # prompt is ~5-20ms of event-loop CPU whose result gets discarded when no
    # backend enforces context_length and caching is off.
    affinity_active = bool(
        getattr(config.server, "cache_affinity_enabled", False)
        and (conversation_id or model_cfg is not None)
    )
    cache_key = None
    if conversation_id:
        # F7: a client-supplied conversation_id is a stable cache key that keeps
        # all turns of one conversation on the same warm backend (fingerprint
        # rotates every turn since it hashes the message prefix).
        cache_key = "conv:" + conversation_id
    elif affinity_active:
        cache_key = fingerprint(request_body)
    input_tokens = None
    if _input_estimate_needed(model_cfg, use_cache):
        input_tokens = _estimate_input_tokens(request_body, config)

    cache_fp = respcache.full_fingerprint(request_body) if use_cache else None
    if cache_fp:
        hit = respcache.lookup(cache_fp, getattr(config.server, "response_cache_ttl_sec", 3600))
        if hit is not None:
            cached_body, _was_stream, _hits = hit
            log_request(
                logical_model=logical_model,
                provider="cache",
                backend_model="cache",
                success=True,
                input_tokens=input_tokens,
                cost=0.0,
                latency_ms=0,
                stream=False,
                api_key_id=api_key_id,
                end_user=end_user,
                request_id=request_id,
                cache_hit=True,
            )
            logs.info("cache hit", model=logical_model, request_id=request_id)
            return (
                cached_body,
                200,
                {"Content-Type": "application/json", "X-Cache-Hit": "true",
                 **({"X-Request-Id": request_id} if request_id else {})},
            )

    backends, err, status = _resolve_backends(
        config, logical_model, max_tokens, input_tokens, prefs=prefs, cache_key=cache_key
    )
    if err is not None:
        return (
            json.dumps({"error": err}).encode(),
            status,
            {"Content-Type": "application/json", **({"X-Request-Id": request_id} if request_id else {})},
        )

    errors: list[str] = []
    for selected in backends:
        stats.start_request(selected.provider.id, selected.backend.model)
        try:
            result = await call_provider(
                client,
                selected.provider,
                selected.backend.model,
                upstream_body,
                stream=False,
            )
        finally:
            stats.end_request(selected.provider.id, selected.backend.model)

        if result.success:
            stats.record_success(
                result.provider_id,
                result.backend_model,
                latency_ms=result.latency_ms,
            )
            circuitbreaker.record_outcome(result.provider_id, result.backend_model, True)
            stats.record_cache_activity(
                result.provider_id,
                result.backend_model,
                cache_key,
                result.cached_tokens,
                result.cache_write_tokens,
                result.input_tokens,
                getattr(selected.backend, "cache_supported", None),
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
                api_key_id=api_key_id,
                end_user=end_user,
                request_id=request_id,
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
                routing_reason=selected.reason,
                request_id=request_id,
            )
            if use_cache and cache_fp:
                respcache.store(
                    cache_fp, logical_model, body, False,
                    getattr(config.server, "response_cache_ttl_sec", 3600),
                    getattr(config.server, "response_cache_max_entries", 1000),
                )
            return (
                body,
                200,
                _provider_headers(result.provider_id, result.backend_model, selected.reason, request_id),
            )

        errors.append(f"[{selected.provider.id}:{selected.backend.model}] {result.error}")
        stats.record_failure(selected.provider.id, selected.backend.model, result.error)
        _record_circuit_failure(selected.provider.id, selected.backend.model, result.status_code, result.error)

        if not result.retryable:
            log_request(
                logical_model=logical_model,
                provider=result.provider_id,
                backend_model=result.backend_model,
                success=False,
                error=result.error,
                latency_ms=result.latency_ms,
                stream=False,
                api_key_id=api_key_id,
                end_user=end_user,
                request_id=request_id,
            )
            logs.error(
                f"request failed (non-retryable): {result.error}",
                model=logical_model,
                provider=result.provider_id,
                status_code=result.status_code,
                latency_ms=result.latency_ms,
                request_id=request_id,
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
                {"Content-Type": "application/json", **({"X-Request-Id": request_id} if request_id else {})},
            )

        log_request(
            logical_model=logical_model,
            provider=result.provider_id,
            backend_model=result.backend_model,
            success=False,
            error=result.error,
            latency_ms=result.latency_ms,
            stream=False,
            api_key_id=api_key_id,
            end_user=end_user,
            request_id=request_id,
        )
        logs.warn(
            f"backend failed, falling back: {result.error}",
            model=logical_model,
            provider=result.provider_id,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
            request_id=request_id,
        )

    logs.error(
        f"all backends failed: {'; '.join(errors)}",
        model=logical_model,
        request_id=request_id,
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
        {"Content-Type": "application/json", **({"X-Request-Id": request_id} if request_id else {})},
    )


async def handle_request_stream(
    client,
    request_body: dict,
    api_key_id: str | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
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
    end_user = request_body.get("user")
    if not isinstance(end_user, str) or not end_user:
        end_user = None
    prefs = ProviderPrefs.from_request(request_body)
    request_body = apply_default_params(
        request_body, model_cfg.default_params if model_cfg else None
    )
    upstream_body = _strip_gateway_fields(request_body)
    max_tokens = _requested_max_tokens(request_body)
    # F5: exact response cache — check before routing when the model opts in.
    use_cache = _cache_enabled(config, model_cfg) and respcache.is_cacheable(request_body)
    # P4: gate hot-path CPU (see handle_request for rationale).
    affinity_active = bool(
        getattr(config.server, "cache_affinity_enabled", False)
        and (conversation_id or model_cfg is not None)
    )
    cache_key = None
    if conversation_id:
        cache_key = "conv:" + conversation_id
    elif affinity_active:
        cache_key = fingerprint(request_body)
    input_tokens = None
    if _input_estimate_needed(model_cfg, use_cache):
        input_tokens = _estimate_input_tokens(request_body, config)
    cache_fp = respcache.full_fingerprint(request_body) if use_cache else None
    if cache_fp:
        hit = respcache.lookup(cache_fp, getattr(config.server, "response_cache_ttl_sec", 3600))
        if hit is not None:
            cached_body, _was_stream, _hits = hit
            log_request(
                logical_model=logical_model,
                provider="cache",
                backend_model="cache",
                success=True,
                input_tokens=input_tokens,
                cost=0.0,
                latency_ms=0,
                stream=True,
                api_key_id=api_key_id,
                end_user=end_user,
                request_id=request_id,
                cache_hit=True,
            )
            logs.info("stream cache hit", model=logical_model, request_id=request_id)
            yield {"headers": {"Content-Type": "text/event-stream", "X-Cache-Hit": "true",
                               **({"X-Request-Id": request_id} if request_id else {})}}
            yield cached_body
            return

    backends, err, status = _resolve_backends(
        config, logical_model, max_tokens, input_tokens, prefs=prefs, cache_key=cache_key
    )
    if err is not None:
        # B6: terminal pre-stream errors carry a real HTTP status in the meta
        # dict instead of streaming a misleading 200 + SSE error event.
        yield {"error": err, "status_code": status}
        return

    errors: list[str] = []
    collected_chunks: list[bytes] = []  # F5: accumulate for response cache store
    committed = False
    for selected in backends:
        t0 = time.monotonic()
        first_yielded = False
        ttft_ms: int | None = None
        transform_parser = _new_transform_parser()
        stats.start_request(selected.provider.id, selected.backend.model)
        pricing = config.pricing_for(selected.provider.id, selected.backend.model)

        try:
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
                    if not committed:
                        # No chunks came before completion (rare, e.g. an
                        # empty-response provider) — commit headers now so the
                        # meta contract (first yield is a dict) holds.
                        committed = True
                        yield {"headers": _provider_headers(selected.provider.id, selected.backend.model, selected.reason, request_id)}
                    circuitbreaker.record_outcome(selected.provider.id, selected.backend.model, True)
                    usage = event.usage
                    stats.record_cache_activity(
                        selected.provider.id,
                        selected.backend.model,
                        cache_key,
                        usage.cached_tokens,
                        usage.cache_write_tokens,
                        usage.input_tokens,
                        getattr(selected.backend, "cache_supported", None),
                    )
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
                        api_key_id=api_key_id,
                        end_user=end_user,
                        request_id=request_id,
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
                        routing_reason=selected.reason,
                        request_id=request_id,
                    )
                    tail = _flush_transform(selected.provider, transform_parser)
                    if tail:
                        if use_cache and cache_fp:
                            collected_chunks.append(tail)
                        yield tail
                    if not event.sent_done:
                        done = b"data: [DONE]\n\n"
                        if use_cache and cache_fp:
                            collected_chunks.append(done)
                        yield done
                    if use_cache and cache_fp and collected_chunks:
                        respcache.store(
                            cache_fp, logical_model, b"".join(collected_chunks), True,
                            getattr(config.server, "response_cache_ttl_sec", 3600),
                            getattr(config.server, "response_cache_max_entries", 1000),
                        )
                    return

                if isinstance(event, StreamFailed):
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    if not event.mid_stream:
                        errors.append(
                            f"[{selected.provider.id}:{selected.backend.model}] {event.error}"
                        )
                        stats.record_failure(selected.provider.id, selected.backend.model, event.error)
                        _record_circuit_failure(selected.provider.id, selected.backend.model, event.status_code, event.error)
                        log_request(
                            logical_model=logical_model,
                            provider=selected.provider.id,
                            backend_model=selected.backend.model,
                            success=False,
                            error=event.error,
                            latency_ms=latency_ms,
                            stream=True,
                            api_key_id=api_key_id,
                            end_user=end_user,
                            request_id=request_id,
                        )
                        if not event.retryable:
                            logs.error(
                                f"stream failed (non-retryable): {event.error}",
                                model=logical_model,
                                provider=selected.provider.id,
                                status_code=event.status_code,
                                latency_ms=latency_ms,
                                request_id=request_id,
                            )
                            if not committed:
                                # B6: pre-stream non-retryable failure — real
                                # status code (e.g. 4xx from the provider).
                                yield {
                                    "error": {
                                        "message": event.error or "Unknown error",
                                        "type": "api_error",
                                        "code": f"http_{event.status_code}",
                                    },
                                    "status_code": event.status_code or 502,
                                }
                                return
                            yield _error_sse(event.error, f"http_{event.status_code}")
                            yield b"data: [DONE]\n\n"
                            return
                        logs.warn(
                            f"stream backend failed, falling back: {event.error}",
                            model=logical_model,
                            provider=selected.provider.id,
                            status_code=event.status_code,
                            latency_ms=latency_ms,
                            request_id=request_id,
                        )
                        break  # try next backend

                    # Mid-stream failure: cannot fall back. Surface the error.
                    stats.record_failure(selected.provider.id, selected.backend.model, event.error)
                    _record_circuit_failure(selected.provider.id, selected.backend.model, event.status_code, event.error)
                    log_request(
                        logical_model=logical_model,
                        provider=selected.provider.id,
                        backend_model=selected.backend.model,
                        success=False,
                        error=event.error,
                        latency_ms=latency_ms,
                        ttft_ms=ttft_ms,
                        stream=True,
                        api_key_id=api_key_id,
                        end_user=end_user,
                        request_id=request_id,
                    )
                    logs.error(
                        f"stream failed mid-stream: {event.error}",
                        model=logical_model,
                        provider=selected.provider.id,
                        latency_ms=latency_ms,
                        request_id=request_id,
                    )
                    yield _error_sse(event.error, "stream_interrupted")
                    yield b"data: [DONE]\n\n"
                    return

                # StreamChunk
                if not first_yielded:
                    first_yielded = True
                    ttft_ms = int((time.monotonic() - t0) * 1000)
                if not committed:
                    # B7: commit-on-first-chunk — emit the definitive routing
                    # headers (provider/backend/reason of the backend that is
                    # actually serving) only at the point of no return, instead
                    # of provisional headers computed from backends[0] that lie
                    # after a pre-first-chunk fallback.
                    committed = True
                    yield {"headers": _provider_headers(selected.provider.id, selected.backend.model, selected.reason, request_id)}
                out = _transform_chunk(config, selected.provider, event.data, transform_parser)
                if out:
                    if pricing.cache_read is None:
                        out = _shift_stream_usage_to_cached(out)
                    if use_cache and cache_fp:
                        collected_chunks.append(out)
                    yield out
        finally:
            stats.end_request(selected.provider.id, selected.backend.model)

    # All backends failed before first chunk
    logs.error(
        f"all backends failed: {'; '.join(errors)}",
        model=logical_model,
        request_id=request_id,
    )
    if not committed:
        # B6: nothing was streamed — surface a real 502 instead of a
        # misleading 200 + SSE error event.
        yield {
            "error": {
                "message": f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
                "type": "api_error",
                "code": "all_backends_failed",
            },
            "status_code": 502,
        }
        return
    yield _error_sse(
        f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
        "all_backends_failed",
    )
    yield b"data: [DONE]\n\n"


def _maybe_normalize_reasoning(config: GatewayConfig, provider, body: bytes) -> bytes:
    """Phase 2 hook: normalize reasoning fields in non-stream responses."""
    return normalize_response_body(provider, body)


def _new_transform_parser():
    return SSEParser()


def _transform_chunk(config: GatewayConfig, provider, chunk: bytes, parser) -> bytes:
    """Phase 2 hook: normalize reasoning fields in SSE chunks."""
    return transform_stream_chunk(provider, chunk, parser)


def _flush_transform(provider, parser) -> bytes:
    return flush_stream_chunk(provider, parser)

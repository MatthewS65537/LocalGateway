"""Responses API endpoint: POST /v1/responses (+ /api/v1 alias).

OpenAI's newer stateful API surface, now supported by OpenRouter. Routes
through the same tier/failover engine as chat. Logical models opt in via
``endpoint: "responses"`` in config. Supports both streaming and non-streaming;
usage is extracted from the ``response.completed`` event (stream) or the
response body's ``usage`` field (non-stream).
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import load_config
from ..provider import call_provider, call_provider_stream, StreamChunk, StreamDone, StreamFailed
from ..router import ProviderPrefs, select_backends
from ..streaming import (
    _provider_headers, _strip_gateway_fields, _compute_cost, _estimate_input_tokens,
    apply_default_params, fingerprint, resolve_request_id,
)
from ..sse import SSEParser, StreamUsage
from .. import circuitbreaker
from .. import logs
from .. import stats
from .. import usage as usage_mod
from .chat import _check_model_allowlist, _key_id

router = APIRouter()


def _extract_responses_usage(body: bytes) -> StreamUsage:
    """Extract usage from a non-streaming Responses API body."""
    try:
        data = json.loads(body)
        u = data.get("usage")
        if isinstance(u, dict):
            out = StreamUsage()
            it = u.get("input_tokens")
            ot = u.get("output_tokens")
            if isinstance(it, int):
                out.input_tokens = it
            if isinstance(ot, int):
                out.output_tokens = ot
            return out
    except Exception:
        pass
    return StreamUsage()


class _ResponsesUsageTracker:
    """Lightweight observer that watches SSE bytes for response.completed events
    to extract usage from a streaming Responses API response."""

    def __init__(self):
        self.parser = SSEParser()
        self.usage = StreamUsage()
        self.ttft_ms: int | None = None
        self._start = time.monotonic()
        self._saw_first_delta = False

    def feed(self, chunk: bytes):
        for ev in self.parser.feed(chunk):
            self._observe(ev)
        if chunk.strip() and self.ttft_ms is None and not self._saw_first_delta:
            # Fall back: any non-empty chunk marks TTFT if we haven't seen a delta.
            pass

    def flush(self):
        ev = self.parser.flush()
        if ev is not None:
            self._observe(ev)

    def _observe(self, ev):
        if ev.done:
            return
        try:
            obj = json.loads(ev.data)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        evt_type = obj.get("type", "")
        if evt_type == "response.output_text.delta" and self.ttft_ms is None:
            self._saw_first_delta = True
            self.ttft_ms = int((time.monotonic() - self._start) * 1000)
        elif evt_type == "response.completed":
            resp = obj.get("response", {})
            u = resp.get("usage") if isinstance(resp, dict) else None
            if isinstance(u, dict):
                it = u.get("input_tokens")
                ot = u.get("output_tokens")
                if isinstance(it, int):
                    self.usage.input_tokens = it
                if isinstance(ot, int):
                    self.usage.output_tokens = ot
        elif evt_type == "response.incomplete":
            # Some providers send usage on incomplete responses too.
            resp = obj.get("response", {})
            u = resp.get("usage") if isinstance(resp, dict) else None
            if isinstance(u, dict):
                it = u.get("input_tokens")
                ot = u.get("output_tokens")
                if isinstance(it, int):
                    self.usage.input_tokens = it
                if isinstance(ot, int):
                    self.usage.output_tokens = ot


@router.post("/v1/responses")
@router.post("/api/v1/responses")
async def create_response(request: Request):
    body = await request.json()
    rejected = _check_model_allowlist(request, body)
    if rejected is not None:
        return rejected
    config = load_config()
    requested_model = body.get("model", "")
    model_cfg = config.model_by_id(requested_model)

    if model_cfg is None or not model_cfg.enabled:
        return JSONResponse(
            {"error": {
                "message": f"Model '{requested_model}' not found or has no configured backends",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }},
            status_code=404,
        )
    if getattr(model_cfg, "endpoint", "chat") != "responses":
        return JSONResponse(
            {"error": {
                "message": f"Model '{model_cfg.id}' is not a responses model (endpoint={getattr(model_cfg, 'endpoint', 'chat')}). Configure endpoint='responses' on a dedicated logical model.",
                "type": "invalid_request_error",
                "code": "wrong_endpoint",
            }},
            status_code=400,
        )

    logical_model = model_cfg.id
    key_id = _key_id(request)
    request_id = resolve_request_id(request.headers.get("x-request-id"))
    end_user = body.get("user")
    if not isinstance(end_user, str) or not end_user:
        end_user = None
    stream = body.get("stream", False)
    prefs = ProviderPrefs.from_request(body)
    body = apply_default_params(body, model_cfg.default_params)
    upstream_body = _strip_gateway_fields(body)
    input_tokens = _estimate_input_tokens(body, config)
    cache_key = fingerprint(body)

    backends = list(select_backends(config, logical_model, input_tokens=input_tokens, prefs=prefs, cache_key=cache_key))
    if not backends:
        return JSONResponse(
            {"error": {
                "message": f"Model '{logical_model}' not found or has no configured backends",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }},
            status_code=404,
            headers={"X-Request-Id": request_id},
        )

    client = request.app.state.http_client

    if stream:
        agen = _responses_stream_gen(client, config, backends, upstream_body, logical_model, key_id, end_user, cache_key, request_id)
        meta = await agen.__anext__()
        if isinstance(meta, dict) and "error" in meta:
            # B6: terminal pre-stream errors carry a real HTTP status.
            await agen.aclose()
            return JSONResponse(
                {"error": meta["error"]},
                status_code=meta.get("status_code", 502),
                headers={"X-Request-Id": request_id},
            )
        extra_headers = meta.get("headers", {}) if isinstance(meta, dict) else {}

        async def byte_stream():
            async for item in agen:
                if isinstance(item, (bytes, bytearray)):
                    yield item

        return StreamingResponse(
            byte_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Request-Id": request_id,
                **{k: v for k, v in extra_headers.items() if k.lower() != "content-type"},
            },
        )
    return await _handle_responses_nonstream(client, config, backends, upstream_body, logical_model, key_id, end_user, input_tokens, request_id)


async def _handle_responses_nonstream(client, config, backends, upstream_body, logical_model, key_id, end_user, input_tokens, request_id):
    errors: list[str] = []
    for selected in backends:
        stats.start_request(selected.provider.id, selected.backend.model)
        try:
            result = await call_provider(
                client, selected.provider, selected.backend.model,
                upstream_body, stream=False, path="/responses",
            )
        finally:
            stats.end_request(selected.provider.id, selected.backend.model)

        if result.success:
            stats.record_success(result.provider_id, result.backend_model, latency_ms=result.latency_ms)
            circuitbreaker.record_outcome(result.provider_id, result.backend_model, True)
            u = _extract_responses_usage(result.body)
            usage_mod.log_request(
                logical_model=logical_model,
                provider=result.provider_id,
                backend_model=result.backend_model,
                success=True,
                input_tokens=u.input_tokens or input_tokens,
                output_tokens=u.output_tokens,
                cost=_compute_cost(config, result.provider_id, result.backend_model,
                                   u.input_tokens or input_tokens, u.output_tokens, None, None),
                latency_ms=result.latency_ms,
                stream=False,
                api_key_id=key_id,
                end_user=end_user,
                request_id=request_id,
            )
            logs.info("responses ok", model=logical_model, provider=result.provider_id,
                      status_code=200, latency_ms=result.latency_ms,
                      input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      routing_reason=selected.reason, request_id=request_id)
            return Response(
                content=result.body, status_code=200,
                headers=_provider_headers(result.provider_id, result.backend_model, selected.reason, request_id),
            )

        errors.append(f"[{selected.provider.id}:{selected.backend.model}] {result.error}")
        stats.record_failure(selected.provider.id, selected.backend.model, result.error)
        if result.status_code == 0 or result.status_code >= 500:
            circuitbreaker.record_outcome(selected.provider.id, selected.backend.model, False, result.error)
        usage_mod.log_request(
            logical_model=logical_model, provider=selected.provider.id,
            backend_model=selected.backend.model, success=False,
            error=result.error, latency_ms=result.latency_ms, stream=False,
            api_key_id=key_id, end_user=end_user, request_id=request_id,
        )
        if not result.retryable:
            logs.error("responses failed (non-retryable): " + str(result.error),
                       model=logical_model, provider=selected.provider.id,
                       status_code=result.status_code, request_id=request_id)
            return JSONResponse(
                {"error": {"message": result.error or "Unknown error",
                           "type": "api_error", "code": f"http_{result.status_code}"}},
                status_code=result.status_code or 502,
                headers={"X-Request-Id": request_id},
            )
        logs.warn("responses backend failed, falling back: " + str(result.error),
                  model=logical_model, provider=selected.provider.id,
                  status_code=result.status_code, request_id=request_id)

    logs.error(f"all backends failed: {'; '.join(errors)}", model=logical_model, request_id=request_id)
    return JSONResponse(
        {"error": {"message": f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
                   "type": "api_error", "code": "all_backends_failed"}},
        status_code=502,
        headers={"X-Request-Id": request_id},
    )


def _responses_stream_gen(client, config, backends, upstream_body, logical_model, key_id, end_user, cache_key, request_id):
    """Streaming Responses API with pre-first-chunk fallback.

    B6/B7: no provisional headers — the first yield is either a real error
    dict (with HTTP status) when nothing was streamed, or the definitive
    provider/backend headers at commit-on-first-chunk. The Responses API has
    no [DONE] sentinel (its terminal event is response.completed), so the
    chat-style [DONE] trailers are dropped on this path.
    """
    async def gen():
        errors: list[str] = []
        committed = False
        for selected in backends:
            t0 = time.monotonic()
            tracker = _ResponsesUsageTracker()
            stats.start_request(selected.provider.id, selected.backend.model)
            try:
                async for event in call_provider_stream(
                    client, selected.provider, selected.backend.model,
                    upstream_body, path="/responses",
                ):
                    if isinstance(event, StreamChunk):
                        tracker.feed(event.data)
                        if not committed:
                            committed = True
                            yield {"headers": _provider_headers(selected.provider.id, selected.backend.model, selected.reason, request_id)}
                        yield event.data
                    elif isinstance(event, StreamDone):
                        tracker.flush()
                        u = tracker.usage
                        stats.record_success(selected.provider.id, selected.backend.model,
                                              latency_ms=event.latency_ms, ttft_ms=tracker.ttft_ms)
                        circuitbreaker.record_outcome(selected.provider.id, selected.backend.model, True)
                        stats.record_cache_activity(
                            selected.provider.id, selected.backend.model, cache_key,
                            u.cached_tokens, u.cache_write_tokens, u.input_tokens,
                            getattr(selected.backend, "cache_supported", None),
                        )
                        usage_mod.log_request(
                            logical_model=logical_model, provider=selected.provider.id,
                            backend_model=selected.backend.model, success=True,
                            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                            cost=_compute_cost(config, selected.provider.id, selected.backend.model,
                                               u.input_tokens, u.output_tokens, None, None),
                            latency_ms=event.latency_ms, ttft_ms=tracker.ttft_ms,
                            stream=True, api_key_id=key_id, end_user=end_user,
                            request_id=request_id,
                        )
                        logs.info("responses stream ok", model=logical_model,
                                  provider=selected.provider.id, status_code=200,
                                  latency_ms=event.latency_ms, ttft_ms=tracker.ttft_ms,
                                  input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                                  routing_reason=selected.reason, request_id=request_id)
                        if not committed:
                            committed = True
                            yield {"headers": _provider_headers(selected.provider.id, selected.backend.model, selected.reason, request_id)}
                        return
                    elif isinstance(event, StreamFailed):
                        if not event.mid_stream:
                            errors.append(f"[{selected.provider.id}:{selected.backend.model}] {event.error}")
                            stats.record_failure(selected.provider.id, selected.backend.model, event.error)
                            if event.status_code == 0 or event.status_code >= 500:
                                circuitbreaker.record_outcome(selected.provider.id, selected.backend.model, False, event.error)
                            # B5: pre-first-chunk failures were never logged —
                            # fallback attempts on /v1/responses were invisible
                            # in the usage DB and Logs page. Mirror the chat path.
                            latency_ms = int((time.monotonic() - t0) * 1000)
                            usage_mod.log_request(
                                logical_model=logical_model, provider=selected.provider.id,
                                backend_model=selected.backend.model, success=False,
                                error=event.error, latency_ms=latency_ms, stream=True,
                                api_key_id=key_id, end_user=end_user, request_id=request_id,
                            )
                            if not event.retryable:
                                logs.error("responses stream failed (non-retryable): " + str(event.error),
                                           model=logical_model, provider=selected.provider.id,
                                           status_code=event.status_code, request_id=request_id)
                                if not committed:
                                    yield {
                                        "error": {"message": event.error or "Unknown error",
                                                  "type": "api_error", "code": f"http_{event.status_code}"},
                                        "status_code": event.status_code or 502,
                                    }
                                    return
                                yield _error_sse(event.error, f"http_{event.status_code}")
                                return
                            logs.warn("responses stream backend failed, falling back: " + str(event.error),
                                      model=logical_model, provider=selected.provider.id,
                                      status_code=event.status_code, latency_ms=latency_ms,
                                      request_id=request_id)
                            continue
                        yield _error_sse(event.error, "stream_error")
                        return
            finally:
                stats.end_request(selected.provider.id, selected.backend.model)
        # All backends failed pre-stream
        if not committed:
            yield {
                "error": {"message": errors[-1] if errors else "All backends failed",
                          "type": "api_error", "code": "all_backends_failed"},
                "status_code": 502,
            }
            return
        err = errors[-1] if errors else "All backends failed"
        yield _error_sse(err, "all_backends_failed")

    return gen()


def _error_sse(message: str, code: str) -> bytes:
    body = json.dumps({"error": {"message": message, "type": "api_error", "code": code}})
    return f"data: {body}\n\n".encode()

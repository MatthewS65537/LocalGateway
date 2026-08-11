"""Embeddings endpoint: POST /v1/embeddings (+ /api/v1 alias).

Routes through the same tier/failover engine as chat (no streaming — the
embeddings API is request/response). Logical models opt in via
``endpoint: "embeddings"`` in config; their backends are upstream embedding
model IDs. Cost is computed from ``usage.total_tokens`` × the input price.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config import load_config
from ..provider import call_provider
from ..router import ProviderPrefs, select_backends
from ..streaming import _provider_headers, _strip_gateway_fields, apply_default_params, resolve_request_id
from .. import circuitbreaker
from .. import logs
from .. import stats
from .. import tokenizer
from ..usage import log_request
from .chat import _check_model_allowlist, _key_id

router = APIRouter()


def _extract_embedding_tokens(body: bytes) -> int | None:
    try:
        data = json.loads(body)
        usage = data.get("usage")
        if isinstance(usage, dict):
            t = usage.get("total_tokens") or usage.get("prompt_tokens")
            if isinstance(t, int):
                return t
    except Exception:
        pass
    return None


@router.post("/v1/embeddings")
@router.post("/api/v1/embeddings")
async def create_embeddings(request: Request):
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
    if getattr(model_cfg, "endpoint", "chat") != "embeddings":
        return JSONResponse(
            {"error": {
                "message": f"Model '{model_cfg.id}' is not an embeddings model (endpoint=chat). Configure endpoint='embeddings' on a dedicated logical model.",
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
    mode = getattr(config.server, "tokenizer", "auto") or "auto"
    input_tokens = tokenizer.count_embedding_input(body.get("input"), mode=mode)
    prefs = ProviderPrefs.from_request(body)
    body = apply_default_params(body, model_cfg.default_params)
    upstream_body = _strip_gateway_fields(body)

    backends = list(select_backends(config, logical_model, input_tokens=input_tokens, prefs=prefs))
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
                path="/embeddings",
            )
        finally:
            stats.end_request(selected.provider.id, selected.backend.model)

        if result.success:
            stats.record_success(result.provider_id, result.backend_model, latency_ms=result.latency_ms)
            circuitbreaker.record_outcome(result.provider_id, result.backend_model, True)
            total_tokens = _extract_embedding_tokens(result.body) or result.input_tokens or input_tokens
            cost = None
            if total_tokens is not None:
                pricing = config.pricing_for(result.provider_id, result.backend_model)
                cost = total_tokens * (pricing.input or 0.0)
            log_request(
                logical_model=logical_model,
                provider=result.provider_id,
                backend_model=result.backend_model,
                success=True,
                input_tokens=total_tokens,
                output_tokens=0,
                cost=cost,
                latency_ms=result.latency_ms,
                stream=False,
                api_key_id=key_id,
                end_user=end_user,
                request_id=request_id,
            )
            logs.info(
                "embeddings ok",
                model=logical_model,
                provider=result.provider_id,
                status_code=200,
                latency_ms=result.latency_ms,
                input_tokens=total_tokens,
                routing_reason=selected.reason,
                request_id=request_id,
            )
            return Response(
                content=result.body,
                status_code=200,
                headers=_provider_headers(result.provider_id, result.backend_model, selected.reason, request_id),
            )

        errors.append(f"[{selected.provider.id}:{selected.backend.model}] {result.error}")
        stats.record_failure(selected.provider.id, selected.backend.model, result.error)
        if result.status_code == 0 or result.status_code >= 500:
            circuitbreaker.record_outcome(selected.provider.id, selected.backend.model, False, result.error)
        log_request(
            logical_model=logical_model,
            provider=result.provider_id,
            backend_model=result.backend_model,
            success=False,
            error=result.error,
            latency_ms=result.latency_ms,
            stream=False,
            api_key_id=key_id,
            end_user=end_user,
            request_id=request_id,
        )
        if not result.retryable:
            logs.error(
                f"embeddings failed (non-retryable): {result.error}",
                model=logical_model,
                provider=result.provider_id,
                status_code=result.status_code,
                request_id=request_id,
            )
            return JSONResponse(
                {"error": {
                    "message": result.error or "Unknown error",
                    "type": "api_error",
                    "code": f"http_{result.status_code}",
                }},
                status_code=result.status_code or 502,
                headers={"X-Request-Id": request_id},
            )
        logs.warn(
            f"embeddings backend failed, falling back: {result.error}",
            model=logical_model,
            provider=result.provider_id,
            status_code=result.status_code,
            request_id=request_id,
        )

    logs.error(f"all backends failed: {'; '.join(errors)}", model=logical_model, request_id=request_id)
    return JSONResponse(
        {"error": {
            "message": f"All backends failed for model '{logical_model}'. Errors: {'; '.join(errors)}",
            "type": "api_error",
            "code": "all_backends_failed",
        }},
        status_code=502,
        headers={"X-Request-Id": request_id},
    )

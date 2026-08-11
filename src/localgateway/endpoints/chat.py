from __future__ import annotations

from fastapi import APIRouter, Request

from ..streaming import handle_request, handle_request_stream, resolve_request_id

router = APIRouter()


def _key_id(request: Request) -> str | None:
    key_cfg = getattr(request.state, "api_key_cfg", None)
    return key_cfg.id if key_cfg and key_cfg.id else None


def _check_model_allowlist(request: Request, body: dict):
    """403 when the resolved API key has a model allowlist that excludes the
    requested model. Returns a Response on rejection, else None."""
    key_cfg = getattr(request.state, "api_key_cfg", None)
    if not key_cfg or not key_cfg.model_allowlist:
        return None
    from ..config import load_config
    cfg = load_config()
    requested = body.get("model", "")
    model = cfg.model_by_id(requested)
    logical = model.id if model else requested
    allowed = set(key_cfg.model_allowlist)
    if logical not in allowed and requested not in allowed:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"error": {
                "message": f"Model '{requested}' is not allowed for API key '{key_cfg.id}'",
                "type": "forbidden",
                "code": "model_not_allowed",
            }},
            status_code=403,
        )
    return None


def _budget_warn_headers(request: Request) -> dict[str, str]:
    """Soft warning headers when a key crosses 80% of a budget, plus the
    burn-rate exhaustion ETA (F4) when a budget is configured."""
    key_cfg = getattr(request.state, "api_key_cfg", None)
    if not key_cfg or not key_cfg.id:
        return {}
    from .. import clientquota
    headers: dict[str, str] = {}
    warns = clientquota.budget_warnings(key_cfg)
    if warns:
        period, spent, limit = warns[0]
        headers["X-Budget-Warning"] = f"{period} budget at {spent / limit * 100:.0f}% (${spent:.4f} of ${limit:.2f})"
    eta = clientquota.budget_eta(key_cfg)
    if eta and eta.get("eta_days") is not None:
        headers["X-Budget-ETA"] = f"{eta['period']} budget exhausted in {eta['eta_days']}d ({eta['eta_date']})"
        if eta["eta_days"] < 3:
            # Fire an alert webhook for imminent exhaustion (fire-and-forget,
            # throttled to one per key per 6h — B13).
            try:
                from .. import alerts
                alerts.send("budget_eta_warning",
                            f"{eta['period']} budget for key '{key_cfg.id}' exhausted in {eta['eta_days']}d",
                            {"key": key_cfg.id, **eta},
                            dedup_key=f"eta:{key_cfg.id}")
            except Exception:
                pass
    return headers


@router.post("/v1/chat/completions")
@router.post("/api/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    rejected = _check_model_allowlist(request, body)
    if rejected is not None:
        return rejected
    stream = body.get("stream", False)
    client = request.app.state.http_client
    key_id = _key_id(request)
    warn_headers = _budget_warn_headers(request)
    request_id = resolve_request_id(request.headers.get("x-request-id"))
    conversation_id = request.headers.get("x-conversation-id") or body.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        conversation_id = None

    if stream:
        from fastapi.responses import JSONResponse, StreamingResponse

        agen = handle_request_stream(client, body, api_key_id=key_id, request_id=request_id, conversation_id=conversation_id)
        meta = await agen.__anext__()
        if isinstance(meta, dict) and "error" in meta:
            # B6: terminal pre-stream errors (model not found, all backends
            # failed, non-retryable provider 4xx) now carry a real HTTP status
            # instead of a misleading 200 + SSE error event. Drain the
            # generator (it has finished) so nothing leaks.
            err = meta.get("error") or {"message": "Unknown error", "type": "api_error"}
            status_code = meta.get("status_code", 502)
            await agen.aclose()
            return JSONResponse(
                {"error": err},
                status_code=status_code,
                headers={"X-Request-Id": request_id, **warn_headers},
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
                **warn_headers,
                **{k: v for k, v in extra_headers.items() if k.lower() != "content-type"},
            },
        )
    else:
        response_body, status_code, headers = await handle_request(client, body, api_key_id=key_id, request_id=request_id, conversation_id=conversation_id)
        from fastapi.responses import Response
        return Response(
            content=response_body,
            status_code=status_code,
            headers={"X-Request-Id": request_id, **warn_headers, **headers},
        )

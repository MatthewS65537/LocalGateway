from __future__ import annotations

from fastapi import APIRouter, Request

from ..streaming import handle_request, handle_request_stream

router = APIRouter()


@router.post("/v1/chat/completions")
@router.post("/api/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = body.get("stream", False)
    client = request.app.state.http_client

    if stream:
        from fastapi.responses import StreamingResponse

        agen = handle_request_stream(client, body)
        meta = await agen.__anext__()
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
                **{k: v for k, v in extra_headers.items() if k.lower() != "content-type"},
            },
        )
    else:
        response_body, status_code, headers = await handle_request(client, body)
        from fastapi.responses import Response
        return Response(
            content=response_body,
            status_code=status_code,
            headers=headers,
        )

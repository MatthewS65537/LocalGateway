"""Mock OpenAI-compatible provider used by the test suite.

Special model names trigger failure modes:
- mock-dead   -> HTTP 500
- mock-429    -> HTTP 429 with Retry-After
- mock-cut    -> (stream) drops the connection mid-stream
- mock-stall  -> (stream) sleeps before the first chunk (triggers idle timeout)

Normal streaming emits 20 chunks x ~50ms with a final usage event
(200 completion tokens, 50 reasoning) -> ~200 tps.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def create_mock_app(name: str = "mock") -> FastAPI:
    app = FastAPI()
    app.state.calls = defaultdict(int)

    @app.get("/v1/models")
    async def models():
        return JSONResponse({
            "object": "list",
            "data": [
                {"id": "mock-reasoner", "context_length": 32768, "owned_by": name},
                {"id": "mock-plain", "owned_by": name},
            ],
        })

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        model = body.get("model", "")
        stream = body.get("stream", False)
        app.state.calls[model] += 1

        if model == "mock-dead":
            return JSONResponse({"error": {"message": "model exploded"}}, status_code=500)

        if model == "mock-429":
            return JSONResponse(
                {"error": {"message": "slow down"}},
                status_code=429,
                headers={"Retry-After": "30"},
            )

        if model == "mock-cut" and stream:
            async def cut():
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n\n'
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise RuntimeError("simulated connection drop")
            return StreamingResponse(cut(), media_type="text/event-stream")

        if model == "mock-stall" and stream:
            async def stall():
                await asyncio.sleep(3)
                yield b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'
                yield b"data: [DONE]\n\n"
            return StreamingResponse(stall(), media_type="text/event-stream")

        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 50},
        }

        if not stream:
            return JSONResponse({
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hello from " + name,
                        "reasoning_content": "I thought about it",
                    },
                    "finish_reason": "stop",
                }],
                "usage": usage,
            })

        async def gen():
            for i in range(20):
                delta = {"content": "0123456789"}
                if i == 0:
                    delta = {"role": "assistant", "reasoning_content": "think", "content": "0123456789"}
                yield f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n".encode()
                await asyncio.sleep(0.05)
            yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n".encode()
            if body.get("stream_options", {}).get("include_usage"):
                yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app

from __future__ import annotations

import argparse

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config, set_config_path
from .auth import check_api_key, is_page_path
from .usage import set_db_path
from . import logs
from . import prober
from . import usage
from .endpoints import chat_router, models_router, admin_router, web_router


def create_app(config_path: str = "config.json") -> FastAPI:
    set_config_path(config_path)
    set_db_path("data/usage.db")
    logs.set_db_path("data/usage.db")

    config = load_config()
    app = FastAPI(
        title="LocalGateway Worker",
        description="Local OpenAI-compatible token gateway with intelligent fallback",
        version="0.1.0",
    )

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        cfg = load_config()
        api_key = cfg.server.api_key
        path = request.url.path
        # Admin routes and UI pages: loopback is always allowed (the supervisor
        # enforces the key for remote clients before proxying here); everyone
        # else needs the key. API routes always require the key when one is set.
        if path.startswith("/admin") or is_page_path(path):
            if not check_api_key(request, cfg):
                return JSONResponse(
                    {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
        elif api_key and (path.startswith("/v1") or path.startswith("/api/v1")):
            auth = request.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer") else auth
            if token != api_key:
                return JSONResponse(
                    {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
        return await call_next(request)

    app.include_router(chat_router)
    app.include_router(models_router)
    app.include_router(admin_router)
    app.include_router(web_router)

    @app.on_event("startup")
    async def startup():
        logs.info("Gateway worker started", provider="worker")
        cfg = load_config()
        usage.enforce_retention(
            cfg.server.usage_retention_days,
            cfg.server.log_retention_lines,
        )
        prober.start_prober(app.state.http_client)

    @app.on_event("shutdown")
    async def shutdown():
        from .ratelimit import ratelimit
        ratelimit.save()
        prober.stop_prober()
        await app.state.http_client.aclose()
        logs.info("Gateway worker shutting down", provider="worker")

    return app


def run_worker(config_path: str, host: str, port: int) -> None:
    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main():
    parser = argparse.ArgumentParser(description="LocalGateway worker (internal)")
    parser.add_argument("--config", "-c", default="config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", "-p", type=int, default=3456)
    args = parser.parse_args()
    run_worker(args.config, args.host, args.port)


if __name__ == "__main__":
    main()

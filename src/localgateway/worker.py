from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config, set_config_path
from .auth import check_api_key, is_page_path, resolve_api_key
from .usage import set_db_path
from . import clientquota
from . import logs
from . import prober
from . import respcache
from . import usage
from .endpoints import chat_router, embeddings_router, models_router, admin_router, web_router
from . import events


def create_app(config_path: str = "config.json") -> FastAPI:
    set_config_path(config_path)
    set_db_path("data/usage.db")
    logs.set_db_path("data/usage.db")
    respcache.set_db_path("data/usage.db")

    config = load_config()

    @asynccontextmanager
    async def lifespan(app):
        logs.info("Gateway worker started", provider="worker")
        cfg = load_config()
        # P6: retention is fully synchronous (DELETEs + possible VACUUM) and
        # used to block the event loop for seconds at startup — run it in a
        # thread so the gateway serves traffic immediately.
        await asyncio.to_thread(
            usage.enforce_retention,
            cfg.server.usage_retention_days,
            cfg.server.log_retention_lines,
        )
        prober.start_prober(app.state.http_client)
        app.state.retention_task = asyncio.create_task(_retention_loop())
        events.set_loop(asyncio.get_running_loop())
        app.state.event_tick_task = asyncio.create_task(_event_tick_loop())
        yield
        from .ratelimit import ratelimit
        ratelimit.save()
        prober.stop_prober()
        task = getattr(app.state, "retention_task", None)
        if task:
            task.cancel()
        etask = getattr(app.state, "event_tick_task", None)
        if etask:
            etask.cancel()
        events.set_loop(None)
        await app.state.http_client.aclose()
        from .provider import close_provider_clients
        await close_provider_clients()
        logs.info("Gateway worker shutting down", provider="worker")
        from . import usage as _usage_mod
        from . import logs as _logs_mod
        _usage_mod.stop_writer()
        _logs_mod.stop_writer()
        from . import respcache as _respcache_mod
        _respcache_mod.stop_writer()

    app = FastAPI(
        title="LocalGateway Worker",
        description="Local OpenAI-compatible token gateway with intelligent fallback",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    # P8: mark the shared pool so provider calls route through per-provider
    # pools (see provider.get_provider_client); the shared client keeps
    # serving admin/probe traffic.
    app.state.http_client._lg_global_pool = True

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        cfg = load_config()
        path = request.url.path
        # Admin routes and UI pages: loopback is always allowed (the supervisor
        # enforces the key for remote clients before proxying here); everyone
        # else needs the key. API routes always require a key when any are set.
        if path.startswith("/admin") or is_page_path(path):
            if not check_api_key(request, cfg):
                return JSONResponse(
                    {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
        elif path.startswith("/v1") or path.startswith("/api/v1"):
            if cfg.server.api_key or cfg.server.api_keys:
                key_cfg = resolve_api_key(request, cfg)
                if key_cfg is None:
                    return JSONResponse(
                        {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                        status_code=401,
                    )
                request.state.api_key_cfg = key_cfg
                # Inbound quotas: RPM token bucket, then budget hard stop.
                retry = clientquota.check_rpm(key_cfg)
                if retry is not None:
                    return JSONResponse(
                        {"error": {
                            "message": f"Rate limit exceeded for API key '{key_cfg.id}' ({key_cfg.rpm} rpm)",
                            "type": "rate_limit_error",
                            "code": "rpm_exceeded",
                        }},
                        status_code=429,
                        headers={"Retry-After": str(retry)},
                    )
                exceeded = clientquota.check_budget(key_cfg)
                if exceeded is not None:
                    period, spent, limit = exceeded
                    try:
                        from . import alerts
                        alerts.send("budget_exceeded", f"Budget exceeded for key '{key_cfg.id}'",
                                    {"key": key_cfg.id, "period": period, "spent": spent, "limit": limit})
                    except Exception:
                        pass
                    return JSONResponse(
                        {"error": {
                            "message": f"{period.capitalize()} budget exceeded for API key '{key_cfg.id}' (${spent:.4f} of ${limit:.2f})",
                            "type": "billing_error",
                            "code": "budget_exceeded",
                        }},
                        status_code=402,
                    )
            else:
                request.state.api_key_cfg = resolve_api_key(request, cfg)
        return await call_next(request)

    app.include_router(chat_router)
    app.include_router(embeddings_router)
    app.include_router(models_router)
    from .endpoints.responses import router as responses_router
    app.include_router(responses_router)
    from .endpoints.metrics import router as metrics_router
    app.include_router(metrics_router)
    from .endpoints.events import router as events_router
    app.include_router(events_router)
    app.include_router(admin_router)
    app.include_router(web_router)
    return app


async def _retention_loop() -> None:
    """Re-run retention daily (previously only enforced at worker startup).
    P6: off-loop via asyncio.to_thread — never block request handling."""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            cfg = load_config()
            await asyncio.to_thread(
                usage.enforce_retention,
                cfg.server.usage_retention_days,
                cfg.server.log_retention_lines,
            )
            logs.info("Periodic retention pass complete", provider="worker")
            try:
                anomalies = await asyncio.to_thread(usage.daily_anomaly_scan)
                if anomalies:
                    logs.warn(f"Cost anomaly scan found {len(anomalies)} spike(s)", provider="worker")
            except Exception as e:
                logs.warn(f"anomaly scan failed: {e}", provider="worker")
        except Exception as e:
            logs.warn(f"periodic retention failed: {e}", provider="worker")


async def _event_tick_loop() -> None:
    """L1: publish a tick event every 2s while the event bus has subscribers.

    Carries a monotonically increasing state_version (bumped by stats/circuit
    mutations) plus the inflight map, so subscribers can skip refetches when
    nothing changed. Idle cost is near-zero: the loop sleeps when no UI is
    connected.
    """
    from . import stats
    while True:
        await asyncio.sleep(2.0)
        if events.subscriber_count() == 0:
            continue
        try:
            snap = stats.snapshot()
            inflight = {k: v.get("in_flight", 0) for k, v in snap.items()}
            events.publish("tick", {
                "version": events.get_state_version(),
                "inflight": inflight,
            })
        except Exception:
            pass


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

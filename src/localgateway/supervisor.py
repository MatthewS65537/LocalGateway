from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import logs
from .auth import check_api_key, is_loopback_host, is_page_path, resolve_api_key
from .config import config_recovered_from, load_config, set_config_path
from .usage import set_db_path
from .endpoints.admin import router as admin_router
from .endpoints.web import NoCacheStaticFiles, router as web_router

HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}
RESP_STRIP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


class Supervisor:
    """Manages the gateway worker subprocess and exposes start/stop/restart."""

    def __init__(self, config_path: str, host: str, port: int, worker_port: int):
        self.config_path = config_path
        self.host = host
        self.port = port
        self.worker_port = worker_port
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.started_at: float | None = None

    def worker_base(self) -> str:
        return f"http://127.0.0.1:{self.worker_port}"

    def is_running(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self) -> dict:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"status": "already_running", "pid": self.proc.pid}
            cmd = [
                sys.executable,
                "-m",
                "localgateway.worker",
                "--config",
                self.config_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.worker_port),
            ]
            self.proc = subprocess.Popen(cmd)
            self.started_at = time.time()
            logs.info("Gateway started", provider="supervisor", pid=self.proc.pid)
            # P9: wait (briefly) until the worker actually serves before
            # returning, so start/restart feel atomic — previously start()
            # returned before the worker bound its port and the first ~1s of
            # requests got 503 "worker unreachable".
            self._wait_ready(timeout=15.0)
            return {"status": "started", "pid": self.proc.pid}

    def _wait_ready(self, timeout: float) -> bool:
        """Poll the worker's /v1/models until it answers (or the process dies)."""
        deadline = time.time() + timeout
        url = f"{self.worker_base()}/v1/models"
        with httpx.Client(timeout=1.0) as probe:
            while time.time() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    return False
                try:
                    probe.get(url)
                    return True
                except Exception:
                    time.sleep(0.2)
        return False

    def stop(self) -> dict:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = None
                self.started_at = None
                return {"status": "already_stopped"}
            pid = self.proc.pid
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc = None
            self.started_at = None
            logs.info("Gateway stopped", provider="supervisor", pid=pid)
            return {"status": "stopped", "pid": pid}

    def restart(self) -> dict:
        self.stop()
        time.sleep(0.3)
        return self.start()

    def status(self) -> dict:
        running = self.is_running()
        return {
            "running": running,
            "pid": self.proc.pid if (running and self.proc) else None,
            "port": self.port,
            "worker_port": self.worker_port,
            "uptime": round(time.time() - self.started_at) if (running and self.started_at) else None,
        }


def create_supervisor_app(sup: Supervisor) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        yield
        sup.stop()
        await client.aclose()

    app = FastAPI(title="LocalGateway Supervisor", lifespan=lifespan)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.client = client
    app.state.sup = sup

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.middleware("http")
    async def admin_auth_middleware(request: Request, call_next):
        # Admin routes and UI pages: loopback is always allowed (the local
        # dashboard); everyone else needs the key. API routes are handled by
        # api_auth_middleware.
        path = request.url.path
        if path.startswith("/admin") or is_page_path(path):
            if not check_api_key(request, load_config()):
                return JSONResponse(
                    {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                    status_code=401,
                )
        return await call_next(request)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        # DNS-rebinding protection: mutating /admin requests without a Bearer
        # token must come from a browser page served by this host. Loopback
        # curl/API clients don't send Origin and pass through.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            path = request.url.path
            if path.startswith("/admin"):
                auth = request.headers.get("Authorization", "")
                if not auth:
                    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
                    host = request.headers.get("host", "")
                    if origin:
                        try:
                            from urllib.parse import urlparse
                            o = urlparse(origin)
                            if o.netloc and o.netloc != host:
                                return JSONResponse(
                                    {"error": {"message": "Invalid origin", "type": "forbidden"}},
                                    status_code=403,
                                )
                        except Exception:
                            pass
        return await call_next(request)

    @app.middleware("http")
    async def api_auth_middleware(request: Request, call_next):
        # API routes (/v1, /api/v1) always require the key when one is set.
        # Admin routes and pages are handled by admin_auth_middleware.
        path = request.url.path
        if path.startswith("/admin") or is_page_path(path):
            return await call_next(request)
        if path.startswith("/v1") or path.startswith("/api/v1"):
            cfg = load_config()
            if cfg.server.api_key or cfg.server.api_keys:
                if resolve_api_key(request, cfg) is None:
                    return JSONResponse(
                        {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                        status_code=401,
                    )
        return await call_next(request)

    # Jinja2-templated pages + /static (from endpoints/web.py)
    app.include_router(web_router)

    # --- Live-state admin endpoints: proxy to the worker when running.
    # In-memory registries (stats, circuits, warmth, rate-limits) live in the
    # worker subprocess. The supervisor mounts admin_router (below) so config,
    # logs, and backups remain editable while the gateway is stopped — but
    # that means live-state endpoints served by the supervisor return empty
    # data ({}, null) because the supervisor's own module state is unused.
    # These explicit routes (registered before admin_router so they match
    # first) forward to the worker when running, and fall back to the local
    # admin handler (degraded but correct shape) when stopped.
    def _make_live_proxy(method: str, path: str, fn_name: str,
                         takes_request: bool = False):
        async def _handler(request: Request):
            if sup.is_running():
                try:
                    url = f"{sup.worker_base()}{path}"
                    if request.url.query:
                        url += f"?{request.url.query}"
                    if method == "GET":
                        r = await client.get(url)
                    else:
                        body = await request.body()
                        headers = {
                            k: v for k, v in request.headers.items()
                            if k.lower() not in HOP_BY_HOP
                        }
                        r = await client.request(method, url,
                                                 headers=headers, content=body)
                    resp_headers = {
                        k: v for k, v in r.headers.items()
                        if k.lower() not in RESP_STRIP
                    }
                    return Response(content=r.content, status_code=r.status_code,
                                    headers=resp_headers)
                except Exception:
                    pass  # worker unreachable — fall through to local handler
            from .endpoints import admin as _admin
            fn = getattr(_admin, fn_name)
            if takes_request:
                return await fn(request)
            return await fn()
        _handler.__name__ = f"_live_proxy_{fn_name}"
        return _handler

    for _m, _p, _fn, _tr in [
        ("GET", "/admin/health", "health_endpoint", False),
        ("GET", "/admin/rate-limits", "rate_limits_endpoint", False),
        ("GET", "/admin/inflight", "inflight_endpoint", False),
        ("GET", "/admin/warmth", "get_warmth", False),
        ("DELETE", "/admin/warmth", "clear_warmth", False),
        ("GET", "/admin/circuit", "circuit_snapshot", False),
        ("POST", "/admin/circuit/reset", "circuit_reset", True),
    ]:
        app.add_api_route(_p, _make_live_proxy(_m, _p, _fn, _tr), methods=[_m])

    # All /admin routes come from the worker's canonical admin_router (single
    # source of truth), so fields like `avatar` never drift between ports.
    app.include_router(admin_router)

    import importlib.resources as ir
    import pathlib

    _web_dir = pathlib.Path(str(ir.files("localgateway.web")))
    if _web_dir.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=str(_web_dir)), name="static")

    @app.get("/admin/server/status")
    async def server_status():
        status = sup.status()
        recovered = config_recovered_from()
        status["config_recovered"] = recovered
        try:
            from .usage import get_active_anomalies
            status["anomalies"] = get_active_anomalies()
        except Exception:
            status["anomalies"] = []
        return JSONResponse(status)

    @app.post("/admin/server/start")
    async def server_start():
        return JSONResponse(sup.start())

    @app.post("/admin/server/stop")
    async def server_stop():
        return JSONResponse(sup.stop())

    @app.post("/admin/server/restart")
    async def server_restart():
        return JSONResponse(sup.restart())

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def proxy(path: str, request: Request):
        if not sup.is_running():
            return JSONResponse(
                {
                    "error": {
                        "message": "Gateway is stopped. Start it from the Logs page.",
                        "type": "server_stopped",
                        "code": "server_stopped",
                    }
                },
                status_code=503,
            )

        url = f"{sup.worker_base()}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
        }

        # SSE-producing API paths. Gate on the path so non-streaming admin
        # endpoints aren't misdetected as SSE proxies. /v1/responses streams
        # the same way as /chat/completions (responses.py emits SSE events).
        # /admin/events is the L1 SSE bus (GET, no body) — needs streaming.
        wants_stream = False
        if path == "admin/events":
            wants_stream = True
        elif body and not path.startswith("admin/"):
            if path.endswith("chat/completions") or path.endswith("/responses"):
                try:
                    wants_stream = bool(json.loads(body).get("stream"))
                except Exception:
                    pass

        try:
            if wants_stream:
                async def gen():
                    async with client.stream(
                        request.method, url, headers=headers, content=body
                    ) as r:
                        for k, v in r.headers.items():
                            if k.lower() not in RESP_STRIP:
                                response.headers[k] = v
                        async for chunk in r.aiter_bytes():
                            yield chunk

                response = StreamingResponse(
                    gen(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
                return response

            r = await client.request(request.method, url, headers=headers, content=body)
            resp_headers = {
                k: v for k, v in r.headers.items() if k.lower() not in RESP_STRIP
            }
            return Response(content=r.content, status_code=r.status_code, headers=resp_headers)
        except httpx.ConnectError:
            return JSONResponse(
                {
                    "error": {
                        "message": "Gateway worker is not reachable. Try restarting it.",
                        "type": "worker_unreachable",
                        "code": "worker_unreachable",
                    }
                },
                status_code=503,
            )

    return app


def run_supervisor(config_path: str, host: str, port: int) -> None:
    worker_port = port + 1000
    set_config_path(config_path)
    set_db_path("data/usage.db")
    logs.set_db_path("data/usage.db")
    from . import respcache
    respcache.set_db_path("data/usage.db")
    cfg = load_config()
    if not is_loopback_host(host) and not cfg.server.api_key:
        logs.warn(
            "Binding to a non-loopback host without an API key: the admin UI and API are open to your network.",
            provider="supervisor",
        )
    sup = Supervisor(config_path, host, port, worker_port)
    sup.start()
    app = create_supervisor_app(sup)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        sup.stop()

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
from .auth import check_api_key, is_loopback_host, is_page_path
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
            return {"status": "started", "pid": self.proc.pid}

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
    app = FastAPI(title="LocalGateway Supervisor")
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
            if cfg.server.api_key:
                auth = request.headers.get("Authorization", "")
                token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer") else auth
                if token != cfg.server.api_key:
                    return JSONResponse(
                        {"error": {"message": "Invalid API key", "type": "authentication_error"}},
                        status_code=401,
                    )
        return await call_next(request)

    # Jinja2-templated pages + /static (from endpoints/web.py)
    app.include_router(web_router)
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

    @app.get("/admin/logs")
    async def get_logs(
        limit: int = 200,
        level: str | None = None,
        search: str | None = None,
        since_id: int | None = None,
    ):
        return JSONResponse({
            "logs": logs.get_logs(limit=limit, level=level, search=search, since_id=since_id)
        })

    @app.delete("/admin/logs")
    async def clear_logs():
        logs.clear_logs()
        return JSONResponse({"status": "ok"})

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

        wants_stream = False
        if body and not path.startswith("admin/"):
            try:
                wants_stream = bool(json.loads(body).get("stream"))
            except Exception:
                pass

        try:
            if wants_stream:
                response = StreamingResponse(
                    gen(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

                async def gen():
                    async with client.stream(
                        request.method, url, headers=headers, content=body
                    ) as r:
                        # Mirror the worker's disclosure headers (X-Provider /
                        # X-Backend) so streaming clients on the supervisor see
                        # which provider served the request. Starlette serializes
                        # headers only when iteration begins, so mutating them
                        # here before the first yield is safe.
                        for k, v in r.headers.items():
                            if k.lower() not in RESP_STRIP:
                                response.headers[k] = v
                        async for chunk in r.aiter_bytes():
                            yield chunk

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

    @app.on_event("shutdown")
    async def shutdown():
        sup.stop()
        await client.aclose()

    return app


def run_supervisor(config_path: str, host: str, port: int) -> None:
    worker_port = port + 1000
    set_config_path(config_path)
    set_db_path("data/usage.db")
    logs.set_db_path("data/usage.db")
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

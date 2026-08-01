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
from .config import GatewayConfig, load_config, save_config, set_config_path
from .usage import get_usage_summary, set_db_path
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

    # Jinja2-templated pages + /static (from endpoints/web.py)
    app.include_router(web_router)

    import importlib.resources as ir
    import pathlib

    _web_dir = pathlib.Path(str(ir.files("localgateway.web")))
    if _web_dir.is_dir():
        app.mount("/static", NoCacheStaticFiles(directory=str(_web_dir)), name="static")

    @app.get("/admin/server/status")
    async def server_status():
        return JSONResponse(sup.status())

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

    @app.get("/admin/config")
    async def get_config():
        return JSONResponse(load_config().model_dump())

    @app.put("/admin/config")
    async def put_config(request: Request):
        body = await request.json()
        try:
            cfg = GatewayConfig.model_validate(body)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        save_config(cfg)
        return JSONResponse({"status": "ok"})

    @app.get("/admin/models")
    async def list_models():
        cfg = load_config()
        return JSONResponse({"models": [m.model_dump() for m in cfg.models]})

    @app.get("/admin/models/overview")
    async def models_overview(hours: int = 168):
        from .usage import get_model_aggregates
        cfg = load_config()
        agg = get_model_aggregates(hours=hours)
        models = []
        for m in cfg.models:
            active = [
                b for b in m.backends
                if b.enabled and (p := cfg.provider_by_id(b.provider)) and p.enabled
            ]
            prices = [cfg.pricing_for(b.provider, b.model) for b in active]
            priced = [p for p in prices if p.input or p.output]
            ctxs = [b.context_length for b in active if b.context_length]
            context_min = min(ctxs) if ctxs else None
            context_max = max(ctxs) if ctxs else None
            a = agg.get(m.id, {})
            req = a.get("requests", 0)
            models.append({
                "id": m.id,
                "display_name": m.display_name or None,
                "description": m.description or None,
                "modality": m.modality or None,
                "tags": m.tags or [],
                "context_min": context_min,
                "context_max": context_max,
                "enabled": m.enabled,
                "backend_count": len(active),
                "requests": req,
                "success_rate": round(a.get("successes", 0) / req * 100, 1) if req else None,
                "tokens": a.get("tokens", 0),
                "cost": a.get("cost", 0.0),
                "tps_p50": a.get("tps_p50"),
                "tps_p90": a.get("tps_p90"),
                "tps_p99": a.get("tps_p99"),
                "input_price": min((p.input for p in priced), default=None),
                "output_price": min((p.output for p in priced), default=None),
            })
        return JSONResponse({"hours": hours, "models": models})

    @app.get("/admin/models/compare")
    async def models_compare_supervisor(ids: str = ""):
        from .endpoints.admin import models_compare
        return await models_compare(ids=ids)

    @app.get("/admin/models/{model_id}")
    async def get_model(model_id: str):
        cfg = load_config()
        model = cfg.model_by_id(model_id)
        if model is None:
            return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
        return JSONResponse(model.model_dump())

    @app.put("/admin/models/{model_id}")
    async def update_model(model_id: str, request: Request):
        cfg = load_config()
        model = cfg.model_by_id(model_id)
        if model is None:
            return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
        body = await request.json()
        for field in ["description", "context_length", "enabled", "capabilities", "modality", "max_output_tokens", "tags", "aliases", "default_params", "display_name"]:
            if field in body:
                setattr(model, field, body[field])
        save_config(cfg)
        return JSONResponse({"status": "ok"})

    @app.delete("/admin/models/{model_id}")
    async def delete_model(model_id: str):
        cfg = load_config()
        idx = next((i for i, m in enumerate(cfg.models) if m.id == model_id), None)
        if idx is None:
            return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
        cfg.models.pop(idx)
        save_config(cfg)
        return JSONResponse({"status": "ok"})

    @app.put("/admin/models/{model_id}/backends/tiers")
    async def set_backends_tiers(model_id: str, request: Request):
        cfg = load_config()
        model = cfg.model_by_id(model_id)
        if model is None:
            return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
        body = await request.json()
        tiers = body.get("tiers", [])
        if not isinstance(tiers, list):
            return JSONResponse({"error": "tiers must be a list of lists"}, status_code=422)
        all_indices = []
        for t in tiers:
            if not isinstance(t, list):
                return JSONResponse({"error": "each tier must be a list of indices"}, status_code=422)
            all_indices.extend(t)
        if sorted(all_indices) != list(range(len(model.backends))):
            return JSONResponse({"error": "tiers must contain all backend indices exactly once"}, status_code=422)
        for tier_num, tier_indices in enumerate(tiers, start=1):
            for idx in tier_indices:
                model.backends[idx].priority = tier_num
        save_config(cfg)
        return JSONResponse({"status": "ok", "tiers": tiers})

    @app.put("/admin/models/{model_id}/backends/pricing")
    async def set_backend_pricing(model_id: str, request: Request):
        cfg = load_config()
        model = cfg.model_by_id(model_id)
        if model is None:
            return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
        body = await request.json()
        provider = body.get("provider")
        backend_model = body.get("model")
        if not provider or not backend_model:
            return JSONResponse({"error": "provider and model required"}, status_code=422)
        backend = None
        for b in model.backends:
            if b.provider == provider and b.model == backend_model:
                backend = b
                break
        if backend is None:
            return JSONResponse({"error": "backend not found"}, status_code=404)

        def _num(key):
            v = body.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        pricing = cfg.pricing.get(provider, {})
        key = backend_model
        entry = pricing.get(key, {})
        entry["input"] = _num("input_price")
        entry["output"] = _num("output_price")
        entry["cache_read"] = _num("cache_read_price")
        entry["cache_write"] = _num("cache_write_price")
        pricing[key] = entry
        cfg.pricing[provider] = pricing
        save_config(cfg)
        return JSONResponse({"status": "ok"})

    @app.get("/admin/usage")
    async def usage(hours: int = 24):
        return JSONResponse(get_usage_summary(hours=hours))

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
                async def gen():
                    async with client.stream(
                        request.method, url, headers=headers, content=body
                    ) as r:
                        async for chunk in r.aiter_bytes():
                            yield chunk

                return StreamingResponse(
                    gen(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

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
    sup = Supervisor(config_path, host, port, worker_port)
    sup.start()
    app = create_supervisor_app(sup)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        sup.stop()

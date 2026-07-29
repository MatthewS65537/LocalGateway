from __future__ import annotations

import importlib.resources as ir
import pathlib

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

router = APIRouter()

_web_dir = pathlib.Path(str(ir.files("localgateway.web")))
_templates = Jinja2Templates(directory=str(_web_dir / "templates"))

# ---------- pages ----------
@router.get("/")
async def dashboard(request: Request):
    return _templates.TemplateResponse("dashboard.html", {"request": request, "active_page": "dashboard"})

@router.get("/admin")
async def admin_redirect():
    return RedirectResponse("/", status_code=307)

@router.get("/models")
async def models(request: Request):
    return _templates.TemplateResponse("models.html", {"request": request, "active_page": "models"})

@router.get("/models/{model_id}")
async def model_detail(request: Request, model_id: str):
    return _templates.TemplateResponse(
        "model_detail.html",
        {"request": request, "active_page": "models", "model_id": model_id},
    )

@router.get("/providers")
async def providers(request: Request):
    return _templates.TemplateResponse("providers.html", {"request": request, "active_page": "providers"})

@router.get("/usage")
async def usage(request: Request):
    return _templates.TemplateResponse("usage.html", {"request": request, "active_page": "usage"})

@router.get("/logs")
async def logs(request: Request):
    return _templates.TemplateResponse("logs.html", {"request": request, "active_page": "logs"})

@router.get("/compare/{ids}")
async def compare(request: Request, ids: str):
    return _templates.TemplateResponse(
        "compare.html",
        {"request": request, "active_page": "models", "ids": ids},
    )

@router.get("/settings")
async def settings(request: Request):
    return _templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})


# ---------- static ----------
if _web_dir.is_dir():
    router.mount("/static", StaticFiles(directory=str(_web_dir)), name="static")
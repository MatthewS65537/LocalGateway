from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import config_mtime, load_config

router = APIRouter()


def _model_list_entry(m, config) -> dict:
    active = [
        b for b in m.backends
        if b.enabled and (p := config.provider_by_id(b.provider)) and p.enabled
    ]
    prices = [config.pricing_for(b.provider, b.model) for b in active]
    priced = [p for p in prices if p.input or p.output]
    pricing = None
    if priced:
        pricing = {
            "prompt": str(min(p.input for p in priced)),
            "completion": str(min(p.output for p in priced)),
        }
        cache_reads = [p.cache_read for p in priced if p.cache_read is not None]
        if cache_reads:
            pricing["input_cache_read"] = str(min(cache_reads))

    architecture = None
    if m.modality or m.capabilities:
        architecture = {
            "modality": m.modality or "text",
            "input_modalities": [],
            "output_modalities": ["text"],
        }
        if m.capabilities.get("vision") or m.modality in ("text+vision", "multimodal"):
            architecture["input_modalities"].append("image")
        architecture["input_modalities"].insert(0, "text")
        if m.capabilities.get("audio"):
            architecture["input_modalities"].append("audio")

    return {
        "id": m.id,
        "object": "model",
        "created": int(config_mtime()),
        "owned_by": "localgateway",
        "name": m.display_name or m.id,
        "description": m.description or None,
        "context_length": m.context_length,
        "max_output_tokens": m.max_output_tokens,
        "endpoint": getattr(m, "endpoint", "chat"),
        "modality": m.modality or None,
        "architecture": architecture,
        "capabilities": m.capabilities or None,
        "tags": m.tags or None,
        "aliases": m.aliases or None,
        "pricing": pricing,
        "backend_count": len(active),
    }


@router.get("/v1/models")
@router.get("/api/v1/models")
async def list_models():
    config = load_config()
    models = []
    for m in config.models:
        if not m.enabled:
            continue
        models.append(_model_list_entry(m, config))
    return JSONResponse({"object": "list", "data": models})


@router.get("/v1/models/{model_id}")
@router.get("/api/v1/models/{model_id}")
async def retrieve_model(model_id: str):
    """OpenAI-style model retrieve. Resolves canonical IDs and aliases."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None or not model.enabled:
        return JSONResponse(
            {"error": {
                "message": f"The model '{model_id}' does not exist",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }},
            status_code=404,
        )
    return JSONResponse(_model_list_entry(model, config))

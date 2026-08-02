from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import load_config, save_config, GatewayConfig, reload_config, TimeSlot, TimeRoutingConfig
from ..ratelimit import ratelimit
from ..usage import (
    log_request,
    get_usage_summary,
    get_model_aggregates,
    get_backend_percentiles,
    get_backend_series,
    get_model_totals,
    rename_model,
    rename_provider,
    PERCENTILES,
)
from .. import logs
from .. import stats

router = APIRouter()

# Sentinel written by GET /admin/config in place of real API keys. When a
# full config is round-tripped back via PUT, a provider key equal to this
# sentinel is treated as "unchanged" and the real stored key is preserved,
# so the UI's read-modify-write flow never clobbers keys it never saw.
REDACTED_KEY = "\u2022\u2022\u2022\u2022\u2022\u2022"

MODEL_SETTABLE = {
    "description": str,
    "context_length": (int, type(None)),
    "enabled": bool,
    "capabilities": dict,
    "modality": str,
    "max_output_tokens": (int, type(None)),
    "tags": list,
    "aliases": list,
    "default_params": dict,
    "display_name": str,
    "avatar": str,
}

BACKEND_SETTABLE = {
    "provider": str,
    "model": str,
    "enabled": bool,
    "context_length": (int, type(None)),
    "max_output_tokens": (int, type(None)),
    "cache_supported": (bool, type(None)),
}


def _apply_fields(obj, body, whitelist):
    """Apply a subset of body fields to obj with pydantic-typed validation.

    Rejects unknown fields and type mismatches (422) instead of mutating the
    in-memory model with a malformed value that would corrupt the next save.
    """
    allowed = set(whitelist)
    unknown = set(body.keys()) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    for field, types in whitelist.items():
        if field not in body:
            continue
        value = body[field]
        if not isinstance(types, tuple):
            types = (types,)
        if not isinstance(value, types):
            raise ValueError(
                f"field '{field}' must be {types[0].__name__ if len(types) == 1 else 'one of ' + ', '.join(t.__name__ for t in types)}"
            )
        setattr(obj, field, value)


@router.get("/admin/config")
async def get_config():
    cfg = load_config()
    dump = cfg.model_dump()
    for p in dump.get("providers", []):
        if p.get("api_key"):
            p["api_key"] = REDACTED_KEY
    return JSONResponse(dump)


@router.post("/admin/config/api-key")
async def set_provider_api_key(request: Request):
    """Set or clear a provider API key without exposing it in GET /admin/config."""
    body = await request.json()
    provider_id = (body.get("provider") or "").strip()
    if not provider_id:
        return JSONResponse({"error": "provider is required"}, status_code=422)
    cfg = load_config()
    provider = cfg.provider_by_id(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Provider '{provider_id}' not found"}, status_code=404)
    value = body.get("api_key")
    provider.api_key = value if value is not None else ""
    save_config(cfg)
    return JSONResponse({"status": "ok"})


@router.get("/admin/config/api-key/{provider_id}")
async def reveal_provider_api_key(provider_id: str):
    """Return the real API key for one provider (admin-only)."""
    cfg = load_config()
    provider = cfg.provider_by_id(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Provider '{provider_id}' not found"}, status_code=404)
    return JSONResponse({"provider": provider_id, "api_key": provider.api_key})


@router.put("/admin/config")
async def put_config(request: Request):
    body = await request.json()
    current = load_config()
    # Optimistic concurrency: reject stale writes.
    try:
        incoming_version = body.get("version")
    except AttributeError:
        incoming_version = None
    if incoming_version is not None and isinstance(body, dict) and current.version != incoming_version:
        return JSONResponse(
            {
                "error": "config changed since load (conflict)",
                "type": "version_conflict",
                "current_version": current.version,
            },
            status_code=409,
        )
    try:
        cfg = GatewayConfig.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    # Preserve real API keys that were redacted to the sentinel on GET. The UI
    # round-trips the full config on save; without this it would clobber every
    # provider key it never received.
    for p in cfg.providers:
        if p.api_key == REDACTED_KEY:
            existing = current.provider_by_id(p.id)
            if existing is not None:
                p.api_key = existing.api_key
    cfg.version = current.version  # normalize; save_config bumps
    save_config(cfg)
    return JSONResponse({"status": "ok", "version": cfg.version})


@router.post("/admin/config/reload")
async def reload_config_endpoint():
    cfg = reload_config()
    return JSONResponse({"status": "ok", "config": cfg.model_dump()})


@router.get("/admin/usage")
async def usage_endpoint(hours: int = 24):
    return JSONResponse(get_usage_summary(hours=hours))


@router.delete("/admin/usage")
async def clear_usage():
    """Clear all usage data from the database."""
    import sqlite3
    from pathlib import Path
    db_path = Path("data/usage.db")
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("DELETE FROM usage")
        c.execute("DELETE FROM logs")
        conn.commit()
        conn.close()
    return JSONResponse({"status": "ok", "message": "All usage data cleared"})


@router.get("/admin/models/overview")
async def models_overview(hours: int = 168):
    """Catalog view: per-model aggregates merged with config metadata."""
    config = load_config()
    agg = get_model_aggregates(hours=hours)
    models = []
    for m in config.models:
        active = [
            b for b in m.backends
            if b.enabled and (p := config.provider_by_id(b.provider)) and p.enabled
        ]
        prices = [config.pricing_for(b.provider, b.model) for b in active]
        priced = [p for p in prices if p.input or p.output]
        a = agg.get(m.id, {})
        req = a.get("requests", 0)
        models.append({
            "id": m.id,
            "display_name": m.display_name or None,
            "avatar": m.avatar or None,
            "description": m.description or None,
            "modality": m.modality or None,
            "tags": m.tags or [],
            "context_length": m.context_length,
            "enabled": m.enabled,
            "backend_count": len(active),
            "requests": req,
            "success_rate": round(a.get("successes", 0) / req * 100, 1) if req else None,
            "tokens": a.get("tokens", 0),
            "cost": a.get("cost", 0.0),
            "tps_p50": a.get("tps_p50"),
            "input_price": min((p.input for p in priced), default=None),
            "output_price": min((p.output for p in priced), default=None),
        })
    return JSONResponse({"hours": hours, "models": models})


@router.get("/admin/models/{model_id}/stats")
async def model_stats(model_id: str, hours: int = 168, p: str = "p50"):
    """Per-backend percentile stats for one model (OpenRouter providers table)."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    pct = PERCENTILES.get(p, 0.5)
    statsp = get_backend_percentiles(model_id, hours=hours, p=pct)
    totals = get_model_totals(model_id, hours=hours)
    backends = []
    for b in sorted(model.backends, key=lambda x: x.priority):
        provider = config.provider_by_id(b.provider)
        pricing = config.pricing_for(b.provider, b.model)
        key = f"{b.provider}:{b.model}"
        s = statsp.get(key, {})
        backends.append({
            "provider": b.provider,
            "provider_name": (provider.name or provider.id) if provider else b.provider,
            "provider_avatar": (provider.avatar if provider else "") or "",
            "backend_model": b.model,
            "priority": b.priority,
            "enabled": b.enabled and (provider.enabled if provider else False),
            "cache_supported": b.cache_supported,
            "context_length": b.context_length,
            "max_output_tokens": b.max_output_tokens,
            "input_price": pricing.input,
            "output_price": pricing.output,
            "cache_read_price": pricing.cache_read,
            "cache_write_price": pricing.cache_write,
            "cooldown_remaining": round(ratelimit.remaining(b.provider, b.model), 1),
            "requests": s.get("requests", 0),
            "success_rate": s.get("success_rate"),
            "ttft_ms": s.get("ttft_ms"),
            "tps": s.get("tps"),
            "tps_p50": s.get("tps_p50"),
            "tps_p90": s.get("tps_p90"),
            "tps_p99": s.get("tps_p99"),
            "latency_ms": s.get("latency_ms"),
        })
    return JSONResponse({
        "model": model_id,
        "hours": hours,
        "percentile": p,
        "backends": backends,
        "totals": totals,
    })


@router.get("/admin/models/{model_id}/series")
async def model_series(model_id: str, hours: int = 168):
    """Time-series TPS/TTFT per backend for charts."""
    config = load_config()
    if config.model_by_id(model_id) is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    return JSONResponse(get_backend_series(model_id, hours=hours))


@router.get("/admin/models/compare")
async def models_compare(ids: str = ""):
    """Compare 2-3 models side by side: metadata + backends + stats."""
    config = load_config()
    id_list = [x.strip() for x in ids.split(",") if x.strip()]
    if not id_list:
        return JSONResponse({"error": "ids query param required (comma-separated)"}, status_code=422)
    if len(id_list) > 3:
        return JSONResponse({"error": "Max 3 models for comparison"}, status_code=422)
    agg = get_model_aggregates(hours=168)
    results = []
    for mid in id_list:
        m = config.model_by_id(mid)
        if m is None:
            return JSONResponse({"error": f"Model '{mid}' not found"}, status_code=404)
        a = agg.get(m.id, {})
        active = [b for b in m.backends if b.enabled and (p := config.provider_by_id(b.provider)) and p.enabled]
        prices = [config.pricing_for(b.provider, b.model) for b in active]
        priced = [p for p in prices if p.input or p.output]
        results.append({
            "id": m.id,
            "display_name": m.display_name or m.id,
            "description": m.description or None,
            "context_length": m.context_length,
            "max_output_tokens": m.max_output_tokens,
            "modality": m.modality or None,
            "capabilities": m.capabilities,
            "tags": m.tags,
            "aliases": m.aliases,
            "default_params": m.default_params,
            "enabled": m.enabled,
            "backend_count": len(active),
            "requests": a.get("requests", 0),
            "success_rate": round(a.get("successes", 0) / a["requests"] * 100, 1) if a.get("requests") else None,
            "tokens": a.get("tokens", 0),
            "cost": a.get("cost", 0.0),
            "tps_p50": a.get("tps_p50"),
            "input_price": min((p.input for p in priced), default=None),
            "output_price": min((p.output for p in priced), default=None),
            "backends": [
                {
                    "provider": b.provider,
                    "model": b.model,
                    "priority": b.priority,
                    "enabled": b.enabled,
                    "context_length": b.context_length,
                    "max_output_tokens": b.max_output_tokens,
                    "input_price": config.pricing_for(b.provider, b.model).input,
                    "output_price": config.pricing_for(b.provider, b.model).output,
                }
                for b in m.backends
            ],
        })
    return JSONResponse({"models": results})


@router.get("/admin/catalog")
async def catalog_search(
    q: str = "",
    modality: str = "",
    provider: str = "",
    capability: str = "",
    min_ctx: int | None = None,
    max_in_price: float | None = None,
):
    """Search/filter the model catalog."""
    config = load_config()
    agg = get_model_aggregates(hours=168)
    results = []
    for m in config.models:
        active = [b for b in m.backends if b.enabled and (p := config.provider_by_id(b.provider)) and p.enabled]
        prices = [config.pricing_for(b.provider, b.model) for b in active]
        priced = [p for p in prices if p.input or p.output]
        a = agg.get(m.id, {})
        min_input = min((p.input for p in priced), default=None)

        if q:
            ql = q.lower()
            alias_hit = any(ql in a.lower() for a in m.aliases)
            if (
                ql not in m.id.lower()
                and ql not in (m.display_name or "").lower()
                and ql not in m.description.lower()
                and not alias_hit
            ):
                continue
        if modality and m.modality != modality:
            continue
        if provider:
            if not any(b.provider == provider for b in m.backends):
                continue
        if capability and not m.capabilities.get(capability, False):
            continue
        if min_ctx and (m.context_length or 0) < min_ctx:
            continue
        if max_in_price is not None and (min_input is None or min_input > max_in_price):
            continue

        results.append({
            "id": m.id,
            "display_name": m.display_name or m.id,
            "description": m.description or None,
            "context_length": m.context_length,
            "max_output_tokens": m.max_output_tokens,
            "modality": m.modality or None,
            "capabilities": m.capabilities,
            "tags": m.tags,
            "aliases": m.aliases,
            "enabled": m.enabled,
            "backend_count": len(active),
            "requests": a.get("requests", 0),
            "success_rate": round(a.get("successes", 0) / a["requests"] * 100, 1) if a.get("requests") else None,
            "tokens": a.get("tokens", 0),
            "cost": a.get("cost", 0.0),
            "tps_p50": a.get("tps_p50"),
            "input_price": min_input,
            "output_price": min((p.output for p in priced), default=None),
        })
    return JSONResponse({"models": results, "count": len(results)})


@router.get("/admin/models/{model_id}")
async def get_model(model_id: str):
    """Full model detail: metadata + backends with stats + pricing."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    a = get_model_aggregates(hours=168).get(model_id, {})
    backends = []
    for b in model.backends:
        provider = config.provider_by_id(b.provider)
        pricing = config.pricing_for(b.provider, b.model)
        backends.append({
            "provider": b.provider,
            "provider_name": (provider.name or provider.id) if provider else b.provider,
            "model": b.model,
            "priority": b.priority,
            "enabled": b.enabled and (provider.enabled if provider else False),
            "cache_supported": b.cache_supported,
            "context_length": b.context_length,
            "max_output_tokens": b.max_output_tokens,
            "input_price": pricing.input,
            "output_price": pricing.output,
        })
    return JSONResponse({
        "id": model.id,
        "display_name": model.display_name or model.id,
        "description": model.description or None,
        "context_length": model.context_length,
        "max_output_tokens": model.max_output_tokens,
        "modality": model.modality or None,
        "capabilities": model.capabilities,
        "tags": model.tags,
        "aliases": model.aliases,
        "default_params": model.default_params,
        "enabled": model.enabled,
        "requests": a.get("requests", 0),
        "success_rate": round(a.get("successes", 0) / a["requests"] * 100, 1) if a.get("requests") else None,
        "tokens": a.get("tokens", 0),
        "cost": a.get("cost", 0.0),
        "tps_p50": a.get("tps_p50"),
        "backends": backends,
    })


@router.post("/admin/models")
async def create_model(request: Request):
    """Add a new model."""
    body = await request.json()
    model_id = body.get("id", "").strip()
    if not model_id:
        return JSONResponse({"error": "id is required"}, status_code=422)
    config = load_config()
    if config.model_by_id(model_id):
        return JSONResponse({"error": f"Model '{model_id}' already exists"}, status_code=409)
    from ..config import ModelConfig
    try:
        new_model = ModelConfig.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    config.models.append(new_model)
    save_config(config)
    return JSONResponse({"status": "ok", "model": new_model.model_dump()})


@router.put("/admin/models/{model_id}")
async def update_model(model_id: str, request: Request):
    """Update model metadata."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    body = await request.json()
    try:
        # time_routing must be validated as a pydantic model (not set raw).
        # Pop it so _apply_fields doesn't setattr the raw dict afterward.
        tr_body = body.pop("time_routing", None)
        if tr_body is not None:
            model.time_routing = TimeRoutingConfig.model_validate(tr_body)
        _apply_fields(model, body, MODEL_SETTABLE)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.delete("/admin/models/{model_id}")
async def delete_model(model_id: str):
    """Remove a model."""
    config = load_config()
    idx = next((i for i, m in enumerate(config.models) if m.id == model_id), None)
    if idx is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    config.models.pop(idx)
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.put("/admin/models/{model_id}/backends/reorder")
async def reorder_backends(model_id: str, request: Request):
    """Reorder backends by setting priority order."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    body = await request.json()
    order = body.get("order", [])
    if not isinstance(order, list):
        return JSONResponse({"error": "order must be a list of backend indices"}, status_code=422)
    if sorted(order) != list(range(len(model.backends))):
        return JSONResponse({"error": "order must contain all backend indices exactly once"}, status_code=422)
    new_backends = [model.backends[i] for i in order]
    for i, b in enumerate(new_backends):
        b.priority = i + 1
    model.backends = new_backends
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.put("/admin/models/{model_id}/backends/tiers")
async def set_backends_tiers(model_id: str, request: Request):
    """Set backend tier grouping. Body: {tiers: [[idx,...], ...]}"""
    config = load_config()
    model = config.model_by_id(model_id)
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
    save_config(config)
    return JSONResponse({"status": "ok", "tiers": tiers})


@router.post("/admin/models/{model_id}/backends")
async def add_backend(model_id: str, request: Request):
    """Add a backend to a model."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    body = await request.json()
    from ..config import BackendConfig
    try:
        new_backend = BackendConfig.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    new_backend.priority = max((b.priority for b in model.backends), default=0) + 1
    model.backends.append(new_backend)
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.put("/admin/models/{model_id}/backends/{index}")
async def update_backend(model_id: str, index: int, request: Request):
    """Update a backend."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    if index < 0 or index >= len(model.backends):
        return JSONResponse({"error": "Invalid backend index"}, status_code=400)
    body = await request.json()
    backend = model.backends[index]
    try:
        _apply_fields(backend, body, BACKEND_SETTABLE)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.delete("/admin/models/{model_id}/backends/{index}")
async def remove_backend(model_id: str, index: int):
    """Remove a backend from a model."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    if index < 0 or index >= len(model.backends):
        return JSONResponse({"error": "Invalid backend index"}, status_code=400)
    model.backends.pop(index)
    for i, b in enumerate(model.backends):
        b.priority = i + 1
    save_config(config)
    return JSONResponse({"status": "ok"})


@router.get("/admin/models/{model_id}/time-routing")
async def get_time_routing(model_id: str):
    """Return a model's time-based routing config."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    return JSONResponse(model.time_routing.model_dump())


@router.put("/admin/models/{model_id}/time-routing")
async def put_time_routing(model_id: str, request: Request):
    """Replace a model's entire time-based routing config."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    body = await request.json()
    try:
        model.time_routing = TimeRoutingConfig.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    save_config(config)
    return JSONResponse(model.time_routing.model_dump())


@router.post("/admin/models/{model_id}/time-slots")
async def create_time_slot(model_id: str, request: Request):
    """Create a time slot for a model. Generates ``id`` when blank."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    body = await request.json()
    try:
        slot = TimeSlot.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if not slot.id:
        slot.id = _slot_id(slot)
    model.time_routing.slots.append(slot)
    save_config(config)
    return JSONResponse(slot.model_dump())


@router.put("/admin/models/{model_id}/time-slots/{slot_id}")
async def update_time_slot(model_id: str, slot_id: str, request: Request):
    """Update a single time slot by id."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    slot = next((s for s in model.time_routing.slots if s.id == slot_id), None)
    if slot is None:
        return JSONResponse({"error": f"Time slot '{slot_id}' not found"}, status_code=404)
    body = await request.json()
    try:
        updated = TimeSlot.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if not updated.id:
        updated.id = slot_id
    idx = model.time_routing.slots.index(slot)
    model.time_routing.slots[idx] = updated
    save_config(config)
    return JSONResponse(updated.model_dump())


@router.delete("/admin/models/{model_id}/time-slots/{slot_id}")
async def delete_time_slot(model_id: str, slot_id: str):
    """Delete a time slot by id."""
    config = load_config()
    model = config.model_by_id(model_id)
    if model is None:
        return JSONResponse({"error": f"Model '{model_id}' not found"}, status_code=404)
    before = len(model.time_routing.slots)
    model.time_routing.slots = [s for s in model.time_routing.slots if s.id != slot_id]
    if len(model.time_routing.slots) == before:
        return JSONResponse({"error": f"Time slot '{slot_id}' not found"}, status_code=404)
    save_config(config)
    return JSONResponse({"status": "ok"})


def _slot_id(slot: TimeSlot) -> str:
    """Deterministic slug from name + hours, e.g. 'peak-09-17'."""
    import re
    stem = re.sub(r"[^a-z0-9]+", "-", (slot.name or "").lower()).strip("-")
    if not stem:
        stem = "slot"
    return f"{stem}-{slot.start_hour}-{slot.end_hour}"


@router.get("/admin/health")
async def health_endpoint():
    config = load_config()
    providers = []
    for p in config.providers:
        providers.append({
            "id": p.id,
            "name": p.name or p.id,
            "base_url": p.base_url,
            "enabled": p.enabled,
        })
    backends = []
    for m in config.models:
        for b in m.backends:
            provider = config.provider_by_id(b.provider)
            backends.append({
                "model": m.id,
                "provider": b.provider,
                "provider_name": (provider.name or provider.id) if provider else b.provider,
                "backend_model": b.model,
                "priority": b.priority,
                "enabled": b.enabled,
                "provider_enabled": provider.enabled if provider else False,
                "cooldown_remaining": round(ratelimit.remaining(b.provider, b.model), 1),
            })
    return JSONResponse({
        "providers": providers,
        "backends": backends,
        "rate_limits": ratelimit.snapshot(),
        "stats": stats.snapshot(),
    })


@router.get("/admin/rate-limits")
async def rate_limits_endpoint():
    return JSONResponse(ratelimit.snapshot())


@router.get("/admin/inflight")
async def inflight_endpoint():
    snap = stats.snapshot()
    return JSONResponse({k: v.get("in_flight", 0) for k, v in snap.items()})


@router.get("/admin/warmth")
async def get_warmth():
    """Cache-affinity warmth registry: which backends are warm for which
    request fingerprints, with hit/miss/write counts and last-seen time."""
    return JSONResponse(stats.warmth_snapshot())


@router.delete("/admin/warmth")
async def clear_warmth():
    """Reset the warmth registry (all fingerprints become cold)."""
    stats.clear_warmth()
    return JSONResponse({"status": "ok"})


@router.post("/admin/backends/snooze")
async def snooze_backend(request: Request):
    body = await request.json()
    provider = body.get("provider")
    model = body.get("model")
    seconds = body.get("seconds")
    permanent = body.get("permanent", False)
    if not provider or not model:
        return JSONResponse({"error": "provider and model are required"}, status_code=422)
    if permanent:
        ratelimit.snooze_permanent(provider, model)
        ratelimit.save()
        return JSONResponse({"status": "ok", "remaining": -1})
    ratelimit.snooze(provider, model, float(seconds or 3600))
    ratelimit.save()
    return JSONResponse({"status": "ok", "remaining": ratelimit.remaining(provider, model)})


@router.post("/admin/backends/unsnooze")
async def unsnooze_backend(request: Request):
    body = await request.json()
    provider = body.get("provider")
    model = body.get("model")
    if not provider or not model:
        return JSONResponse({"error": "provider and model are required"}, status_code=422)
    ratelimit.unsnooze(provider, model)
    ratelimit.save()
    return JSONResponse({"status": "ok"})


@router.post("/admin/backends/unsnooze-all")
async def unsnooze_all_backends():
    """Remove every snooze/cooldown entry (manual + auto)."""
    cleared = ratelimit.clear()
    return JSONResponse({"status": "ok", "cleared": cleared})


@router.post("/admin/rename")
async def rename_entity(request: Request):
    """Rename a model or provider, cascading to all related data.

    Body: { "type": "model" | "provider", "old_id": str, "new_id": str }
    Cascades to: config, usage DB, logs, probes, snooze state, pricing keys.
    """
    body = await request.json()
    rename_type = body.get("type")
    old_id = (body.get("old_id") or "").strip()
    new_id = (body.get("new_id") or "").strip()

    if rename_type not in ("model", "provider"):
        return JSONResponse({"error": "type must be 'model' or 'provider'"}, status_code=422)
    if not old_id or not new_id:
        return JSONResponse({"error": "old_id and new_id are required"}, status_code=422)
    if old_id == new_id:
        return JSONResponse({"error": "old_id and new_id are the same"}, status_code=422)

    config = load_config()

    if rename_type == "model":
        model = config.model_by_id(old_id)
        if model is None:
            return JSONResponse({"error": f"Model '{old_id}' not found"}, status_code=404)
        if config.model_by_id(new_id) is not None:
            return JSONResponse({"error": f"Model '{new_id}' already exists"}, status_code=409)

        model.id = new_id
        save_config(config)
        rows = rename_model(old_id, new_id)
        return JSONResponse({
            "status": "ok",
            "type": "model",
            "old_id": old_id,
            "new_id": new_id,
            "usage_rows_updated": rows,
        })

    # Provider rename
    provider = config.provider_by_id(old_id)
    if provider is None:
        return JSONResponse({"error": f"Provider '{old_id}' not found"}, status_code=404)
    if config.provider_by_id(new_id) is not None:
        return JSONResponse({"error": f"Provider '{new_id}' already exists"}, status_code=409)

    provider.id = new_id

    # Cascade backend.provider references
    backend_refs = 0
    for m in config.models:
        for b in m.backends:
            if b.provider == old_id:
                b.provider = new_id
                backend_refs += 1

    # Cascade pricing keys (provider:backend_model)
    pricing = dict(config.pricing) if config.pricing else {}
    prefix = old_id + ":"
    moved_pricing = 0
    for k in list(pricing.keys()):
        if k.startswith(prefix):
            pricing[new_id + ":" + k[len(prefix):]] = pricing.pop(k)
            moved_pricing += 1
    config.pricing = pricing

    save_config(config)

    # Cascade usage DB + logs + probes
    rows = rename_provider(old_id, new_id)

    # Cascade snooze state (in-memory + file)
    snooze_moved = ratelimit.rename_provider(old_id, new_id)

    return JSONResponse({
        "status": "ok",
        "type": "provider",
        "old_id": old_id,
        "new_id": new_id,
        "backend_refs_updated": backend_refs,
        "pricing_keys_moved": moved_pricing,
        "usage_rows_updated": rows,
        "snooze_keys_moved": snooze_moved,
    })


@router.get("/admin/providers/{provider_id}/models")
async def discover_provider_models(provider_id: str):
    """Fetch the model catalog from an upstream provider (GET /models)."""
    config = load_config()
    provider = config.provider_by_id(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Provider '{provider_id}' not found"}, status_code=404)

    url = f"{provider.base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {provider.api_key}"}
    headers.update(provider.headers)

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            return JSONResponse(
                {"error": resp.text[:500] or f"HTTP {resp.status_code}", "status_code": resp.status_code},
                status_code=502,
            )
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        models = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    models.append({
                        "id": item["id"],
                        "context_length": item.get("context_length"),
                        "owned_by": item.get("owned_by"),
                    })
                elif isinstance(item, str):
                    models.append({"id": item})
        models.sort(key=lambda m: m["id"])
        return JSONResponse({"provider": provider_id, "models": models, "latency_ms": latency_ms})
    except Exception as e:
        return JSONResponse({"error": str(e)[:500]}, status_code=502)


@router.post("/admin/backends/test")
async def test_backend(request: Request):
    """Send a minimal probe request to a backend and report the result."""
    from ..sse import StreamAccumulator
    from ..streaming import compute_tps
    from ..usage import get_backend_percentiles

    body = await request.json()
    provider_id = body.get("provider", "")
    model = body.get("model", "")
    stream = bool(body.get("stream", False))
    logical_model = body.get("logical_model", "")
    if not provider_id or not model:
        return JSONResponse({"error": "provider and model are required"}, status_code=422)

    config = load_config()
    provider = config.provider_by_id(provider_id)
    if provider is None:
        return JSONResponse({"error": f"Provider '{provider_id}' not found"}, status_code=404)

    if not ratelimit.is_available(provider_id, model):
        return JSONResponse({"ok": False, "skipped": True, "error": "backend is snoozed"}, status_code=200)

    max_tokens = config.server.probe_max_tokens or 64
    probe_body = {
        "model": model,
        "messages": [{"role": "user", "content": "Write a detailed paragraph explaining how a rainbow forms. Be thorough and specific."}],
        "max_tokens": max_tokens,
        "stream": stream,
        "stream_options": {"include_usage": True},
    }
    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider.headers)

    async def single_probe():
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if stream:
                    acc = StreamAccumulator(start=t0)
                    async with client.stream("POST", url, headers=headers, json=probe_body) as resp:
                        if resp.status_code != 200:
                            text = b""
                            async for c in resp.aiter_bytes():
                                text += c
                            latency_ms = int((time.monotonic() - t0) * 1000)
                            return {"ok": False, "status_code": resp.status_code, "error": text.decode("utf-8", errors="replace")[:500], "latency_ms": latency_ms}
                        async for c in resp.aiter_bytes():
                            if c:
                                acc.feed(c)
                        for ev in acc.finish():
                            pass
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    out_tok = acc.usage.output_tokens or acc.estimated_output_tokens()
                    in_tok = acc.usage.input_tokens
                    ttft_ms = acc.ttft_ms or acc._first_byte_ms
                    tps = compute_tps(out_tok, latency_ms, ttft_ms)
                    return {"ok": True, "latency_ms": latency_ms, "ttft_ms": ttft_ms, "tps": tps, "output_tokens": out_tok, "input_tokens": in_tok, "stream": True}
                else:
                    resp = await client.post(url, headers=headers, json=probe_body)
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    if resp.status_code != 200:
                        return {"ok": False, "status_code": resp.status_code, "error": resp.text[:500], "latency_ms": latency_ms}
                    from ..provider import _extract_usage
                    u = _extract_usage(resp.content)
                    out_tok = u.output_tokens
                    in_tok = u.input_tokens
                    ttft_ms = None
                    tps = compute_tps(out_tok, latency_ms, ttft_ms)
                    return {"ok": True, "latency_ms": latency_ms, "ttft_ms": ttft_ms, "tps": tps, "output_tokens": out_tok, "input_tokens": in_tok}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500], "latency_ms": int((time.monotonic() - t0) * 1000)}

    MAX_REPROBE = 3
    IMPLAUSIBLE_TPS = 2000
    result = None
    attempts = 0
    implausible_tps_values = []

    for attempt in range(MAX_REPROBE):
        result = await single_probe()
        if not result.get("ok"):
            break
        tps = result.get("tps")
        if tps is None or tps < IMPLAUSIBLE_TPS:
            break
        implausible_tps_values.append(tps)
        if attempt < MAX_REPROBE - 1:
            await asyncio.sleep(0.5)

    if result and result.get("ok") and result.get("tps", 0) >= IMPLAUSIBLE_TPS:
        key = f"{provider_id}:{model}"
        hist = get_backend_percentiles(logical_model, hours=168, p=0.5) if logical_model else {}
        p50_tps = hist.get(key, {}).get("tps_p50")
        if p50_tps is not None:
            result["tps"] = p50_tps
            result["tps_source"] = "p50_historical"
        implausible_tps_values = []

    if result and result.get("ok") and logical_model:
        latency_ms = result.get("latency_ms")
        ttft_ms = result.get("ttft_ms")
        tps = result.get("tps")
        out_tok = result.get("output_tokens")
        in_tok = result.get("input_tokens")
        pricing = config.pricing_for(provider_id, model)
        cost = None
        if pricing and out_tok:
            cost = (in_tok or 0) * pricing.input + out_tok * pricing.output
        log_request(
            logical_model=logical_model, provider=provider_id,
            backend_model=model, success=True,
            input_tokens=in_tok, output_tokens=out_tok,
            cost=cost, latency_ms=latency_ms, ttft_ms=ttft_ms,
            tps=tps, stream=stream, is_probe=True,
        )
        stats.record_success(provider_id, model, latency_ms=latency_ms, ttft_ms=ttft_ms)

    return JSONResponse(result if result else {"ok": False, "error": "Probe failed"})


@router.get("/admin/logs")
async def logs_endpoint(
    limit: int = 200,
    level: str | None = None,
    search: str | None = None,
    since_id: int | None = None,
):
    return JSONResponse({
        "logs": logs.get_logs(limit=limit, level=level, search=search, since_id=since_id)
    })


@router.delete("/admin/logs")
async def clear_logs_endpoint():
    logs.clear_logs()
    return JSONResponse({"status": "ok"})

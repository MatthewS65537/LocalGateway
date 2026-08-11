from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import load_config, save_config, GatewayConfig, reload_config, TimeSlot, TimeRoutingConfig, validated_api_key, validate_config
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

# P2: short-lived cache for models_overview's get_model_aggregates call.
_overview_cache: dict[tuple, tuple[float, dict]] = {}

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
    "probe_interval_s": (float, type(None)),
    # C1: shipped fields that the granular edit APIs couldn't touch (422
    # "unknown fields") — only a raw config PUT worked.
    "cache_responses": bool,
    "endpoint": str,
}

BACKEND_SETTABLE = {
    "provider": str,
    "model": str,
    "enabled": bool,
    "context_length": (int, type(None)),
    "max_output_tokens": (int, type(None)),
    "cache_supported": (bool, type(None)),
    # C1: F8 static weights were unreachable via the per-backend edit modal.
    "weight": float,
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
    server = dump.get("server") or {}
    for k in server.get("api_keys", []):
        if k.get("key"):
            k["key"] = REDACTED_KEY
    if server.get("api_key"):
        server["api_key"] = REDACTED_KEY
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
    if value == REDACTED_KEY:
        return JSONResponse(
            {"error": "refusing to save the redacted placeholder as an API key — enter the real key"},
            status_code=422,
        )
    provider.api_key = value if value is not None else ""
    save_config(cfg)
    return JSONResponse({"status": "ok"})


@router.post("/admin/config/api-key/{provider_id}/reveal")
async def reveal_provider_api_key(provider_id: str):
    """Return the real API key for one provider (admin-only).

    POST-only so plaintext keys never appear in browser history or access logs.
    """
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
            else:
                return JSONResponse(
                    {"error": f"refusing to save the redacted placeholder as API key for provider '{p.id}' — enter the real key"},
                    status_code=422,
                )
    # Same preservation for the legacy gateway key and client API keys.
    if cfg.server.api_key == REDACTED_KEY:
        cfg.server.api_key = current.server.api_key
    for k in cfg.server.api_keys:
        if k.key == REDACTED_KEY:
            existing = current.api_key_by_id(k.id)
            if existing is not None:
                k.key = existing.key
            else:
                return JSONResponse(
                    {"error": f"refusing to save the redacted placeholder as key for API key entry '{k.id}' — enter the real key"},
                    status_code=422,
                )
    cfg.version = current.version  # normalize; save_config bumps
    save_config(cfg)
    _overview_cache.clear()
    return JSONResponse({"status": "ok", "version": cfg.version})


@router.post("/admin/config/reload")
async def reload_config_endpoint():
    cfg = reload_config()
    return JSONResponse({"status": "ok", "config": cfg.model_dump()})


@router.post("/admin/config/validate")
async def validate_config_endpoint(request: Request):
    """N1: dry-run preflight validation of a candidate config. No write."""
    body = await request.json()
    try:
        cfg = GatewayConfig.model_validate(body)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    issues = validate_config(cfg)
    return JSONResponse({"issues": issues})


@router.get("/admin/usage")
async def usage_endpoint(hours: int = 24):
    return JSONResponse(get_usage_summary(hours=hours))


@router.get("/admin/usage/series")
async def usage_series(hours: int = 168):
    """Daily/hourly cost + tokens per provider, for the usage-page chart."""
    from ..usage import get_daily_series
    hours = max(1, min(hours, 24 * 365))
    return JSONResponse(get_daily_series(hours=hours))


@router.delete("/admin/usage")
async def clear_usage():
    """Clear all usage data from the database.

    B11: previously hardcoded Path("data/usage.db") (ignoring the configured
    path), left the response cache populated (cached replies kept replaying
    with zero usage rows to show for them), and never invalidated the TPS map
    or overview caches, so stats-driven routing and the models page showed
    ghost data for up to 30-60s. Route through the modules' own clearers and
    invalidate the caches.
    """
    from .. import usage as _usage
    from .. import logs as _logs
    from .. import respcache as _respcache
    cleared = {
        "usage": _usage.clear_usage(),
        "logs": _logs.clear_logs(),
        "cache": _respcache.clear(),
    }
    from ..usage import invalidate_tps_map
    invalidate_tps_map()
    _overview_cache.clear()
    return JSONResponse({"status": "ok", "message": "All usage data cleared", "cleared": cleared})


@router.get("/admin/models/overview")
async def models_overview(hours: int = 168):
    """Catalog view: per-model aggregates merged with config metadata."""
    config = load_config()
    # P2: cache the expensive get_model_aggregates call (2 full SQL aggregations
    # + a per-model TPS percentile pass over 7 days). 60s TTL keeps the models
    # page responsive without serving stale data for long.
    import time as _time
    now = _time.time()
    cache_key = ("overview", hours)
    cached = _overview_cache.get(cache_key)
    if cached and now - cached[0] < 60.0:
        agg = cached[1]
    else:
        agg = get_model_aggregates(hours=hours)
        _overview_cache[cache_key] = (now, agg)
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
        # U3: the Models page reads context_min/context_max for its Context
        # stat, filter, sort and table column, but the payload only carried the
        # rarely-set model-level context_length — the whole column showed "—"
        # and the Min-context filter wiped the catalog. Compute the active
        # backend range here (mirroring how backend_count is derived).
        ctx_vals = [b.context_length for b in active if b.context_length]
        models.append({
            "id": m.id,
            "display_name": m.display_name or None,
            "avatar": m.avatar or None,
            "description": m.description or None,
            "modality": m.modality or None,
            "tags": m.tags or [],
            "context_length": m.context_length,
            "context_min": min(ctx_vals) if ctx_vals else (m.context_length or None),
            "context_max": max(ctx_vals) if ctx_vals else (m.context_length or None),
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
    from .. import circuitbreaker
    circuitbreaker.configure_from_config(config)
    circuits = circuitbreaker.snapshot()
    ttl = getattr(config.server, "cache_affinity_ttl_sec", 300) or 300
    candidate_keys = [f"{b.provider}:{b.model}" for b in model.backends]
    warmth = stats.warmth_for_backends(candidate_keys, ttl)
    backends = []
    for b in sorted(model.backends, key=lambda x: x.priority):
        provider = config.provider_by_id(b.provider)
        pricing = config.pricing_for(b.provider, b.model)
        key = f"{b.provider}:{b.model}"
        s = statsp.get(key, {})
        circ = circuits.get(key, {})
        warm = warmth.get(key)
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
            "circuit_open": circ.get("open", False),
            "circuit_failures": circ.get("consecutive_failures", 0),
            "circuit_open_remaining_s": circ.get("open_remaining_s", 0.0),
            "warm": bool(warm),
            "warm_hit_rate": round(warm["last_hit_rate"], 3) if warm else None,
            "warm_fingerprints": warm["fingerprints"] if warm else 0,
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
    from .. import circuitbreaker
    circuitbreaker.configure_from_config(config)
    return JSONResponse({
        "providers": providers,
        "backends": backends,
        "rate_limits": ratelimit.snapshot(),
        "stats": stats.snapshot(),
        "circuit": {
            "enabled": bool(getattr(config.server, "circuit_breaker_enabled", False)),
            "circuits": circuitbreaker.snapshot(),
        },
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


# ---------- circuit breaker ----------

@router.get("/admin/circuit")
async def circuit_snapshot():
    """Circuit breaker state per backend (consecutive failures, open/remaining)."""
    from .. import circuitbreaker
    circuitbreaker.configure_from_config(load_config())
    return JSONResponse({
        "enabled": bool(getattr(load_config().server, "circuit_breaker_enabled", False)),
        "circuits": circuitbreaker.snapshot(),
    })


@router.post("/admin/alerts/test")
async def alert_test():
    """Send a test alert to the configured webhook (validates URL + delivery)."""
    from .. import alerts
    cfg = load_config()
    if not cfg.server.alert_webhook_url:
        return JSONResponse({"ok": False, "error": "No alert webhook URL configured"}, status_code=422)
    delivered = alerts.send("test", "LocalGateway test alert", {"test": True})
    if not delivered:
        return JSONResponse({"ok": False, "error": "No alert webhook URL configured"}, status_code=422)
    return JSONResponse({"ok": True})


@router.post("/admin/anomalies/ack")
async def ack_anomaly_endpoint(request: Request):
    """N2: dismiss a cost anomaly banner. Body: {id: int}."""
    from .. import usage as _usage
    body = await request.json()
    aid = body.get("id")
    if aid is None:
        return JSONResponse({"error": "id required"}, status_code=422)
    ok = _usage.ack_anomaly(int(aid))
    return JSONResponse({"ok": ok})


@router.post("/admin/circuit/reset")
async def circuit_reset(request: Request):
    """Manually close circuits. Body: {} (all) or {provider, model} (one)."""
    from .. import circuitbreaker
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    cleared = circuitbreaker.reset(body.get("provider"), body.get("model"))
    return JSONResponse({"status": "ok", "cleared": cleared})


# ---------- token counting ----------

@router.post("/admin/tokens")
async def count_tokens(request: Request):
    """Estimate token counts for a request body (messages or embeddings input).

    Body: {model?, messages?} or {model?, input?}. Uses tiktoken when
    installed (server.tokenizer=auto), else the chars/4 heuristic.
    """
    from .. import tokenizer
    body = await request.json()
    config = load_config()
    mode = getattr(config.server, "tokenizer", "auto") or "auto"
    result: dict = {"tokenizer": tokenizer.tokenizer_name(mode)}

    messages = body.get("messages")
    if messages is not None:
        n = tokenizer.count_messages(messages, mode=mode)
        result["messages_tokens"] = n
        # Context-fit check against the requested model's backends.
        model_id = body.get("model")
        if model_id and n is not None:
            model = config.model_by_id(model_id)
            if model is not None:
                fits = []
                for b in model.backends:
                    if not b.enabled:
                        continue
                    if b.context_length is None:
                        fits.append({"backend": f"{b.provider}:{b.model}", "fits": None})
                    else:
                        fits.append({
                            "backend": f"{b.provider}:{b.model}",
                            "fits": b.context_length >= n,
                            "context_length": b.context_length,
                        })
                result["backends"] = fits
    inp = body.get("input")
    if inp is not None:
        result["input_tokens"] = tokenizer.count_embedding_input(inp, mode=mode)
    if "messages_tokens" not in result and "input_tokens" not in result:
        return JSONResponse({"error": "provide messages or input"}, status_code=422)
    return JSONResponse(result)


# ---------- usage export ----------

@router.get("/admin/usage/export")
async def usage_export(hours: int = 720, format: str = "json", include_probes: bool = False):
    """Download raw usage rows as JSON or CSV. Probes excluded by default."""
    from ..usage import export_rows, EXPORT_COLUMNS
    hours = max(1, min(hours, 24 * 365))
    rows = export_rows(hours=hours, include_probes=include_probes)
    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        from fastapi.responses import Response
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="localgateway-usage-{hours}h.csv"'},
        )
    return JSONResponse({"hours": hours, "count": len(rows), "rows": rows})


# ---------- response cache (C3: admin surface for respcache) ----------

@router.get("/admin/cache")
async def response_cache_stats():
    """Response-cache stats for the Settings page (entries, hits)."""
    from .. import respcache
    st = respcache.stats()
    st["enabled"] = bool(getattr(load_config().server, "response_cache_enabled", False))
    return JSONResponse(st)


@router.delete("/admin/cache")
async def response_cache_clear():
    """Drop all cached responses (with usage/log cleanup consistency)."""
    from .. import respcache
    from ..usage import invalidate_tps_map
    n = respcache.clear()
    invalidate_tps_map()
    return JSONResponse({"status": "ok", "cleared": n})


# ---------- config backups ----------

@router.get("/admin/backups")
async def list_backups():
    """List config backup files (newest first) with size/mtime."""
    from ..config import _backup_paths, _config_path
    out = []
    for i, p in enumerate(_backup_paths()):
        if not p.exists():
            continue
        st = p.stat()
        out.append({
            "slot": i,
            "path": str(p),
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "current": False,
        })
    if _config_path.exists():
        st = _config_path.stat()
        out.insert(0, {
            "slot": -1,
            "path": str(_config_path),
            "name": _config_path.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "current": True,
        })
    return JSONResponse({"backups": out})


@router.post("/admin/backups/restore")
async def restore_backup(request: Request):
    """Restore config.json from a backup slot. Validates before swapping."""
    import json as _json
    import os
    import shutil
    from ..config import _backup_at, _config_path, _parse, reload_config

    body = await request.json()
    try:
        slot = int(body.get("slot"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "slot (int) is required"}, status_code=422)
    backup = _backup_at(slot)
    if not backup.exists():
        return JSONResponse({"error": f"Backup slot {slot} does not exist"}, status_code=404)
    try:
        _parse(_json.loads(backup.read_text(encoding="utf-8")))
    except Exception as e:
        return JSONResponse({"error": f"Backup is not a valid config: {e}"}, status_code=422)
    # B10: the pre-restore config must be preserved as the newest .bak —
    # the comment claimed "Normal save rotation preserves the pre-restore
    # config as the newest .bak" but the raw copy2+replace never invoked
    # _rotate_backups, so a misclick on "restore" was unrecoverable. Mirror
    # save_config's rotation, then swap. Stage the restore source FIRST —
    # rotating + copying the current config into slot 0 must not clobber the
    # backup we are about to read back.
    from ..config import _rotate_backups, _backup_path
    shutil.copy2(str(backup), str(_config_path) + ".restore-tmp")
    _rotate_backups()
    shutil.copy2(str(_config_path), str(_backup_path()))
    os.replace(str(_config_path) + ".restore-tmp", str(_config_path))
    cfg = reload_config()
    logs.warn(f"config restored from backup slot {slot} ({backup.name})", provider="config")
    return JSONResponse({"status": "ok", "restored_from": str(backup), "version": cfg.version})


# ---------- API key governance ----------

@router.post("/admin/config/server-key/{key_id}/reveal")
async def reveal_server_api_key(key_id: str):
    """Reveal a client API key's secret (POST-only, like provider keys)."""
    cfg = load_config()
    if key_id == "legacy":
        if not cfg.server.api_key:
            return JSONResponse({"error": "No legacy api_key set"}, status_code=404)
        return JSONResponse({"id": "legacy", "key": cfg.server.api_key})
    k = cfg.api_key_by_id(key_id)
    if k is None:
        return JSONResponse({"error": f"Key '{key_id}' not found"}, status_code=404)
    return JSONResponse({"id": key_id, "key": k.key})


@router.get("/admin/keys/spend")
async def keys_spend():
    """Per-key limits (from config) merged with live spend + RPM usage."""
    from .. import clientquota
    from ..usage import get_all_key_spend
    cfg = load_config()
    spend = get_all_key_spend()
    keys = []
    configured_ids = set()
    for k in cfg.server.api_keys:
        configured_ids.add(k.id)
        s = spend.get(k.id, {"day_cost": 0.0, "day_requests": 0, "month_cost": 0.0, "month_requests": 0})
        keys.append({
            "id": k.id,
            "label": k.label,
            "enabled": k.enabled,
            "rpm": k.rpm,
            "daily_budget_usd": k.daily_budget_usd,
            "monthly_budget_usd": k.monthly_budget_usd,
            "model_allowlist": k.model_allowlist,
            "current_rpm": clientquota.current_rpm(k.id),
            **s,
        })
    if cfg.server.api_key:
        s = spend.get("default", {"day_cost": 0.0, "day_requests": 0, "month_cost": 0.0, "month_requests": 0})
        keys.append({
            "id": "default", "label": "Legacy default key", "enabled": True,
            "rpm": None, "daily_budget_usd": None, "monthly_budget_usd": None,
            "model_allowlist": [], "current_rpm": 0, **s,
        })
    # Keys seen in usage but no longer configured.
    for kid, s in spend.items():
        if kid and kid not in configured_ids and not (kid == "default" and cfg.server.api_key):
            keys.append({"id": kid, "label": "(deleted key)", "enabled": False,
                         "rpm": None, "daily_budget_usd": None, "monthly_budget_usd": None,
                         "model_allowlist": [], "current_rpm": 0, **s})
    return JSONResponse({"keys": keys})


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
    try:
        key = validated_api_key(provider)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    headers = {"Authorization": f"Bearer {key}"}
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
    """Send a minimal probe request to a backend and report the result.

    Probe mechanics (body, implausible-TPS rejection, p50 fallback) live in
    prober.py — this endpoint is the manual, on-demand wrapper.
    """
    from .. import prober

    body = await request.json()
    provider_id = body.get("provider", "")
    model = body.get("model", "")
    stream = bool(body.get("stream", True))
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        result = await prober.probe_with_reprobe(
            client, provider, model, logical_model or None, max_tokens, stream=stream
        )

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
    request_id: str | None = None,
):
    return JSONResponse({
        "logs": logs.get_logs(limit=limit, level=level, search=search,
                              since_id=since_id, request_id=request_id)
    })


@router.delete("/admin/logs")
async def clear_logs_endpoint():
    logs.clear_logs()
    return JSONResponse({"status": "ok"})

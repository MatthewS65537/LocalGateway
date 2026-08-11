"""Prometheus text-exposition endpoint: GET /metrics.

Hand-rolled (zero dependencies), assembled from the same in-memory registries
and usage aggregates that feed /admin/health. The supervisor's catch-all proxy
forwards it, so Grafana scrapes the public port directly. Auth: none when no
API key is set; otherwise the worker/supervisor middlewares require the Bearer
key (same as /admin). P12: windowed values are gauges (a counter that slides
over a 24h window produces garbage rate()); the render is cached ~5s because
each scrape previously ran 6 SQL aggregate queries.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import stats
from .. import circuitbreaker
from ..config import load_config
from ..usage import get_usage_summary
from ..auth import check_api_key

router = APIRouter()

_render_cache: dict[str, tuple[float, str]] = {}
_RENDER_TTL = 5.0


def _esc_label(v: str) -> str:
    """Escape a label value for Prometheus text format."""
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render() -> str:
    config = load_config()
    snap = stats.snapshot()
    circuitbreaker.configure_from_config(config)
    circuits = circuitbreaker.snapshot()

    lines: list[str] = []

    # ---- Per-backend counters + gauges (from in-memory stats) ----
    # Build a model lookup so we can label each backend with its logical model.
    backend_to_model: dict[str, str] = {}
    for m in config.models:
        for b in m.backends:
            backend_to_model[f"{b.provider}:{b.model}"] = m.id

    lines.append("# HELP lga_requests_total Total completed requests per backend")
    lines.append("# TYPE lga_requests_total counter")
    lines.append("# HELP lga_request_errors_total Total failed requests per backend")
    lines.append("# TYPE lga_request_errors_total counter")
    lines.append("# HELP lga_inflight Currently in-flight requests per backend")
    lines.append("# TYPE lga_inflight gauge")

    for key, st in sorted(snap.items()):
        provider, _, backend_model = key.partition(":")
        model = backend_to_model.get(key, "")
        labels = (
            f'model="{_esc_label(model)}",provider="{_esc_label(provider)}",'
            f'backend="{_esc_label(backend_model)}"'
        )
        req = st.get("requests", 0) or 0
        err = st.get("failures", 0) or 0
        inflight = st.get("in_flight", 0) or 0
        lines.append(f"lga_requests_total{{{labels}}} {req}")
        lines.append(f"lga_request_errors_total{{{labels}}} {err}")
        lines.append(f"lga_inflight{{{labels}}} {inflight}")

    # ---- Cumulative cost + tokens (from usage DB, 24h window) ----
    # P12: these were declared `counter` but computed over a trailing 24h
    # window, so the values DECREASED as data aged out and rate() produced
    # garbage. Windowed aggregates are gauges — monotonically increasing
    # counters would need in-process accumulation.
    lines.append("# HELP lga_cost_usd_total Spend per provider (trailing 24h window)")
    lines.append("# TYPE lga_cost_usd_total gauge")
    lines.append("# HELP lga_tokens_total Tokens per provider by kind (trailing 24h window)")
    lines.append("# TYPE lga_tokens_total gauge")

    try:
        summary = get_usage_summary(hours=24)
        for provider, row in sorted(summary.get("by_provider", {}).items()):
            cost = row.get("cost", 0.0) or 0.0
            inp = row.get("input_tokens", 0) or 0
            outp = row.get("output_tokens", 0) or 0
            pl = f'provider="{_esc_label(provider)}"'
            lines.append(f"lga_cost_usd_total{{{pl}}} {cost}")
            lines.append(f'lga_tokens_total{{{pl},kind="input"}} {inp}')
            lines.append(f'lga_tokens_total{{{pl},kind="output"}} {outp}')
    except Exception:
        pass

    # ---- Circuit breaker state ----
    lines.append("# HELP lga_circuit_open Circuit breaker state (1=open, 0=closed)")
    lines.append("# TYPE lga_circuit_open gauge")
    for key, c in sorted(circuits.items()):
        provider, _, backend_model = key.partition(":")
        model = backend_to_model.get(key, "")
        labels = (
            f'model="{_esc_label(model)}",provider="{_esc_label(provider)}",'
            f'backend="{_esc_label(backend_model)}"'
        )
        lines.append(f"lga_circuit_open{{{labels}}} {1 if c.get('open') else 0}")

    # ---- Cache warmth gauge ----
    lines.append("# HELP lga_warm_keys Cache-warm conversation keys per backend")
    lines.append("# TYPE lga_warm_keys gauge")
    try:
        warmth = stats.warmth_snapshot()
        warm_counts: dict[str, int] = {}
        for _fp, bucket in warmth.items():
            for bk, entry in bucket.items():
                warm_counts[bk] = warm_counts.get(bk, 0) + 1
        for key, count in sorted(warm_counts.items()):
            provider, _, backend_model = key.partition(":")
            model = backend_to_model.get(key, "")
            labels = (
                f'model="{_esc_label(model)}",provider="{_esc_label(provider)}",'
                f'backend="{_esc_label(backend_model)}"'
            )
            lines.append(f"lga_warm_keys{{{labels}}} {count}")
    except Exception:
        pass

    return "\n".join(lines) + "\n"


@router.get("/metrics")
async def metrics(request: Request):
    # P12: gate /metrics behind the API key when one is set — the endpoint
    # exposes spend + backend topology, which was world-readable on
    # non-loopback binds. Loopback scrapes stay exempt (check_api_key is
    # loopback-aware), so local Prometheus/Grafana keep working keylessly.
    cfg = load_config()
    if (cfg.server.api_key or cfg.server.api_keys) and not check_api_key(request, cfg):
        return JSONResponse(
            {"error": {"message": "Invalid API key", "type": "authentication_error"}},
            status_code=401,
        )
    now = time.time()
    hit = _render_cache.get("last")
    if hit and now - hit[0] < _RENDER_TTL:
        return PlainTextResponse(hit[1], media_type="text/plain; version=0.0.4")
    body = _render()
    _render_cache["last"] = (now, body)
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

from __future__ import annotations

import asyncio
import random
import time

from . import logs
from . import stats
from . import usage
from .config import load_config
from .ratelimit import ratelimit

_probe_task: asyncio.Task | None = None
_probe_running = False
IMPLAUSIBLE_TPS = 2000
MAX_REPROBE = 3


async def _probe_one(client, provider_cfg, backend_model: str, model_id: str | None, max_tokens: int) -> dict:
    import json

    from .sse import StreamAccumulator, StreamUsage
    from .streaming import compute_tps

    url = f"{provider_cfg.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider_cfg.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_cfg.headers)

    probe_body = {
        "model": backend_model,
        "messages": [{"role": "user", "content": "Write a detailed paragraph explaining how a rainbow forms. Be thorough and specific."}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t0 = time.monotonic()
    acc = StreamAccumulator(start=t0)
    try:
        async with client.stream("POST", url, headers=headers, json=probe_body, timeout=60.0) as resp:
            if resp.status_code != 200:
                text = b""
                async for c in resp.aiter_bytes():
                    text += c
                latency_ms = int((time.monotonic() - t0) * 1000)
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error": text.decode("utf-8", errors="replace")[:500],
                    "latency_ms": latency_ms,
                }
            async for chunk in resp.aiter_bytes():
                if chunk:
                    acc.feed(chunk)

        for ev in acc.finish():
            pass

        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = acc.usage
        output_tokens = usage.output_tokens
        if output_tokens is None:
            output_tokens = acc.estimated_output_tokens()
        input_tokens = usage.input_tokens
        ttft_ms = acc.ttft_ms
        if ttft_ms is None:
            ttft_ms = acc._first_byte_ms
        tps = compute_tps(output_tokens, latency_ms, ttft_ms)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "ttft_ms": ttft_ms,
            "tps": tps,
            "output_tokens": output_tokens,
            "input_tokens": input_tokens,
        }
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "error": str(e)[:500], "latency_ms": latency_ms}


async def _prober_loop(client, interval_s: float, max_tokens: int) -> None:
    global _probe_running
    while _probe_running:
        try:
            cfg = load_config()
            if cfg.server.probe_enabled:
                backends_to_probe = []
                for m in cfg.models:
                    if not m.enabled:
                        continue
                    for b in m.backends:
                        if not b.enabled:
                            continue
                        prov = cfg.provider_by_id(b.provider)
                        if not prov or not prov.enabled:
                            continue
                        if not ratelimit.is_available(b.provider, b.model):
                            continue
                        backends_to_probe.append((prov, b.model, m.id))

                if backends_to_probe:
                    random.shuffle(backends_to_probe)
                    for prov, backend_model, model_id in backends_to_probe[:10]:
                        result = await _probe_one(client, prov, backend_model, model_id, max_tokens)
                        if not result["ok"]:
                            usage.log_request(
                                logical_model=model_id,
                                provider=prov.id,
                                backend_model=backend_model,
                                success=False,
                                error=result.get("error"),
                                latency_ms=result.get("latency_ms"),
                                stream=True,
                            )
                            stats.record_failure(prov.id, backend_model, result.get("error"))
                            logs.warn(
                                f"probe failed: {prov.id}:{backend_model} - {result.get('error')}",
                                provider=prov.id,
                            )
                            await asyncio.sleep(1)
                            continue

                        ttft = result.get("ttft_ms")
                        latency = result.get("latency_ms")
                        tps = result.get("tps")
                        out_tok = result.get("output_tokens", 0)
                        in_tok = result.get("input_tokens", 0)

                        if tps is not None and tps >= IMPLAUSIBLE_TPS:
                            for _ in range(MAX_REPROBE - 1):
                                await asyncio.sleep(0.5)
                                result = await _probe_one(client, prov, backend_model, model_id, max_tokens)
                                if not result["ok"]:
                                    break
                                tps = result.get("tps")
                                if tps is None or tps < IMPLAUSIBLE_TPS:
                                    ttft = result.get("ttft_ms")
                                    latency = result.get("latency_ms")
                                    tps = result.get("tps")
                                    out_tok = result.get("output_tokens", 0)
                                    in_tok = result.get("input_tokens", 0)
                                    break
                            if tps is not None and tps >= IMPLAUSIBLE_TPS:
                                key = f"{prov.id}:{backend_model}"
                                hist = usage.get_backend_percentiles(model_id, hours=168, p=0.5) if model_id else {}
                                p50_tps = hist.get(key, {}).get("tps_p50")
                                if p50_tps is not None:
                                    tps = p50_tps
                                ttft = result.get("ttft_ms")
                                latency = result.get("latency_ms")
                                out_tok = result.get("output_tokens", 0)
                                in_tok = result.get("input_tokens", 0)

                        pricing = cfg.pricing_for(prov.id, backend_model)
                        cost = None
                        if pricing and out_tok:
                            cost = (in_tok * pricing.input) + (out_tok * pricing.output)
                        usage.log_request(
                            logical_model=model_id,
                            provider=prov.id,
                            backend_model=backend_model,
                            success=True,
                            input_tokens=in_tok or None,
                            output_tokens=out_tok or None,
                            cost=cost,
                            latency_ms=latency,
                            ttft_ms=ttft,
                            tps=tps,
                            stream=True,
                        )
                        stats.record_success(prov.id, backend_model, latency_ms=latency, ttft_ms=ttft)
                        await asyncio.sleep(1)
        except Exception as e:
            logs.error(f"prober error: {e}")

        jitter = random.uniform(0.8, 1.2)
        await asyncio.sleep(interval_s * jitter)


def start_prober(client, interval_s: float | None = None, max_tokens: int | None = None) -> None:
    global _probe_task, _probe_running
    if _probe_task is not None:
        return
    cfg = load_config()
    interval = interval_s or cfg.server.probe_interval_s or 3600.0
    tokens = max_tokens or cfg.server.probe_max_tokens or 64
    _probe_running = True
    _probe_task = asyncio.create_task(_prober_loop(client, interval, tokens))
    logs.info("Prober started", interval_s=interval, max_tokens=tokens)


def stop_prober() -> None:
    global _probe_task, _probe_running
    _probe_running = False
    if _probe_task:
        _probe_task.cancel()
        _probe_task = None
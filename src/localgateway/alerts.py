"""Outbound alerting via a generic webhook.

When ``server.alert_webhook_url`` is set, notable gateway events (circuit
breaker trips, budget exhaustion) are POSTed there as JSON:

    {"event": "circuit_open", "title": "...", "detail": {...}, "ts": 1234.5}

Works with ntfy.sh (as JSON topic publish), Slack/Discord incoming-webhook
relays, or any custom endpoint. Delivery is fire-and-forget on a daemon
thread — alerts never block the request path, and failures are logged, not
raised.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

# B13: throttle repeat sends of the same (event, dedup_key) — the budget-ETA
# alert fired on EVERY request while a key was in the <3d window, storming the
# webhook exactly when spend was highest. Minimum interval between sends of the
# same key; a key re-entering the window later can alert again.
_DEDUP_INTERVAL = 6 * 3600.0  # 6 hours
_last_sent: dict[str, float] = {}
_dedup_lock = threading.Lock()


def _webhook_url() -> str | None:
    try:
        from .config import load_config
        return load_config().server.alert_webhook_url
    except Exception:
        return None


def send(event: str, title: str, detail: dict | None = None, dedup_key: str | None = None) -> bool:
    """POST an alert to the configured webhook. Returns True if delivered.

    ``dedup_key`` (e.g. "eta:key1") throttles repeats to one send per
    ``_DEDUP_INTERVAL`` per key, so request-path alerting can't storm the
    webhook. Without a dedup_key the alert always sends (one-shot events)."""
    if dedup_key is not None:
        now = time.time()
        with _dedup_lock:
            last = _last_sent.get(dedup_key, 0.0)
            if now - last < _DEDUP_INTERVAL:
                return False
            _last_sent[dedup_key] = now
    url = _webhook_url()
    if not url:
        return False
    payload = json.dumps({
        "event": event,
        "title": title,
        "detail": detail or {},
        "ts": time.time(),
        "source": "localgateway",
    }).encode("utf-8")

    def _deliver() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as e:
            try:
                from . import logs
                logs.warn(f"alert webhook delivery failed: {e}", provider="alerts")
            except Exception:
                pass

    threading.Thread(target=_deliver, daemon=True).start()
    return True

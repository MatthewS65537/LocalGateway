from __future__ import annotations

import ipaddress

from fastapi import Request

from .config import GatewayConfig

# UI pages: treated the same as /admin for auth purposes (loopback-exempt,
# key-required for remote clients). Shared by supervisor and worker.
PAGE_PATHS = frozenset({"/", "/models", "/providers", "/usage", "/logs", "/settings"})
PAGE_PREFIXES = ("/models/", "/compare/")


def is_page_path(path: str) -> bool:
    return path in PAGE_PATHS or any(path.startswith(p) for p in PAGE_PREFIXES)


def is_loopback_host(host: str | None) -> bool:
    """True when the peer address is a loopback address (127.0.0.0/8, ::1)."""
    if not host:
        return False
    # IPv4-mapped IPv6 loopback (::ffff:127.0.0.1) and bare "127.0.0.1".
    if host.startswith("::ffff:"):
        host = host[7:]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "::1")


def client_host(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def check_api_key(request: Request, config: GatewayConfig) -> bool:
    """Admin auth policy, shared by supervisor and worker.

    - No API key configured: everything allowed (admin UI is open locally).
    - Loopback clients (the local dashboard): allowed without a key.
    - Everyone else: must present the API key as a Bearer token.
    """
    api_key = config.server.api_key
    if not api_key:
        return True
    if is_loopback_host(client_host(request)):
        return True
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer") else auth
    return bool(token) and token == api_key

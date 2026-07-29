from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Cooldown:
    until: float


@dataclass
class RateLimitState:
    _cooldowns: dict[str, _Cooldown] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, provider_id: str, model: str) -> str:
        return f"{provider_id}:{model}"

    def set_cooldown(self, provider_id: str, model: str, retry_after: float | None) -> None:
        if retry_after is None:
            retry_after = 5.0
        until = time.monotonic() + max(0.1, float(retry_after))
        with self._lock:
            self._cooldowns[self._key(provider_id, model)] = _Cooldown(until=until)

    def snooze(self, provider_id: str, model: str, seconds: float) -> None:
        """Manually snooze a backend for a given duration."""
        until = time.monotonic() + max(0.1, float(seconds))
        with self._lock:
            self._cooldowns[self._key(provider_id, model)] = _Cooldown(until=until)

    def unsnooze(self, provider_id: str, model: str) -> None:
        """Remove a manual snooze / cooldown."""
        with self._lock:
            self._cooldowns.pop(self._key(provider_id, model), None)

    def is_available(self, provider_id: str, model: str) -> bool:
        """True if the provider is NOT rate-limited (available to serve)."""
        key = self._key(provider_id, model)
        with self._lock:
            cd = self._cooldowns.get(key)
            if cd is None:
                return True
            if time.monotonic() >= cd.until:
                self._cooldowns.pop(key, None)
                return True
            return False

    def remaining(self, provider_id: str, model: str) -> float:
        key = self._key(provider_id, model)
        with self._lock:
            cd = self._cooldowns.get(key)
            if cd is None:
                return 0.0
            return max(0.0, cd.until - time.monotonic())

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            now = time.monotonic()
            return {
                k: max(0.0, v.until - now) for k, v in self._cooldowns.items() if now < v.until
            }


ratelimit = RateLimitState()
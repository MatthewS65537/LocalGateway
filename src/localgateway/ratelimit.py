from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


SNOOZE_FILE: Path | None = None


def _snooze_file() -> Path:
    global SNOOZE_FILE
    if SNOOZE_FILE is None:
        from .config import _config_path
        SNOOZE_FILE = _config_path.parent / "snooze.json"
    return SNOOZE_FILE


@dataclass
class _Cooldown:
    until: float


@dataclass
class RateLimitState:
    _cooldowns: dict[str, _Cooldown] = field(default_factory=dict)
    _permanent: set[str] = field(default_factory=set)
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
            key = self._key(provider_id, model)
            self._cooldowns[key] = _Cooldown(until=until)
            self._permanent.discard(key)

    def snooze_permanent(self, provider_id: str, model: str) -> None:
        """Permanently snooze a backend until explicitly removed."""
        with self._lock:
            key = self._key(provider_id, model)
            self._permanent.add(key)
            self._cooldowns.pop(key, None)

    def unsnooze(self, provider_id: str, model: str) -> None:
        """Remove a manual snooze / cooldown / permanent snooze."""
        with self._lock:
            key = self._key(provider_id, model)
            self._cooldowns.pop(key, None)
            self._permanent.discard(key)

    def is_available(self, provider_id: str, model: str) -> bool:
        """True if the provider is NOT rate-limited (available to serve)."""
        key = self._key(provider_id, model)
        with self._lock:
            if key in self._permanent:
                return False
            cd = self._cooldowns.get(key)
            if cd is None:
                return True
            if time.monotonic() >= cd.until:
                self._cooldowns.pop(key, None)
                return True
            return False

    def remaining(self, provider_id: str, model: str) -> float:
        """Returns -1 for permanent snooze, 0 for available, else seconds remaining."""
        key = self._key(provider_id, model)
        with self._lock:
            if key in self._permanent:
                return -1.0
            cd = self._cooldowns.get(key)
            if cd is None:
                return 0.0
            return max(0.0, cd.until - time.monotonic())

    def snapshot(self) -> dict[str, float]:
        """Returns remaining seconds (-1 = permanent)."""
        with self._lock:
            now = time.monotonic()
            out: dict[str, float] = {}
            for k in self._permanent:
                out[k] = -1.0
            for k, v in self._cooldowns.items():
                if k in self._permanent:
                    continue
                rem = max(0.0, v.until - now)
                if now < v.until:
                    out[k] = rem
            return out

    def rename_provider(self, old_id: str, new_id: str) -> int:
        """Rename a provider across all snooze/cooldown keys. Returns keys moved."""
        prefix = old_id + ":"
        moved = 0
        with self._lock:
            new_perm = {k for k in self._permanent}
            new_cool = dict(self._cooldowns)
            for k in list(self._permanent):
                if k.startswith(prefix):
                    new_perm.discard(k)
                    new_perm.add(new_id + ":" + k[len(prefix):])
                    moved += 1
            for k, v in list(self._cooldowns.items()):
                if k.startswith(prefix):
                    del new_cool[k]
                    new_cool[new_id + ":" + k[len(prefix):]] = v
                    moved += 1
            self._permanent = new_perm
            self._cooldowns = new_cool
        if moved:
            self.save()
        return moved

    def save(self) -> None:
        """Persist snooze state to disk."""
        now = time.monotonic()
        real_now = time.time()
        data: dict = {"permanent": [], "timed": {}}
        with self._lock:
            for k in self._permanent:
                data["permanent"].append(k)
            for k, v in self._cooldowns.items():
                if k in self._permanent:
                    continue
                remaining = v.until - now
                if remaining > 0:
                    data["timed"][k] = real_now + remaining
        try:
            _snooze_file().write_text(json.dumps(data, indent=2))
        except Exception as e:
            import warnings
            warnings.warn(f"Failed to save snooze state: {e}")

    def load_from_file(self) -> None:
        """Restore snooze state from disk. Call on startup."""
        sf = _snooze_file()
        if not sf.exists():
            return
        try:
            data = json.loads(sf.read_text())
        except Exception as e:
            import warnings
            warnings.warn(f"Failed to load snooze state: {e}")
            return
        real_now = time.time()
        now = time.monotonic()
        with self._lock:
            self._permanent = set(data.get("permanent", []))
            for k, expiry in data.get("timed", {}).items():
                remaining = float(expiry) - real_now
                if remaining > 0:
                    self._cooldowns[k] = _Cooldown(until=now + remaining)


ratelimit = RateLimitState()
ratelimit.load_from_file()
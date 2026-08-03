from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3456
    api_key: str | None = None
    routing_mode: str = "explore"  # explore | failover
    routing_decay: float = 0.4
    probe_enabled: bool = True
    probe_interval_s: float = 3600.0
    probe_max_tokens: int = 64
    chart_enabled: bool = True
    cache_affinity_enabled: bool = False
    cache_affinity_ttl_sec: int = 300
    max_inflight_before_spill: int | None = None
    usage_retention_days: int = 30
    log_retention_lines: int = 10000


class ProviderConfig(BaseModel):
    id: str
    name: str = ""
    base_url: str
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 120.0
    enabled: bool = True
    stream_idle_timeout: float | None = 60.0
    avatar: str = ""  # custom avatar text; empty = first letter of id


class BackendConfig(BaseModel):
    provider: str
    model: str
    priority: int = 1
    enabled: bool = True
    context_length: int | None = None
    max_output_tokens: int | None = None
    cache_supported: bool | None = None


class TimeSlot(BaseModel):
    """A time window with active provider backends.

    ``start_hour``/``end_hour`` are 24h hours (0-23). When ``start_hour`` >
    ``end_hour`` the span wraps midnight (e.g. 22:00-06:00). ``days_of_week``
    uses 0=Mon..6=Sun; an empty list means every day. ``active_providers`` is
    a whitelist of ``"provider:model"`` backend keys; an empty list means all
    backends pass (opt-out style).
    """
    id: str = ""
    name: str = ""
    start_hour: int = Field(default=0, ge=0, le=23)
    end_hour: int = Field(default=23, ge=0, le=23)
    days_of_week: list[int] = Field(default_factory=list)
    active_providers: list[str] = Field(default_factory=list)
    enabled: bool = True


class TimeRoutingConfig(BaseModel):
    """Per-model time-based routing. OFF by default (enabled=False)."""
    enabled: bool = False
    timezone: str = "UTC"
    slots: list[TimeSlot] = Field(default_factory=list)


class ModelConfig(BaseModel):
    id: str
    backends: list[BackendConfig] = Field(default_factory=list)
    description: str | None = ""
    context_length: int | None = None
    enabled: bool = True
    capabilities: dict[str, bool] = Field(default_factory=dict)
    modality: str = ""
    max_output_tokens: int | None = None
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    default_params: dict[str, Any] = Field(default_factory=dict)
    display_name: str = ""
    avatar: str = ""  # custom avatar text; empty = first letter of id/display_name
    time_routing: TimeRoutingConfig = Field(default_factory=TimeRoutingConfig)


class PricingEntry(BaseModel):
    input: float = 0.0
    output: float = 0.0
    cache_read: float | None = None
    cache_write: float | None = None


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)
    pricing: dict[str, PricingEntry] = Field(default_factory=dict)
    version: int = 0  # bumped on every save; used for optimistic-concurrency (409) checks

    def provider_by_id(self, provider_id: str) -> ProviderConfig | None:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    def model_by_id(self, model_id: str) -> ModelConfig | None:
        """Resolve by canonical id or any alias."""
        for m in self.models:
            if m.id == model_id:
                return m
        for m in self.models:
            if model_id in m.aliases:
                return m
        return None

    def pricing_for(self, provider_id: str, model: str) -> PricingEntry:
        return self.pricing.get(f"{provider_id}:{model}", PricingEntry())


_config_lock = threading.RLock()
_config: GatewayConfig | None = None
_config_path: Path = Path("config.json")
_config_mtime: float = -1.0
BACKUP_KEEP = 5
_config_recovered: str | None = None  # path of the backup used for the current config


def set_config_path(path: str | Path) -> None:
    global _config_path
    _config_path = Path(path)


def _file_mtime() -> float:
    try:
        return _config_path.stat().st_mtime if _config_path.exists() else 0.0
    except OSError:
        return 0.0


def _backup_path() -> Path:
    return _config_path.with_suffix(_config_path.suffix + ".bak")


def _backup_paths() -> list[Path]:
    """Newest-first backup paths: config.json.bak, config.json.bak.1, ... """
    paths = [_backup_path()]
    for i in range(1, BACKUP_KEEP):
        paths.append(_config_path.with_suffix(f"{_config_path.suffix}.bak.{i}"))
    return paths


def _backup_at(i: int) -> Path:
    """Backup at slot i: 0 -> .bak, 1 -> .bak.1, ... """
    if i == 0:
        return _backup_path()
    return _config_path.with_suffix(f"{_config_path.suffix}.bak.{i}")


def _rotate_backups() -> None:
    """Shift existing backups up one slot and drop the oldest beyond BACKUP_KEEP.

    Called before config.json is renamed into slot 0, so the chain
    (.bak -> .bak.1 -> .bak.2 -> ...) preserves up to BACKUP_KEEP generations.
    """
    # Drop the oldest slot beyond the keep count (slot BACKUP_KEEP).
    try:
        extra = _backup_at(BACKUP_KEEP)
        if extra.exists():
            extra.unlink()
    except OSError:
        pass
    # Shift .bak.{k} -> .bak.{k+1} for k = BACKUP_KEEP-2 .. 0 (oldest first).
    # Slot BACKUP_KEEP is dropped above and never refilled.
    for i in range(BACKUP_KEEP - 2, -1, -1):
        src = _backup_at(i)
        dst = _backup_at(i + 1)
        try:
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        except OSError:
            pass


def last_good_backup() -> Path | None:
    """Newest readable backup, or None."""
    for p in _backup_paths():
        if p.exists():
            return p
    return None


def config_recovered_from() -> str | None:
    """Path of the backup currently serving as config, if recovery happened."""
    return _config_recovered


def _parse(raw: dict) -> GatewayConfig:
    """Parse config with one-time field migrations for removed/renamed keys."""
    if isinstance(raw, dict):
        # reasoning_mode was removed from the schema (2026-08-01); strip stale keys.
        for p in raw.get("providers", []):
            if isinstance(p, dict):
                p.pop("reasoning_mode", None)
    return GatewayConfig.model_validate(raw)


def load_config() -> GatewayConfig:
    """Load config, reloading from disk if the file changed (mtime-based).

    This lets a separate supervisor process edit config.json while the worker
    picks up changes automatically. On corrupt JSON, falls back to the last-good
    .bak file, then to an empty config, so the gateway always starts.
    """
    global _config, _config_mtime, _config_recovered
    with _config_lock:
        mtime = _file_mtime()
        if _config is None or mtime != _config_mtime:
            candidates = []
            if _config_path.exists():
                candidates.append(_config_path)
            for bak in _backup_paths():
                if bak.exists():
                    candidates.append(bak)

            loaded = None
            used_backup: str | None = None
            for path in candidates:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    loaded = _parse(raw)
                    if path != _config_path:
                        used_backup = str(path)
                    break
                except (json.JSONDecodeError, ValueError, OSError) as e:
                    logs_fallback = f"config parse failed for {path}: {e}"
                    try:
                        from . import logs as _logs
                        _logs.error(logs_fallback, provider="config")
                    except Exception:
                        pass
                    continue

            if loaded is None:
                used_backup = None
            _config = loaded if loaded is not None else GatewayConfig()
            _config_recovered = used_backup
            _config_mtime = mtime
        return _config


def save_config(cfg: GatewayConfig) -> None:
    global _config, _config_mtime, _config_recovered
    with _config_lock:
        cfg.version += 1
        data = json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False)
        # Rotate backups before overwriting (atomic via os.replace).
        if _config_path.exists():
            try:
                _rotate_backups()
                _config_path.rename(_backup_path())
            except OSError:
                pass
        tmp = _config_path.with_suffix(_config_path.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, _config_path)
        _config = cfg
        _config_recovered = None  # a clean save clears any recovery state
        _config_mtime = _file_mtime()


def reload_config() -> GatewayConfig:
    global _config, _config_mtime
    with _config_lock:
        _config = None
        _config_mtime = -1.0
        return load_config()


def get_config_dict() -> dict[str, Any]:
    return load_config().model_dump()
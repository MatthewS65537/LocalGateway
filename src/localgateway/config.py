from __future__ import annotations

import json
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


class ProviderConfig(BaseModel):
    id: str
    name: str = ""
    base_url: str
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 120.0
    enabled: bool = True
    stream_idle_timeout: float | None = 60.0
    reasoning_mode: str = "auto"  # auto (dual-emit) | passthrough


class BackendConfig(BaseModel):
    provider: str
    model: str
    priority: int = 1
    enabled: bool = True
    context_length: int | None = None
    max_output_tokens: int | None = None


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


def set_config_path(path: str | Path) -> None:
    global _config_path
    _config_path = Path(path)


def _file_mtime() -> float:
    try:
        return _config_path.stat().st_mtime if _config_path.exists() else 0.0
    except OSError:
        return 0.0


def load_config() -> GatewayConfig:
    """Load config, reloading from disk if the file changed (mtime-based).

    This lets a separate supervisor process edit config.json while the worker
    picks up changes automatically.
    """
    global _config, _config_mtime
    with _config_lock:
        mtime = _file_mtime()
        if _config is None or mtime != _config_mtime:
            if _config_path.exists():
                raw = json.loads(_config_path.read_text(encoding="utf-8"))
                _config = GatewayConfig.model_validate(raw)
            else:
                _config = GatewayConfig()
            _config_mtime = mtime
        return _config


def save_config(cfg: GatewayConfig) -> None:
    global _config, _config_mtime
    with _config_lock:
        _config = cfg
        _config_path.write_text(
            json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _config_mtime = _file_mtime()


def reload_config() -> GatewayConfig:
    global _config, _config_mtime
    with _config_lock:
        _config = None
        _config_mtime = -1.0
        return load_config()


def get_config_dict() -> dict[str, Any]:
    return load_config().model_dump()
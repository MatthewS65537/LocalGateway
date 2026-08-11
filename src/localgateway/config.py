from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ApiKeyConfig(BaseModel):
    """A client API key with optional quotas. ``id`` is a slug used for usage
    attribution; ``key`` is the Bearer secret clients send."""
    id: str
    key: str
    label: str = ""
    enabled: bool = True
    rpm: int | None = None                    # max requests/min; None = unlimited
    daily_budget_usd: float | None = None     # hard stop at 402 when exceeded
    monthly_budget_usd: float | None = None
    model_allowlist: list[str] = Field(default_factory=list)  # empty = all models


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 3456
    api_key: str | None = None  # legacy single key (full access); prefer api_keys
    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
    routing_mode: str = "explore"  # explore | failover
    routing_decay: float = 0.4
    stats_routing_enabled: bool = True  # use measured TPS for sort/weights
    circuit_breaker_enabled: bool = False
    circuit_breaker_threshold: int = 3      # consecutive failures before trip
    circuit_breaker_backoff_s: float = 60.0  # base backoff; doubles per trip (cap 30m)
    probe_enabled: bool = True
    probe_interval_s: float = 3600.0
    probe_max_tokens: int = 64
    chart_enabled: bool = True
    cache_affinity_enabled: bool = False
    cache_affinity_ttl_sec: int = 300
    max_inflight_before_spill: int | None = None
    usage_retention_days: int = 30
    log_retention_lines: int = 10000
    tokenizer: str = "auto"  # auto (tiktoken if installed) | heuristic
    alert_webhook_url: str | None = None  # POST JSON alerts (circuit trips, budgets)
    response_cache_enabled: bool = False  # global opt-in; per-model via ModelConfig.cache_responses
    response_cache_ttl_sec: int = 3600
    response_cache_max_entries: int = 1000


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


def validated_api_key(provider: ProviderConfig) -> str:
    """Return the provider's API key, raising ValueError with an actionable
    message when it contains non-ASCII characters.

    httpx/httpcore encodes request headers as ASCII, so a corrupted key (e.g.
    the redacted-placeholder sentinel accidentally persisted by a config
    round-trip) would otherwise surface as a cryptic UnicodeEncodeError.
    """
    key = provider.api_key or ""
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(
            f"Stored API key for provider '{provider.id}' is invalid (contains "
            "non-ASCII characters — the redacted placeholder may have been saved "
            "over it). Re-enter the key on the Providers page."
        ) from None
    return key


class BackendConfig(BaseModel):
    provider: str
    model: str
    priority: int = 1
    enabled: bool = True
    context_length: int | None = None
    max_output_tokens: int | None = None
    cache_supported: bool | None = None
    weight: float = 1.0  # within-tier load-balancing weight (1.0 = equal share). 0 = never first; higher = more traffic.


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

    @field_validator("modality", mode="before")
    @classmethod
    def _normalize_modality(cls, v):
        # "text+vision" was the legacy label for non-text-only models; the UI now
        # exposes a single "multimodal" bucket. Coerce on load so old configs /
        # backups migrate automatically without a manual edit.
        if v == "text+vision":
            return "multimodal"
        return v

    max_output_tokens: int | None = None
    endpoint: str = "chat"  # chat | embeddings | responses — which upstream API this model serves
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    default_params: dict[str, Any] = Field(default_factory=dict)
    display_name: str = ""
    avatar: str = ""  # custom avatar text; empty = first letter of id/display_name
    time_routing: TimeRoutingConfig = Field(default_factory=TimeRoutingConfig)
    probe_interval_s: float | None = None  # per-model override; None = server default
    cache_responses: bool = False  # opt-in response caching (F5 stage 1: exact)


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

    def api_key_by_id(self, key_id: str) -> ApiKeyConfig | None:
        for k in self.server.api_keys:
            if k.id == key_id:
                return k
        return None


# ---------- N1: preflight validation ----------

REDACTED_KEY = "\u2022\u2022\u2022\u2022\u2022\u2022"


def validate_config(cfg: GatewayConfig) -> list[dict]:
    """Dry-run lint of a candidate config. Returns a list of issues.

    Each issue: {severity: "error"|"warning", code: str, path: str, message: str}.
    Errors should block the save; warnings allow "Save anyway".
    """
    issues: list[dict] = []

    # --- duplicate provider ids ---
    seen_pids: set[str] = set()
    for p in cfg.providers:
        if p.id in seen_pids:
            issues.append({"severity": "error", "code": "duplicate_provider_id",
                           "path": f"providers[{p.id}]", "message": f"Duplicate provider id '{p.id}'"})
        seen_pids.add(p.id)

    # --- duplicate model ids + alias collisions ---
    seen_mids: set[str] = set()
    all_aliases: dict[str, str] = {}  # alias -> model_id
    for m in cfg.models:
        if m.id in seen_mids:
            issues.append({"severity": "error", "code": "duplicate_model_id",
                           "path": f"models[{m.id}]", "message": f"Duplicate model id '{m.id}'"})
        seen_mids.add(m.id)
        for alias in (m.aliases or []):
            if alias in seen_mids or alias in all_aliases:
                issues.append({"severity": "error", "code": "alias_collision",
                               "path": f"models[{m.id}].aliases",
                               "message": f"Alias '{alias}' collides with another model id or alias"})
            all_aliases[alias] = m.id

    # --- orphan backends (backend references missing provider) ---
    for m in cfg.models:
        for i, b in enumerate(m.backends):
            if b.provider not in seen_pids:
                issues.append({"severity": "error", "code": "orphan_backend",
                               "path": f"models[{m.id}].backends[{i}]",
                               "message": f"Backend references unknown provider '{b.provider}'"})

    # --- placeholder / corrupted keys ---
    for p in cfg.providers:
        if p.api_key == REDACTED_KEY:
            issues.append({"severity": "error", "code": "placeholder_key",
                           "path": f"providers[{p.id}].api_key",
                           "message": f"Provider '{p.id}' has the redacted placeholder key — re-enter the real key"})
        elif p.api_key:
            try:
                p.api_key.encode("ascii")
            except (UnicodeEncodeError, UnicodeDecodeError):
                issues.append({"severity": "error", "code": "non_ascii_key",
                               "path": f"providers[{p.id}].api_key",
                               "message": f"Provider '{p.id}' key contains non-ASCII characters (likely corrupted)"})

    # --- warnings ---

    # model with no backends
    for m in cfg.models:
        if not m.backends:
            issues.append({"severity": "warning", "code": "model_no_backends",
                           "path": f"models[{m.id}]", "message": f"Model '{m.id}' has no backends"})

    # disabled provider referenced by enabled backend
    pid_enabled = {p.id: p.enabled for p in cfg.providers}
    for m in cfg.models:
        for i, b in enumerate(m.backends):
            if b.enabled and b.provider in pid_enabled and not pid_enabled[b.provider]:
                issues.append({"severity": "warning", "code": "disabled_provider_referenced",
                               "path": f"models[{m.id}].backends[{i}]",
                               "message": f"Backend references disabled provider '{b.provider}'"})

    # pricing gaps
    for m in cfg.models:
        for i, b in enumerate(m.backends):
            key = f"{b.provider}:{b.model}"
            if key not in cfg.pricing:
                issues.append({"severity": "warning", "code": "pricing_gap",
                               "path": f"models[{m.id}].backends[{i}]",
                               "message": f"No pricing for '{key}' — cost tracking will show $0"})

    # max_output_tokens > context_length
    for m in cfg.models:
        mot = m.max_output_tokens
        for i, b in enumerate(m.backends):
            ctx = b.context_length or m.context_length
            if mot and ctx and mot > ctx:
                issues.append({"severity": "warning", "code": "mot_exceeds_context",
                               "path": f"models[{m.id}].backends[{i}]",
                               "message": f"max_output_tokens ({mot}) exceeds context_length ({ctx})"})

    return issues


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


def config_mtime() -> float:
    """Mtime of the config file currently on disk (0 when missing)."""
    return _file_mtime()


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
    # A full-config PUT can rename model/provider IDs without going through
    # /admin/rename, so drop the routing TPS cache (keyed by model_id). Lazy
    # import avoids a config→usage circular dependency at load time.
    try:
        from . import usage as _usage
        _usage.invalidate_tps_map()
    except Exception:
        pass


def reload_config() -> GatewayConfig:
    global _config, _config_mtime
    with _config_lock:
        _config = None
        _config_mtime = -1.0
        return load_config()


def get_config_dict() -> dict[str, Any]:
    return load_config().model_dump()
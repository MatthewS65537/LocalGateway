# LocalGateway

A local OpenAI-compatible token gateway that collates many provider subscriptions behind logical model IDs with priority-tier failover and weighted load balancing. Think of it as a small, self-hosted OpenRouter that runs entirely on your machine.

- **One logical model, many backends.** Map a single model ID (e.g. `glm-5.2`) to several upstream providers with per-backend priority. Tier 1 backends are load-balanced as a group; tier 2+ act as failover tiers.
- **OpenAI-compatible.** Exposes `/v1/chat/completions` and `/v1/models` (also mirrored at `/api/v1/...`), so any client that speaks the OpenAI API can point at it.
- **Intelligent routing.** Explore / failover modes, weighted round-robin, rate-limit (429 + `Retry-After`) handling with snooze, **circuit breaking** on flaky backends, **stats-driven sorts** (measured TPS p50, throughput-per-dollar), and an optional **cache-affinity** mode that pins requests to warmed backends to maximize prompt-cache hits.
- **Streaming with pre-first-chunk fallback.** If the first backend errors before any bytes are sent, the request transparently retries on the next tier.
- **Usage & cost tracking.** SQLite-backed accounting of tokens, cost, TPS percentiles, cache tokens, and reasoning tokens per backend, with CSV/JSON export.
- **Governance.** Multiple API keys with per-key RPM limits, daily/monthly budgets, model allowlists, and per-user (`user` field) attribution.
- **Embeddings.** `POST /v1/embeddings` routes through the same tier/failover engine as chat (opt-in per logical model).
- **Background probing.** Periodically tests backends for latency/TPS and availability; implausible results are rejected and retried.
- **Web dashboard.** A built-in multi-page UI (FastAPI + Jinja2 + vanilla JS) for models, providers, usage, logs, and settings — all configurable without editing JSON by hand.

## Architecture

LocalGateway runs as a **supervisor + worker** pair so the admin UI stays available even while the gateway is stopped.

```
localgateway (CLI)
  └─ main.py            → SUPERVISOR (default) or worker (--no-supervisor)
       ├─ supervisor.py → always-on on the config port (default 3456)
       │     • Jinja2 UI: /, /models, /models/{id}, /compare/{ids},
       │                  /providers, /usage, /logs, /settings
       │     • /admin/server/*, config, usage, logs, models CRUD
       │     • proxies everything else → worker
       └─ worker.py     → gateway (port + 1000, e.g. 4456)
             • /v1/chat/completions, /v1/models
             • /api/v1/chat/completions, /api/v1/models
             • /admin/health, discover, test, CRUD, catalog
```

The supervisor stays up on the config port and proxies API traffic to the worker, which runs on `port + 1000`. The dashboard can start/stop the worker without taking itself down.

### Key modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Pydantic schema + alias-aware model lookup + mtime-based hot reload |
| `router.py` | Tier-based selection, explore/failover, provider prefs, cache-affinity 5-band priority |
| `cache.py` | Request fingerprinting for prompt-cache affinity |
| `streaming.py` | SSE streaming, pre-first-chunk fallback, `default_params` merge, response headers |
| `stats.py` | In-flight tracking + cache warmth registry |
| `ratelimit.py` | 429 handling, snooze/cooldown state keyed by `provider:backend_model` |
| `prober.py` | Background backend probing with implausible-TPS rejection |
| `usage.py` | SQLite usage/cost accounting, TPS percentiles, rename migration |
| `supervisor.py` / `worker.py` | The two FastAPI apps |

## Installation

Requires Python 3.10+.

```bash
git clone <repo-url> LocalGateway
cd LocalGateway
python3 -m pip install -e ".[dev]"
# optional: real tokenizer for input estimates + /admin/tokens
python3 -m pip install -e ".[tokens]"
```

## Quick start

1. Copy the sample config and fill in your provider credentials:

   ```bash
   cp config.example.json config.json
   ```

   Edit `config.json` and replace `REPLACE_WITH_YOUR_API_KEY` with a real key. See [Configuration](#configuration) for the full schema.

2. Start the server (runs the supervisor + worker on port 3456 by default):

   ```bash
   ./run.sh
   # or explicitly:
   python3 -m localgateway.main
   ```

3. Open the dashboard at <http://127.0.0.1:3456>.

4. Point any OpenAI-compatible client at the gateway:

   ```bash
   curl http://127.0.0.1:3456/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "my-model",
       "messages": [{"role": "user", "content": "Hello!"}]
     }'
   ```

### CLI flags

```
python3 -m localgateway.main [--config config.json] [--host HOST] [--port PORT] [--no-supervisor]
```

- `--config / -c` — path to config file (default `config.json`)
- `--host` — override the configured host
- `--port / -p` — override the configured port
- `--no-supervisor` — run the gateway worker directly without the web UI / supervisor

### Docker

```bash
docker build -t localgateway .
docker run -p 3456:3456 \
  -v "$(pwd)/config.json:/app/config.json" \
  -v lg-data:/app/data \
  localgateway
```

Set `LGW_CONFIG`, `LGW_HOST`, `LGW_PORT` env vars to override defaults. The container binds `0.0.0.0` — set an API key (legacy or `api_keys`) before exposing the port.

## Configuration

All configuration lives in `config.json` (gitignored — see [Security](#security)). A fully documented starting point is in [`config.example.json`](config.example.json). The file is hot-reloaded on mtime change, so you can edit it (or use the web UI) without restarting.

### Top-level shape

```jsonc
{
  "server":   { ... },   // gateway + UI settings
  "providers": [ ... ],  // upstream OpenAI-compatible endpoints
  "models":    [ ... ],  // logical model IDs → backend assignments
  "pricing":   { ... }   // per-backend cost rates
}
```

### `server`

| Field | Default | Description |
| --- | --- | --- |
| `host` | `127.0.0.1` | Bind address |
| `port` | `3456` | Supervisor/dashboard port (worker uses `port + 1000`) |
| `api_key` | `null` | Legacy full-access key clients must send to access the gateway |
| `api_keys` | `[]` | Per-key quotas: `id`, `key`, `label`, `enabled`, `rpm`, `daily_budget_usd`, `monthly_budget_usd`, `model_allowlist` (see [Access keys](#access-keys-and-quotas)) |
| `routing_mode` | `explore` | `explore` (weighted across tier 1) or `failover` (strict priority) |
| `routing_decay` | `0.4` | Weight decay across tiers in explore mode |
| `stats_routing_enabled` | `true` | Use measured TPS p50 (usage DB + probes) for throughput/value sorts and explore weights |
| `circuit_breaker_enabled` | `false` | Trip backends out of rotation after repeated 5xx/timeouts |
| `circuit_breaker_threshold` | `3` | Consecutive failures before the circuit opens |
| `circuit_breaker_backoff_s` | `60.0` | Base backoff; doubles per trip, capped at 30 minutes |
| `probe_enabled` | `true` | Run background backend probes |
| `probe_interval_s` | `3600` | Seconds between probes |
| `probe_max_tokens` | `64` | Token budget per probe |
| `chart_enabled` | `true` | Render throughput charts on model detail |
| `cache_affinity_enabled` | `false` | Enable cache-aware routing (see below) |
| `cache_affinity_ttl_sec` | `300` | Warmth TTL for cache affinity |
| `max_inflight_before_spill` | `null` | In-flight threshold before spilling warm→cold (`null` = sticky) |
| `usage_retention_days` | `30` | Days of usage history kept (pruned daily, not just at startup) |
| `log_retention_lines` | `10000` | Cap on the structured log table |
| `tokenizer` | `auto` | `auto` (uses `tiktoken` when installed) or `heuristic` (chars÷4) |
| `alert_webhook_url` | `null` | Webhook for alerts (circuit trips, budget exhaustion) — see [Alerts](#alerts) |

### `providers[]`

Each provider is an OpenAI-compatible endpoint.

| Field | Default | Description |
| --- | --- | --- |
| `id` | — | Unique slug, referenced by backends |
| `name` | `""` | Display name |
| `base_url` | — | e.g. `https://api.example.com/v1` |
| `api_key` | `""` | Provider API key |
| `headers` | `{}` | Extra headers to send upstream |
| `timeout` | `120.0` | Request timeout (seconds) |
| `enabled` | `true` | Include in routing |
| `stream_idle_timeout` | `60.0` | Idle timeout for SSE streams |
| `avatar` | `""` | Custom avatar text (empty = first letter of id) |

### `models[]`

A logical model maps one ID to multiple backends with priorities.

| Field | Default | Description |
| --- | --- | --- |
| `id` | — | Logical model ID (what clients send) |
| `backends[]` | `[]` | Upstream assignments (see below) |
| `aliases[]` | `[]` | Alternate IDs that resolve to this model |
| `display_name` | `""` | UI display name |
| `description` | `""` | Free-text description |
| `modality` | `""` | e.g. `text`, `multimodal` |
| `context_length` | `null` | Override context window (else max across backends) |
| `max_output_tokens` | `null` | Override max output (else max across backends) |
| `capabilities` | `{}` | `text`/`vision`/`audio`/`tools`/`json_mode`/... flags |
| `tags[]` | `[]` | Free-form tags |
| `default_params` | `{}` | Params merged into client requests (client wins) |
| `enabled` | `true` | Expose to clients |
| `avatar` | `""` | Custom avatar text |
| `time_routing` | — | Per-model time-based routing (see below, off by default) |

#### `backends[]`

| Field | Default | Description |
| --- | --- | --- |
| `provider` | — | Provider `id` to use |
| `model` | — | Upstream model name |
| `priority` | `1` | Tier (1 = load-balance group, 2+ = failover) |
| `enabled` | `true` | Include this backend |
| `context_length` | `null` | Per-backend context window |
| `max_output_tokens` | `null` | Per-backend max output |
| `cache_supported` | `null` | `null` = auto-detect, `true`/`false` to force |
| `endpoint` (model) | `"chat"` | `chat` or `embeddings` — which upstream API this model serves |

### `pricing`

A flat map keyed by `provider:upstream_model`:

```jsonc
"pricing": {
  "my-provider:upstream-model-name": {
    "input": 0.0,        // $/token
    "output": 0.0,       // $/token
    "cache_read": null,  // $/token for cached input (null = unknown)
    "cache_write": null // $/token for cache writes
  }
}
```

## Routing

### Tiers and failover

Each backend has a `priority` (tier). Within tier 1, backends are load-balanced according to `routing_mode`:

- **`explore`** — weighted selection across tier 1, decaying into lower tiers (`routing_decay`). Good for spreading load and discovering which backend is fastest.
- **`failover`** — strict priority order; tier 2 is only tried if tier 1 is exhausted/rate-limited.

On a failure **before the first byte** of a response, the request transparently retries on the next available backend/tier.

### Circuit breaker (optional)

When `server.circuit_breaker_enabled` is `true`, backends that fail with genuine backend errors (HTTP 5xx, timeouts, connection errors — never 4xx or 429s, which use the cooldown path) are cut out of rotation after `circuit_breaker_threshold` consecutive failures. The circuit re-opens on an exponentially growing backoff (`circuit_breaker_backoff_s`, doubled per trip, capped at 30 minutes); the first request after the backoff is a half-open trial. Open circuits are surfaced on the dashboard health list, the model-detail provider table, and `GET /admin/circuit`.

### Stats-driven routing (on by default)

When `server.stats_routing_enabled` is `true`:
- `provider.sort = "throughput"` ranks by **measured TPS p50** from the usage DB (probes included), instead of the old success-rate/latency proxy.
- A new `provider.sort = "value"` ranks by throughput per dollar (TPS p50 ÷ input+output price).
- Explore mode weights the tier-1 round-robin start by measured performance (ε-greedy-style), so fast backends earn more traffic while slow ones are still probed.

### Time-based routing (per-model, off by default)

Each backend has a `priority` (tier). Within tier 1, backends are load-balanced according to `routing_mode`:

- **`explore`** — weighted selection across tier 1, decaying into lower tiers (`routing_decay`). Good for spreading load and discovering which backend is fastest.
- **`failover`** — strict priority order; tier 2 is only tried if tier 1 is exhausted/rate-limited.

On a failure **before the first byte** of a response, the request transparently retries on the next available backend/tier.

### Per-request provider preferences

Clients can steer routing per request with an OpenRouter-style `provider` field in the request body:

```jsonc
{
  "model": "my-model",
  "provider": {
    "order": ["my-provider"],
    "ignore": ["slow-provider"],
    "allow_fallbacks": true,
    "sort": "cache"
  },
  "messages": [...]
}
```

Responses disclose the chosen backend via `X-Provider` and `X-Backend` headers.

### Access keys and quotas

The gateway supports any number of client API keys (`server.api_keys`), each with:

- `rpm` — requests per minute (sliding window; excess gets `429` with `Retry-After`).
- `daily_budget_usd` / `monthly_budget_usd` — hard spend stops (`402 budget_exceeded`); at ≥80% of a budget, responses carry an `X-Budget-Warning` header.
- `model_allowlist` — logical model IDs this key may call (else `403`).

Requests are attributed per key (usage rows get `api_key_id`) and per end user via the OpenAI `user` field (`end_user` column). The Usage page has per-key and per-user tables, and `GET /admin/keys/spend` merges config with live spend. The legacy `server.api_key` remains a full-access fallback key. Manage everything from **Settings → Access Keys**.

### Alerts

When `server.alert_webhook_url` is set, notable events (circuit trips, budget exhaustion) are POSTed as JSON — compatible with ntfy.sh, Slack/Discord webhook relays, or any endpoint:

```json
{"event": "circuit_open", "title": "Circuit open on openrouter:gpt-5", "detail": {"backend": "..."}, "ts": 1750000000.0, "source": "localgateway"}
```

Test delivery from **Settings → Alerts** (`POST /admin/alerts/test`).

### Embeddings

Logical models with `endpoint: "embeddings"` serve `POST /v1/embeddings` (and `/api/v1/embeddings`) through the same tier/failover engine as chat. Input token estimates drive context-length filtering; cost uses the input price × `usage.total_tokens`. Responses disclose `X-Provider`/`X-Backend` and every call is logged like a chat call.

### Other public endpoints

- `GET /v1/models/{id}` (and `/api/v1/...`) — OpenAI-style model retrieve; resolves aliases.
- `POST /admin/tokens` — token-count estimation for a `messages` array or embeddings `input` (tiktoken when installed), including a per-backend context-fit report.
- `GET /admin/usage/export?hours=N&format=csv|json` — raw usage rows download.
- `GET /admin/backups` / `POST /admin/backups/restore` — inspect and restore the five rotating config backups.
- `GET /admin/circuit` / `POST /admin/circuit/reset` — circuit-breaker introspection and manual reset.

### Time-based routing (per-model, off by default)

Each model can define **time slots** — time windows during which only specific provider backends serve traffic. During a matching slot, excluded backends are filtered out *before* the tier / cache-affinity logic runs, so the existing routing rules still apply within the active set.

```jsonc
"time_routing": {
  "enabled": false,      // OFF by default; enable per-model
  "timezone": "UTC",     // IANA timezone name
  "slots": [
    {
      "id": "peak-9-17",  // auto-generated when blank
      "name": "Peak hours",
      "start_hour": 9,
      "end_hour": 17,
      "days_of_week": [0, 1, 2, 3, 4],  // 0=Mon..6=Sun; empty = every day
      "active_providers": ["provider-a:gpt-4o", "provider-b:gpt-4o"],
      "enabled": true
    }
  ]
}
```

- **`start_hour` / `end_hour`**: 24h hours (0–23). Wraps at midnight (e.g. `start_hour=22, end_hour=6`).
- **`active_providers`**: `provider:model` backend whitelist. Empty list = all backends pass (opt-out style).
- **`days_of_week`**: 0=Mon..6=Sun. Empty = every day.
- Slots are evaluated in order; the first matching enabled slot wins.
- If no slot matches, all backends pass (same as having the feature off).

### Cache affinity (optional)

When `server.cache_affinity_enabled` is `true`, the router prefers backends that have a warm prompt cache for the current request fingerprint (sha1 of the system prompt + all but the final user turn). Selection uses a 5-band priority — warm+idle > warm+busy > cold+idle > cold+busy > ratelimited — so cache hits take precedence over tiers and round-robin. This is off by default and changes nothing when disabled.

- **Warmth** is recorded per backend when a request succeeds on a cache-capable backend (explicit `cache_supported` on the backend, or auto-detected from reported `prompt_tokens.cached_tokens` / `cache_creation_input_tokens` usage). A successful response without cache tokens still warms a backend declared `cache_supported: true`, so non-reporting providers participate when you opt them in.
- **Fingerprints** are bounded: messages are truncated to 2000 chars and disambiguated with a per-message length + tail hash, so distinct long prompts never collide onto the same cache slot.
- **`max_inflight_before_spill`** caps concurrency on a warm backend; beyond it, traffic spills to cold backends.
- Configure all of this from **Settings → Cache Affinity** in the UI. `GET/DELETE /admin/warmth` exposes/resets the live warmth registry (also available from the Settings page via *Clear Warmth Data*).

## Testing

The suite uses `pytest` with `pytest-asyncio`. A mock upstream provider is spun up per test.

```bash
PYTHONPATH=src python3 -m pytest
```

If you see a flaky `test_resolve_alias` failure under random test ordering, run deterministically:

```bash
PYTHONPATH=src python3 -m pytest -p no:randomly
```

## Security

**`config.json` contains your provider API keys and is gitignored.** Never commit it. The repo ships a sanitized [`config.example.json`](config.example.json) — copy it to `config.json` and fill in your own credentials.

- `config.json`, `snooze.json`, and the `data/` directory (which holds `usage.db`) are all listed in `.gitignore`.
- The default bind address is `127.0.0.1` (localhost only). Set `server.api_key` if you expose the gateway beyond localhost.
- If you accidentally commit secrets, rotate the affected keys immediately — git history is not a safe place for them.

## License

[MIT](LICENSE) © 2026 MatthewS65537
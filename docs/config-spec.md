# Mneme Config Spec — `unified_mneme` branch

Single-file configuration for the Mneme proxy. Consolidates the four
scattered config surfaces (env vars, hardcoded constants,
`setup_config.json`, launch scripts) into one `mneme.yaml` the proxy
actually loads at startup.

Status: IMPLEMENTED — `load_config()` + env promotion + backend generalization
(`type: ollama|openai` + `providers:` registry) + per-model sampling are live in
`proxy/mneme_proxy.py`. This doc is the schema reference. See
`mneme.yaml.example` for a copy-able config.

## Design goals

1. One file (`$MNEME_CHUNK_DIR/mneme.yaml`) holds every knob we tune.
2. Backend choice (`ollama` vs `openai`-compatible) is a config entry, not a
   branch. Adding an OpenAI-standard provider is a config change only.
3. Fail loud on unknown/typo'd keys (no silent ignore).

## Precedence

```
config file  <  environment variable  <  per-request override
```

Env vars win over the file, so `launch.sh` / generated `start_proxy.sh`
keep working unchanged. A config value is used only when the matching env
var is unset. Per-request overrides (e.g. `temperature` passed to
`query_model`) win over both.

## Schema

```yaml
# mneme.yaml — lives next to the DB, e.g. ~/mneme/chunks/mneme.yaml

backend:
  type: openai              # "ollama" | "openai"   (openai = OpenAI-compatible)
  provider: openrouter      # key into `providers:` (ignored when type=ollama)
  ollama_url: http://localhost:11434

providers:                  # OpenAI-compatible providers. Add one -> new backend.
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY       # read key from this env var
    model: deepseek/deepseek-v4-flash
    embed_model: voyageai/voyage-4-lite
    label_model: meta-llama/llama-3.2-3b-instruct
    headers: {}              # optional extra headers (e.g. HTTP-Referer/X-Title)
    # OpenRouter-only reliability (ignored by other OpenAI-compatible providers):
    fallback_models: []      # [openai/gpt-5.4-mini, ...] — walked in order if the primary model's providers all fail
    provider: {}             # { ignore: [deepinfra], preferred_max_latency: { p90: 3 }, ... } — provider routing prefs
  deepseek:
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    model: deepseek-chat
    embed_model: ""          # no embeddings -> set "" to disable / reuse another
    label_model: meta-llama/llama-3.2-3b-instruct
    headers: {}
  # groq / openai / together / mistral / xai / fireworks ... same shape

sampling:                    # defaults; per-model overrides live under `models:`
  temperature: 0.3
  top_p: 0.95
  top_k: 64                  # ollama-only (ignored by openai path)
  ctx_tokens: 256000
  completion_reserve: 8192
  max_tokens: 2048           # output cap — currently unset for chat

timeouts:
  chat_timeout: 120          # anti-grind guardrail (foreground turns)
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30
  edge_failures: 2
  edge_ratio: 0.5

storage:
  chunk_dir: ~/mneme/chunks      # per-instance dir: config + prompts + logs
  db_path: ~/mneme/chunks/mneme.db  # memory DB file — SQLite + FAISS index live beside it (portable; shareable across proxies/machines)
  port: 8080
  inject_system: true
  memory_only: false          # true = memory-only build: strategy/self-improving layer off (memory + grading + tools stay on)
  memory_enabled: true        # master switch: false = NO memory — no retrieval/injection, no staging/archiving, search_memory off (run with tools only)
  staging_turns: 1           # flush cadence — swarm default: every turn (general Mneme: 6)
  staging_idle: 120          # seconds of inactivity before flush
  context_recent_extra: 14   # extra USER TURNS of recent context kept in the tool loop beyond staging_turns (default 14 -> 15-turn window)
  belief_evolution: false    # gated off by default — floods the backend

retrieval:
  max_injected_tokens: 6000
  route_threshold: 0.08
  classify_threshold: 0.78
  baseline_noise: 0.20       # fallback only; _calibrate_noise() overrides at startup
  inject_min_similarity: 0.45  # absolute cosine floor — below this, nothing is injected.
                               # ⚠ EMBEDDER-DEPENDENT: every embedder has its own similarity
                               # scale — tune per model (voyage-4-lite ~0.62, snowflake-arctic-embed2 ~0.45)
  keyword_fallback: false      # pad sparse FAISS with LIKE-substring matches (junk-prone)
  age_decay_days: 7
  max_siblings: 3
  max_chunk_words: 500
  max_chunk_size: 10000

caps:                        # rarely-tuned char/truncation limits
  max_history_messages: 32
  db_msg_cap: 8000
  compress_threshold: 500
  compress_max_tok: 2048
  max_tool_forward: 12000
  tool_followup_budget: 50000
  chunk_size: 4000

models:                      # P8 per-model overrides, keyed by model name
  deepseek/deepseek-v4-flash:
    temperature: 0.3
    max_tokens: 2048
    reasoning_field: reasoning        # where the provider puts chain-of-thought
    quirks: ["empty-query search_memory guard", "thinking->content fallback"]
  muse-glimmer:30b:
    temperature: 1.0
    top_k: 64
    num_ctx: 32768
    reasoning_field: thinking
    quirks: ["peg-native bug fixed in Ollama 0.32.13+"]
  meta-llama/llama-3.2-3b-instruct:
    temperature: 0.0                  # labeler — deterministic
    max_tokens: 15
```

## Key reference

`[env]` = already env-backed today. `[const]` = hardcoded constant today
(needs promotion). `[new]` = new key introduced by this refactor.

### backend (refactor: replaces `MNEME_BACKEND=ollama|openrouter`)
| key | default | today | notes |
|---|---|---|---|
| type | `openai` | [env] `MNEME_BACKEND` | `ollama` \| `openai`; `openai` = any OpenAI-compatible provider |
| provider | `openrouter` | [new] | selects a `providers:` entry |
| ollama_url | `http://localhost:11434` | [const] `OLLAMA_URL` | ollama path only |

### providers (refactor: new)
Per-provider `base_url` + `api_key_env` + 3 model names. OpenRouter's
`HTTP-Referer`/`X-Title` headers move into `headers:`. The `reasoning_field`
quirk moves to `models:` (field name is per-model, not per-provider).

OpenRouter-specific reliability keys (only added to OpenRouter requests — a plain
OpenAI-compatible provider ignores them):
- `fallback_models:` (list) — model fallbacks, walked in order if every provider
  for the primary model fails (whole-model outage, cold-start no-content, context-
  length, moderation). Mirrors OpenRouter's `models` array.
- `provider:` (mapping) — routing prefs passed as the `provider` object:
  `ignore` / `only` / `order` / `allow_fallbacks` / `preferred_max_latency` /
  `preferred_min_throughput`.
- `stream:` (bool, default `true`) — `true` streams tokens (fast first-token hang
  detection); `false` lets OpenRouter buffer the full reply so it can transparently
  fail over a mid-stream stall (streaming commits the first token and disables
  failover). Config-only, so flipping it later is a one-line edit.

### sampling
| key | default | today |
|---|---|---|
| temperature | 0.3 | [env] `MNEME_TEMPERATURE` (note: global default is currently 0.3 = `OLLAMA_TEMP`; P8 flags that Muse's 1.0 leaked globally) |
| top_p | 0.95 | [env] `MNEME_TOP_P` |
| top_k | 64 | [env] `MNEME_TOP_K` (ollama only) |
| ctx_tokens | 256000 | [env] `MNEME_CTX_TOKENS` (sent to Ollama as `num_ctx`) |
| completion_reserve | 8192 | [env] `MNEME_COMPLETION_RESERVE` |
| max_tokens | 65536 | [env] `MNEME_MAX_TOKENS` (sent to Ollama as `num_predict`; matches Hermes `default_max_tokens`) |
| reasoning_enabled | 0 (off) | [env] `MNEME_REASONING_ENABLED`. Thinking is OFF by default — a reasoning model (Qwen3.6 etc.) can runaway-think on a trivial ask. Set `1` to opt in. |
| reasoning_effort | — | [env] `MNEME_REASONING_EFFORT` (low/high/max) for effort-level models (deepseek); implies reasoning on. |

### timeouts
| key | default | today |
|---|---|---|
| chat_timeout | 120 | [env] `MNEME_CHAT_TIMEOUT` |
| novelty_timeout | 600 | [env] `MNEME_NOVELTY_TIMEOUT` |
| embed_timeout | 60 | [const] hardcoded in `_embed_single` |
| label_timeout | 30 | [const] hardcoded in `_llm_topic_label` |
| edge_failures | 2 | [env] `MNEME_EDGE_FAILURES` |
| edge_ratio | 0.5 | [env] `MNEME_EDGE_RATIO` |

### storage
| key | default | today |
|---|---|---|
| chunk_dir | `~/mneme/chunks` | [env] `MNEME_CHUNK_DIR` |
| db_path | `<chunk_dir>/mneme.db` | [env] `MNEME_DB_PATH` — the memory DB file; the FAISS index lives beside it. Defaults to `<chunk_dir>/mneme.db` when unset (back-compat). |
| port | 8080 | [env] `MNEME_PORT` |
| inject_system | true | [env] `MNEME_INJECT_SYSTEM` |
| memory_enabled | true | [const] `MEMORY_ENABLED` — master switch: false = NO memory (no retrieval/injection/staging, search_memory auto-off) |
| staging_turns | 1 | [const] `STAGING_TURNS` |
| staging_idle | 120 | [const] `STAGING_IDLE` |
| context_recent_extra | 14 | [const] `CONTEXT_RECENT_EXTRA` — recent-convo window = staging_turns + this |
| belief_evolution | false | [env] `MNEME_BELIEF_EVOLUTION` (just added) |

### retrieval
| key | default | today |
|---|---|---|
| max_injected_tokens | 6000 | [const] `MAX_INJECTED_TOKENS` |
| route_threshold | 0.08 | [const] `ROUTE_THRESHOLD` |
| classify_threshold | 0.78 | [const] `CLASSIFY_THRESHOLD` |
| baseline_noise | 0.20 | [const] `BASELINE_NOISE` (fallback; calibrated at startup) |
| inject_min_similarity | 0.45 | [const] `INJECT_MIN_SIMILARITY` — absolute cosine floor; below it, nothing is injected. **Embedder-dependent**: every embedding model has its own similarity scale — measure and tune (voyage-4-lite ~0.62, snowflake-arctic-embed2 ~0.45) |
| keyword_fallback | false | [const] `KEYWORD_FALLBACK` — pad sparse FAISS with LIKE-substring matches (off by default) |
| age_decay_days | 7 | [const] `AGE_DECAY_DAYS` |
| max_siblings | 3 | [const] `MAX_SIBLINGS` |
| max_chunk_words | 500 | [const] `MAX_CHUNK_WORDS` |
| max_chunk_size | 10000 | [const] `MAX_CHUNK_SIZE` |

### caps (char/truncation limits — rarely tuned, still promoted for completeness)
| key | default | today |
|---|---|---|
| max_history_messages | 32 | [const] `MAX_HISTORY_MESSAGES` |
| db_msg_cap | 8000 | [const] `DB_MSG_CAP` |
| compress_threshold | 500 | [const] `COMPRESS_THRESHOLD` |
| compress_max_tok | 2048 | [const] `COMPRESS_MAX_TOK` |
| max_tool_forward | 12000 | [const] `MAX_TOOL_FORWARD` |
| tool_followup_budget | 50000 | [const] `TOOL_FOLLOWUP_BUDGET` — cap on the accumulated tool-loop followup; older results compacted away before re-query |
| chunk_size | 4000 | [const] `CHUNK_SIZE` |

### tools (native bootstrap + built-tool registry)
| key | default | today |
|---|---|---|
| native | auto | `NATIVE_TOOLS_MODE` (auto/on/off) — inject proxy-owned bash/write unless the client supplied its own |
| dir | ~/mneme/chunks/tools | `TOOLS_DIR` — canonical directory built tools are stored in + run from |
| bash_timeout | 30 | `BASH_TIMEOUT` — seconds before a native bash command is killed |
| inject_min_similarity | 0.75 | `TOOL_INJECT_MIN_SIM` — stricter than retrieval; a built tool is auto-injected only above this |
| inject_max | 3 | `TOOL_INJECT_MAX` — max built tools auto-injected per turn |
| inject_tokens | 600 | `TOOL_INJECT_TOKENS` — token budget for injected tool descriptions |
| search_memory | true | `MNEME_TOOL_SEARCH_MEMORY` (1/0) — expose the search_memory tool |
| list_tools | true | `MNEME_TOOL_LIST_TOOLS` (1/0) — expose the list_tools tool |
| read_tool | true | `MNEME_TOOL_READ_TOOL` (1/0) — expose the read_tool tool |
| read_file | true | `MNEME_TOOL_READ_FILE` (1/0) — expose the read_file tool |
| fetch_url | true | `MNEME_TOOL_FETCH_URL` (1/0) — expose the fetch_url tool |
| web_search | true | `MNEME_TOOL_WEB_SEARCH` (1/0) — expose the web_search tool |

## Known gotchas to fix during the sweep

- **Duplicate `CHUNK_SIZE`** — defined twice (line 62 = 2000, line 1259 =
  3000; the second wins). Collapse into one config key.
- **`BASELINE_NOISE`** — set to 0.20 then overwritten by `_calibrate_noise()`
  at startup. Treat the config value as fallback only, and log the calibrated
  value at startup.
- **`DIM`** — hardcoded 1024 (embedding dimension). Must match the configured
  `embed_model`; a mismatched embedder breaks the FAISS index (the
  auto-re-embed path from commit 53f996c mitigates, but it's a migration).
- **Env-var mapping** — use `MNEME_*` snake_case for every promoted constant,
  e.g. `MNEME_STAGING_TURNS`, `MNEME_ROUTE_THRESHOLD`, `MNEME_MAX_INJECTED_TOKENS`.

## Backend generalization (how "add a provider" becomes config-only)

Today the dispatch is `MNEME_BACKEND == "openrouter"` at three call sites
(query/embed/label). The refactor changes the concept to
`backend.type: ollama | openai`, where `openai` is any OpenAI-standard
provider selected from `providers:`. Three OpenRouter-isms move out of code
into config:

1. `_or_headers()`'s `HTTP-Referer`/`X-Title` → `providers.<name>.headers`.
2. The `reasoning` field name (deepseek-v4-flash streams `delta.reasoning`;
   other providers use `reasoning_content` or nothing) → `models.<name>.reasoning_field`.
3. Tool-call argument JSON-string handling → already tolerates dict vs string.

Providers this makes config-only (OpenAI-standard, Bearer auth):
OpenAI, DeepSeek, Groq, Together, Fireworks, Mistral, xAI, Perplexity,
OpenRouter.

Providers that still need a NEW adapter branch (not OpenAI-standard):
Anthropic (`x-api-key` + `anthropic-version`), Google Gemini, AWS Bedrock
(SigV4).

## Implementation phases

- **Phase 1 (LOW, ~1-2h):** `load_config()` reads `mneme.yaml` into
  `os.environ` defaults at startup, before constants are read. Covers the ~21
  already-env-backed vars with zero downstream changes.
- **Phase 2 (LOW-MEDIUM, ~2-3h):** promote the ~20 `[const]` values to
  env-backed in one sweep, with a startup `[CONFIG]` dump logging every
  resolved setting (so a missed promotion is visible, not silent).
- **Phase 3 (MEDIUM):** `backend.type`/`providers:` refactor + per-model
  `models:` merge (P8) + prompt hot-reload (P9 `POST /admin/reload`).

## Branch note

`unified_mneme` is forked from `openrouter-backend` (which already carries
both backends behind `MNEME_BACKEND` plus the Aug 18 hardening fixes). The
branch merge is therefore a strategy-layer reconciliation (port the
`novelty-thinking` fixes/features onto this tree), not a backend rewrite.

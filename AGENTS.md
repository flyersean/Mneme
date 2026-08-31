# AGENTS.md — Setting up Mneme and authoring extensions

This file is the instruction manual for **AI agents** (coding agents, orchestration
agents, etc.) that need to (1) stand up a Mneme proxy instance and (2) build an
**extension** (an orchestrator or any other driver) that consumes Mneme proxies.

It is self-contained: every config key and the full HTTP contract are specified below.
The two other reference docs are `docs/config-spec.md` (exhaustive env-var mapping) and
`mneme.yaml.example` (a tuned, copy-able proxy config).

---

## 1. What Mneme is (the mental model you must preserve)

Mneme is a **proxy**, not a model, not a framework. A single running proxy is a thin
Flask service that points at three *separate, swappable* parts:

```
  Mneme proxy  ──▶  provider   (runs a chat model + an embedder + a labeler)
               ──▶  database   (portable SQLite + FAISS index — the memory)
               ──▶  UI         (built-in chat page + /instructions prompt editor)
```

- The **provider** is chosen by config (`backend.type` + `providers:`). One proxy can talk
  to Ollama while another talks to OpenRouter.
- The **database** is portable and independent of any one proxy's config: `storage.db_path`
  points at the SQLite file, and the FAISS index lives in the same directory. Many proxies
  on different machines can share one DB.
- **Extensions are NOT part of the stack.** An extension is an external consumer that talks
  to proxies **only over HTTP**. It must never import or reference the proxy's code.

The `extensions/swarm/` orchestrator is the worked example of an extension. Keep the
separation clean: adding an extension must never require changing proxy code, and must not
break the ability to mix providers against one DB.

---

## 2. Setting up a Mneme proxy

A proxy needs: the code, a backend, and a config file. The single source of truth is
**`mneme.yaml`**.

### 2.1 Install

```bash
git clone https://github.com/flyersean/Mneme.git && cd Mneme
./scripts/install.sh          # installs Ollama (if needed) + systemd keep-alive
```

Or use the interactive setup wizard, which asks for the DB dir, backend, models, port, and
writes `mneme.yaml` + a start script for you:

```bash
python3 scripts/mneme_setup.py
```

### 2.2 Config location + precedence

The proxy reads `mneme.yaml` once at startup, resolving it in this order:

1. `--config <path>` CLI arg
2. `MNEME_CONFIG` env var
3. `$MNEME_CHUNK_DIR/mneme.yaml`

Setting resolution (highest wins):

```
environment variable  >  mneme.yaml  >  built-in default
```

Every key is also reachable as a `MNEME_*` env var (e.g. `storage.port` → `MNEME_PORT`).
The proxy **fails loudly on unknown/typo'd keys** — do not invent keys.

### 2.3 Per-instance vs shared DB (important)

- `storage.chunk_dir` = the **per-instance** directory: this proxy's config, prompts
  (`instructions/`), and logs. Each proxy instance has its own.
- `storage.db_path` = the **shared** memory DB file. Two proxies can share one DB by giving
  them the same `db_path` while each keeps its own `chunk_dir`.

The setup wizard lays this out as `<db>/instances/<port>/mneme.yaml` (config) with
`db_path: <db>/mneme.db` (shared DB).

---

## 3. `mneme.yaml` — full spec (proxy config)

### 3.1 Complete copy-able example

```yaml
backend:
  type: openai              # "ollama" | "openai"   (openai = any OpenAI-compatible provider)
  provider: openrouter      # which `providers:` entry to use (ignored when type=ollama)
  ollama_url: http://localhost:11434   # used only when type=ollama

providers:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY     # key is read from THIS env var, never stored here
    model: deepseek/deepseek-v4-flash   # chat model
    embed_model: voyageai/voyage-4-lite # embeddings model
    label_model: meta-llama/llama-3.2-3b-instruct  # topic-label model
    headers: {}                         # optional (HTTP-Referer / X-Title, etc.)
    fallback_models: []                 # OpenRouter-only: walked if the primary model fails
    provider: {}                        # OpenRouter-only routing prefs
    stream: true                        # OpenRouter-only: stream vs buffer+failover

sampling:
  temperature: 0.3
  top_p: 0.95
  top_k: 64                 # ollama-only (ignored by the openai path)
  ctx_tokens: 256000
  completion_reserve: 8192  # tokens reserved for the reply, out of ctx_tokens
  # max_tokens: 2048        # output cap — leave unset for chat (truncates summaries)
  reasoning_enabled: 0      # thinking OFF by default (reasoning models runaway-think)

timeouts:
  chat_timeout: 120
  ollama_chat_timeout: 120  # Ollama cold-start model load delays first token
  first_token_timeout: 30
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30
  edge_failures: 2
  edge_ratio: 0.5

storage:
  chunk_dir: ~/mneme/chunks          # PER-INSTANCE: config + prompts + logs
  db_path: ~/mneme/chunks/mneme.db   # SHARED: memory DB (FAISS index lives beside it)
  port: 8080
  inject_system: true
  memory_only: true                  # true = strategy/learning layer OFF (memory + grading + tools stay on)
  staging_turns: 1                   # flush to memory every N turns (swarm default 1)
  staging_idle: 120                  # ...or after this many idle seconds
  belief_evolution: false

retrieval:
  max_injected_tokens: 6000          # token budget for memory injected each turn
  inject_min_similarity: 0.45        # THE main knob — absolute cosine floor; below it nothing injects.
                                     # ⚠ EMBEDDER-DEPENDENT: voyage-4-lite ~0.62, snowflake-arctic-embed2 ~0.45
  strategy_min_similarity: 0.40      # second (lower) floor for strategy retrieval
  keyword_fallback: false            # LIKE-substring fallback (junk-prone; off)
  route_threshold: 0.08              # deprecated for retrieval
  classify_threshold: 0.78           # unused (dead config)
  baseline_noise: 0.20               # fallback only; auto-calibrated at startup
  age_decay_days: 7
  max_siblings: 3
  max_chunk_words: 500
  max_chunk_size: 10000

caps:
  max_history_messages: 32
  db_msg_cap: 8000
  compress_threshold: 500
  compress_max_tok: 2048
  max_tool_forward: 12000
  chunk_size: 4000

models:                              # per-model overrides; keyed by EXACT model name
  deepseek/deepseek-v4-flash:
    temperature: 0.2
    max_tokens: 2048
    reasoning_field: reasoning
```

### 3.2 Key reference

| Section | Keys (default) |
|---|---|
| `backend` | `type` (openai), `provider` (openrouter), `ollama_url` (http://localhost:11434) |
| `providers.<name>` | `base_url`, `api_key_env`, `model`, `embed_model`, `label_model`, `headers` {}, `fallback_models` [], `provider` {}, `stream` true |
| `sampling` | `temperature` 0.3, `top_p` 0.95, `top_k` 64, `ctx_tokens` 256000, `completion_reserve` 8192, `max_tokens` (unset), `reasoning_enabled` 0, `reasoning_effort` |
| `timeouts` | `chat_timeout` 120, `ollama_chat_timeout` 120, `first_token_timeout` 30, `novelty_timeout` 600, `embed_timeout` 60, `label_timeout` 30, `edge_failures` 2, `edge_ratio` 0.5 |
| `storage` | `chunk_dir` ~/mneme/chunks, `db_path` <chunk_dir>/mneme.db, `port` 8080, `inject_system` true, `memory_only` false, `staging_turns` 1, `staging_idle` 120, `belief_evolution` false |
| `retrieval` | `max_injected_tokens` 6000, `inject_min_similarity` 0.45, `strategy_min_similarity` 0.40, `keyword_fallback` false, `age_decay_days` 7, `max_siblings` 3, `max_chunk_words` 500, `max_chunk_size` 10000 |
| `caps` | `max_history_messages` 32, `db_msg_cap` 8000, `compress_threshold` 500, `compress_max_tok` 2048, `max_tool_forward` 12000, `chunk_size` 4000 |
| `models.<name>` | `temperature`, `top_p`, `top_k`, `num_ctx`, `max_tokens`, `reasoning_field`, `quirks` [] |

**Critical rules:**
- `retrieval.inject_min_similarity` is **embedder-dependent**. Every embedding model has its
  own cosine scale — re-tune whenever you change `embed_model` (voyage-4-lite ~0.62,
  snowflake-arctic-embed2 ~0.45). Same for `strategy_min_similarity` (must stay below it).
- The embedding dimension (`DIM`, hardcoded 1024) must match the configured `embed_model`.
  A mismatched embedder breaks the FAISS index — a re-embed migration is required.
- Unknown keys abort startup. `models:` keys must be the exact model name string.

---

## 4. The proxy API (the contract extensions use)

An extension talks to a proxy over HTTP only. The proxy is OpenAI-compatible.

### 4.1 Chat completion (the one an orchestrator uses)

```
POST http://localhost:<port>/v1/chat/completions
Content-Type: application/json

{"model": "default", "messages": [{"role": "system", "content": "..."},
                                   {"role": "user", "content": "..."}]}
```

`model` is `"default"` — each proxy is hard-wired to one chat model via its config. The
response is standard OpenAI shape; extract the text at:

```
response.json()["choices"][0]["message"]["content"]
```

Per-request generation overrides are opt-in via a nested `options` object (sent by the
swarm orchestrator). They win over the proxy's own config for that call only:
`temperature`, `top_p`, `top_k` (Ollama only), and `max_tokens` (mapped to `num_predict` on
Ollama). Bare top-level `temperature`/`max_tokens` fields from a generic client are
IGNORED — the proxy does not silently override its own config.

### 4.2 Full endpoint table

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to storage |
| POST | `/search` | Debug search `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status":"ok","chunks":N,"backend":"model"}` |
| GET | `/` `/chat` | Built-in chat UI |
| GET | `/instructions` | Prompt reference + editor |
| GET | `/list` | List all chunks + metadata |
| GET/POST | `/capabilities` | Capability-edge records |
| POST | `/mode/think` | Novelty thinking mode |
| POST | `/mode/learn` | Learning mode |
| GET/POST | `/preferences` | User preferences |

---

## 5. `swarm_config.yaml` — full spec (orchestrator config)

This is the config for `extensions/swarm/swarm_orchestrator.py`. It is the reference for
what an orchestrator config should look like.

### 5.1 Complete copy-able example

```yaml
ollama_url: http://localhost:11434   # base URL for backend=ollama steps
timeout: 600                         # default per-call timeout (seconds)

steps:
  - name: outline                     # name = label AND goto/if jump target
    backend: mneme                    # "mneme" (default) | "ollama"
    port: 8080                        # required for backend=mneme
    options: { temperature: 0.7 }     # per-step override (wins over the proxy config for THIS call only)
    system_prompt: "You are the Outline step. ..."
    read_dir: raw                     # directory to read context from (relative to cwd)
    write_dir: brain/outline          # directory to write output.txt into
    clear_dir: raw                    # wipe after reading (NOT with goto/if)

  - name: review
    backend: mneme
    port: 8084
    system_prompt: "...end with exactly one line: VERDICT: APPROVE or VERDICT: REVISE"
    read_dir: brain/draft
    write_dir: brain/review
    if:
      condition: matches              # contains | equals | startswith | endswith | matches
      value: '(?i)VERDICT\s*:\s*APPROVE'
      then: finalize                  # label, or END
      else: revise

  - name: revise
    backend: mneme
    port: 8083
    read_dir: brain/review
    write_dir: brain/draft
    goto: review                      # jump back to a named step

  - name: raw_step                    # example of a non-proxy step
    backend: ollama
    model: llama3.1:8b                # required for backend=ollama
    options: { temperature: 0.8 }     # optional ollama gen options
    read_dir: brain/draft
    write_dir: Speak
```

### 5.2 Step schema (every key)

| Key | Required | Meaning |
|---|---|---|
| `name` | no | label used as a `goto`/`if` target and in logs |
| `backend` | no | `mneme` (default) or `ollama` |
| `port` | mneme only | proxy port to POST to |
| `model` | ollama only | Ollama model name |
| `options` | no | per-step generation override. `mneme`: OpenAI-style `temperature`/`top_p`/`top_k`/`max_tokens`. `ollama`: Ollama `temperature`/`top_p`/`top_k`/`num_predict`. Wins over the proxy's config for this call only. |
| `system_prompt` | no | system message. For `mneme`, the role prompt usually lives in the proxy's own `instructions/`; only set here for an extra prepended instruction. |
| `read_dir` | no | dir to read all files from (recursive, dot-files skipped) → `"NO_INPUT"` if absent/empty |
| `write_dir` | no | dir to write `output.txt` into |
| `clear_dir` | no | dir to wipe. **Cannot be combined with `goto`/`if`.** |
| `goto` | no | label to jump to next. **Cannot be combined with `clear_dir`/`if`.** |
| `if` | no | branch on this step's output (see below) |
| `timeout` | no | per-step request timeout override |

### 5.3 Control flow

- `if` branches on the step's output. `condition` is `contains` / `equals` / `startswith` /
  `endswith` / `matches` (regex). `then:`/`else:` name a step label, or `END` (reserved).
- `goto` jumps unconditionally to a named step.
- `END` stops the run. There is no iteration cap — the loop runs until it hits `END` or is
  interrupted.

---

## 6. Writing a new orchestrator (requirements)

An orchestrator is any program that drives Mneme proxies. To be a *correct* extension, it
must satisfy all of these:

1. **HTTP only.** Talk to proxies via `POST /v1/chat/completions` (OpenAI format). Never
   `import` the proxy, never read its DB or FAISS index directly, never shell into its
   process. If you need the proxy's state, use its HTTP endpoints (`/health`, `/list`,
   `/search`, `/save`).

2. **OpenAI request shape.** `{"model": "default", "messages": [{"role", "content"}]}`.
   The `model` value is `"default"` unless you know the proxy is configured otherwise.

3. **Read the response defensively.** `choices[0].message.content`. Treat a non-200 or a
   malformed body as a hard error (fail loud) — do not silently continue with an empty
   result.

4. **Folder IO is the state convention.** Read inputs from `read_dir`, write results to
   `write_dir/output.txt`, wipe scratch with `clear_dir`. Paths are relative to the run
   directory. Skip dot-files/dirs when reading (editor/tooling artifacts).

5. **Control flow in config, not code.** Prefer `goto`/`if`/`END` in the YAML so the loop is
   editable without touching the driver. The driver only executes: read → call model →
   write → branch.

6. **Two backends.** A step is either `mneme` (POST to a proxy port) or `ollama`
   (POST to `<ollama_url>/api/chat` with `{model, messages, stream:false, options?}`).

7. **Self-contained.** Depend only on `requests` + `pyyaml` (or stdlib). No proxy code, no
   model SDKs.

8. **Never assume one proxy.** The orchestrator must not care how many proxies there are,
   which providers they use, or whether they share a DB — those are proxy-level concerns.
   One step may target an Ollama proxy while the next targets an OpenRouter proxy against
   the same DB.

---

## 7. Authoring a Mneme extension (convention)

A new extension lives at `extensions/<name>/` and is a self-contained HTTP consumer.

Required layout:

```
extensions/<name>/
  README.md          # what it does, how to run, how to adapt
  <your script>      # the driver (orchestrator)
  <your config>.yaml # the loop/step definition (if config-driven)
```

Rules:

1. **HTTP-only.** Same contract as §6. An extension uses the proxy, it is not part of it.
2. **Relative paths** in config so the extension runs from any working directory (create a
   run dir, drop inputs in, run from there). No absolute `/workspace/...` or pod-specific
   paths.
3. **Example ports, not real ones.** Ship the config with placeholder ports and document
   that they must be pointed at the user's actual proxies. Note RunPod reserves `8081`.
4. **Add a `README.md`** with: what it does, the run command, the config schema, and how to
   adapt it (ports, backends, control flow).
5. **Reference it in the main README** under the `## Extensions` section.

The reference implementation is `extensions/swarm/` — read it before writing a new one.

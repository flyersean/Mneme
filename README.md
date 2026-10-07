# Mneme — persistent memory and tools for AI agents

> ⚠️ **Experimental / pre-1.0 — under active development.** Built iteratively with AI, reviewed by a human.

**Mneme is a persistent-memory and tool proxy for AI agents.** It sits between any
OpenAI-compatible client and a model backend, archives conversations into searchable
memory, retrieves the relevant parts back into later turns, and gives the model a
persistent tool layer. It runs against **local Ollama models** or any **hosted
OpenAI-compatible provider** (OpenRouter, OpenAI, DeepSeek, Groq, …). Point Pi,
Open WebUI, a script, or any OpenAI-compatible app at it.

```text
      any OpenAI-compatible client
                   │
                   ▼
        ┌────────────────────────┐
        │      Mneme proxy       │
        │  memory · tools · MCP  │
        │  provenance · harness  │
        └───────┬────────┬───────┘
                │        │
                ▼        ▼
          SQLite + FAISS   model backend
          (memory DB)      Ollama / hosted API
```

## Why it's not just a chat-history database

Three decisions separate it from "store the transcript, paste it back":

- **Retrieval has a confidence floor.** Mneme doesn't inject the nearest memory just
  because it's the nearest. A chunk must clear an absolute similarity threshold to be
  used at all — irrelevant context is worse than none.

- **Memory carries provenance, not just content.** Every chunk is tagged *observed*
  (user input, a fetched page, a tool result) or *claim* (model output). Model-generated
  content is re-injected with an `[UNVERIFIED]` marker so the model can't quietly
  re-assert its own earlier output as fact — which is how a hallucination, once saved,
  becomes permanent.

- **Bad memories are correctable.** Any chunk can be flagged or taken out of
  circulation, with every action in an audit log. A model can *propose* a chunk is
  wrong; only you can act on it.

## Quick start

Three scripts take you from a fresh machine to a running proxy. Run the first two on the
**host** (laptop or GPU pod); the third on your **laptop** only for a remote pod.

| Script | Where | What it does |
|---|---|---|
| `install.sh` | host | Installs Python deps + the repo, then offers the big optional components (Ollama, Chromium, Hound MCP) one at a time — each auto-skipped when already installed, otherwise asked `[y/N]`. Idempotent. |
| `mneme_setup.py` | host | Interactive wizard. For each of the three model roles — chat, embedder, labeler — it asks the same three things: provider (the full catalog, same list as the chat page's picker), API key if the provider needs one (offers any saved key), and the model id (free-text; Ollama chat offers its pulled list + "enter a name"). Each role may use a different provider. Then: context window, optional Pi, and port. Writes config + start script, launches and health-checks the proxy. |
| `mneme_connect.py` | laptop | (Remote pod only.) Opens a stay-alive SSH tunnel and prints the local URLs. |

**1. Install** (on the host):

```bash
# Interactive — asks y/N for each big download (Ollama, Chromium, Hound):
curl -sSL -o /tmp/install.sh https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/install.sh && MNEME_BRANCH=agent-harness bash /tmp/install.sh

# Or non-interactive, install everything (the old one-liner behaviour):
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/install.sh | MNEME_BRANCH=agent-harness MNEME_YES=1 bash
```

**macOS** (⚠ untested — no Mac was available to verify):

```bash
curl -sSL -o /tmp/install_mac.sh https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/install_mac.sh && MNEME_BRANCH=agent-harness bash /tmp/install_mac.sh
```

**2. Configure** (on the host):

```bash
curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/mneme_setup.py && MNEME_BRANCH=agent-harness python3 /tmp/setup.py
```

**3. Connect** (your laptop — remote pod only):

```bash
curl -sSL -o /tmp/mneme_connect.py https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/mneme_connect.py && python3 /tmp/mneme_connect.py
```

The installer always installs Python deps and the repo; the big downloads — Ollama, two
Chromium builds for the browser tools, and the Hound web stack — are opt-in. Each is
skipped automatically when already installed, and otherwise asked `[y/N]`. Skip them all
with `MNEME_YES=0`, or install them all without prompting with `MNEME_YES=1` (or the
per-component `MNEME_INSTALL_OLLAMA` / `_CHROMIUM` / `_HOUND` flags). Privileged steps
(apt cleanup, a systemd keep-alive drop-in, a `/usr/local/bin` helper) are skipped
automatically when you're not root — the proxy itself needs none of them. Prefer to read
first? `git clone` and run `./scripts/install.sh`.

Once running, the proxy is at `http://localhost:8080/` — dashboard at `/`, chat UI at
`/chat`, OpenAI-compatible API at `/v1`.

## What you get

| URL | Page |
|---|---|
| `/` | **Dashboard** — live status (model, chunk count) + links to every page below. |
| `/chat` | **Chat UI** — the main interface: chat, slash-commands (`/runs`, `/run`, …), a file browser, the model switcher, and theme picker. |
| `/memory` | **Memory management** — review, filter, flag and remove stored chunks (reversible; nothing is deleted). |
| `/instructions` | **Prompt editor** — every injected prompt, editable inline. |
| `/templates` | **Model templates** — view/edit/import/export known-good settings and Modelfiles. |
| `/ollama` | **Ollama** — browse and pull local models. |
| `/runs` | **Agent harness** — durable multi-step runs, events, skills, profiles, approvals. |
| `/strategies` | **Strategy layer** — the self-improving memory's learned strategies. |
| `/extensions` | **Extensions** — discover, configure, run and stop HTTP extensions. |

The UI ships with **light and dark themes** (and supports custom ones — a theme is one
CSS file in `static/themes/`, listed by `GET /themes` and picked from the nav dropdown).

## Security — read this first

**Mneme is for local or otherwise trusted environments.** It's a tool proxy, not a
hardened service. When enabled, it can expose capabilities equivalent to giving a model
a shell on the host — `bash`, `write`, `read_file`, configured MCP servers, and
model-directed tool use. None of this is sandboxed.

**There is no authentication, and the proxy binds to `127.0.0.1` by default — keep it
that way.** For remote access, use an SSH tunnel (`mneme_connect.py` does this), a VPN,
or a reverse proxy that handles auth. Binding wider is a deliberate opt-out:

```bash
MNEME_BIND=0.0.0.0   # reachable from the network; starts with a warning
```

**Running a self-editing agent? Lock the config** so it can't rewrite its own
instructions on a live proxy:

```yaml
runtime:
  hot_reload: false
```

**What Mneme does *not* protect against:** memory is retrievable by anything that can
reach the proxy, and the memory DB is a plain SQLite file. Don't put secrets in
conversations you intend to archive. Provenance grading and the similarity floor reduce
the risk of a bad memory being treated as fact — they aren't a security boundary.

## Model selection

Mneme runs three models against one shared memory store:

- **Chat** — answers you. Any OpenAI-compatible model; pick for capability.
- **Embedder** — turns text into vectors. Must be **1024-dim**; it also fixes the
  similarity scale (see `inject_min_similarity` below).
- **Labeler** — tags topics on every turn. Must be **non-thinking** (a thinking labeler
  stalls the pipeline).

**Thinking models** (Gemma 3/4, etc.) are handled for you: reasoning is **off by
default** (`think: false`), so the model answers directly. If a model hangs or times
out, suspect the proxy's config before the model. Opt back in with
`MNEME_REASONING_ENABLED=1`.

**Model templates** package known-good settings (sampling, thinking mode, output caps,
per-model overrides, and optional corrected Modelfiles) so you select them at setup
instead of rediscovering them. See [`model_templates.yaml`](model_templates.yaml).

## Configuration

Everything lives in one file — `~/mneme/chunks/instances/<port>/mneme.yaml` — plus a few
env vars. Precedence: **env var > config file > default**. The proxy logs a `[CONFIG]`
line at startup showing the final value of every setting.

| Setting | Default | What it does |
|---|---|---|
| `backend.type` / `backend.provider` | `openai` / `openrouter` | which backend + which `providers:` entry |
| `providers.<name>.model` | `deepseek/deepseek-v4-flash` | chat model |
| `providers.<name>.embed_model` | `qwen/qwen3-embedding-8b` | embedding model (1024-dim) |
| `providers.<name>.label_model` | `meta-llama/llama-3.2-3b-instruct` | topic-labeling model (non-thinking) |
| `sampling.temperature` | `0.2` | creativity — lower is more deterministic |
| `retrieval.inject_min_similarity` | `0.45` | **the main knob** — minimum similarity for a memory to be injected (see below) |
| `retrieval.strategy_min_similarity` | `0.40` | second, lower floor for the strategy layer |
| `storage.memory_enabled` | `true` | master switch — `false` disables all memory |
| `storage.inject_enabled` | `true` | `false` = save-only (no auto-injection, but saving + search keep working) |
| `storage.memory_only` | `false` | `true` = turn off the experimental strategy layer (the `memory-only`-branch default) |
| `harness.enabled` | `true` | the agent harness (runs) — `false` to disable it |
| `runtime.hot_reload` | `true` | `false` = lock config/prompts until restart |
| `mcp_servers` | `[]` | MCP servers to connect |

Full reference: [`mneme.yaml.example`](mneme.yaml.example).

**Context budget** — memory, the recent-conversation window, and tool results each get a
bounded share of the context window (`ctx_tokens − completion_reserve −
tool_followup_tokens`), so the model always has room to answer and the window never
overruns.

**Tune `inject_min_similarity` per embedder.** This is the one setting you must not copy
blindly — every embedding model has its own similarity scale. Reference: `qwen3-embedding-8b`
noise ~0.39 → use ~0.45; `voyage-4-lite` → ~0.62; `snowflake-arctic-embed2` → ~0.45.
`strategy_min_similarity` must stay below it.

## Features

- **Core memory** — topic-aware chunking with LLM labeling, FAISS vector search gated by
  an absolute floor, recency-weighted scoring, source tracking, and full-page chunking for
  `fetch_url`. Embeddings self-heal across machines and embedders.
- **Tools** — `search_memory`, `read_file`, `fetch_url` (with JS rendering), `web_search`,
  plus `bash`/`write` (native), each with an on/off flag.
- **MCP client** — install any tool by pointing at an MCP server (stdio or HTTP); tools
  hot-add on config change or `POST /mcp/servers` with no restart.
- **Provenance grading** — the model's sources are graded on *honesty*, not correctness
  ("I don't know" beats fabrication); fabricated citations fail.
- **Images (vision)** — images pass through to vision-capable backends and are remembered
  like articles (content-addressed, GC'd, recalled via `read_image`).
- **Themes** — light/dark/custom, switchable in the UI.
- **Scope & permissions** — set the model's write scope from the file browser; paths
  outside it surface an inline "grant access" affordance, and blocked writes are tracked.
- **Agent harness** — see below.

## Agent harness (runs)

A **run** is a goal that outlives a single chat turn. The harness — not the model — owns
its state: tasks, steps, tool calls, artifacts, checkpoints and an append-only event log
live in `harness.db`. Each step is an ordinary Mneme turn, and only counts as done when
the harness verifies it. Runs support planning, verification checks, pause/resume/retry,
profiles, approvals, skills, scheduled jobs, and controlled self-improvement.

```bash
curl -s localhost:8080/runs -H 'Content-Type: application/json' -d \
  '{"goal": "Find the latest Python release and write it to notes.txt",
    "tasks": ["Find the latest stable Python version", "Write it to notes.txt"],
    "budget": {"max_steps": 10, "max_failures": 2}}'
curl -s localhost:8080/runs/<run_id>          # status, tasks, steps, artifacts
```

Type `/help` in chat for harness commands. Full guide:
[`docs/harness/USER_GUIDE.md`](docs/harness/USER_GUIDE.md).

## Swarms

Mneme includes a declarative agent-workflow engine (`extensions/swarm`): you describe a
workflow in `swarm_config.yaml` — which agents run, what each reads, where it writes, how
it branches, retries and parallelizes — and the orchestrator drives it. Agents
communicate through files on disk (a shared blackboard) rather than by negotiating with
each other. It exists to test one hypothesis, honestly stated: *can several small
locally-runnable models, given memory, tools, roles and a structured workflow, do work
that would normally need one large model?* **That has not been demonstrated
quantitatively yet**, and the system is sensitive to configuration — some combinations
work well, some don't work at all. Tuning is part of using it. See
[`extensions/swarm/README.md`](extensions/swarm/README.md).

## API

| Method | Path | Description |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-compatible chat (memory, tools, grading all happen proxy-side) |
| POST | `/search` | Direct memory retrieval without a generation |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/list`, `/detail/<chunk_id>` | Browse stored memory |
| POST | `/memory/chunks/<id>/remove`, `/memory/chunks/<id>/bad` | Curation — set the removed/bad flags (reversible) |
| GET | `/memory/log` | Audit log of curation actions |
| GET/POST | `/instructions` | Read/edit the injected prompts |
| POST | `/runs`, GET `/runs`, `/runs/<id>` | Create / list / inspect agent runs |
| GET | `/models`, `/v1/models` | List models |
| GET | `/mcp/servers`, POST/DELETE `/mcp/servers[/<name>]` | MCP server management |

Plus the web pages above (`/`, `/chat`, `/memory`, `/templates`, `/ollama`, `/runs`,
`/strategies`, `/extensions`) and their JSON endpoints. The full list lives in
[`AGENTS.md`](AGENTS.md).

## Extensions, gateway, multiple instances

- **Extensions** — separate programs that talk to the proxy over HTTP (never import it).
  `extensions/` ships the `swarm`, a Pi coding-agent bridge, and terminal/Telegram
  gateways. An optional `extension.yaml` manifest makes an extension manageable from the
  `/extensions` page. See [`extensions/README.md`](extensions/README.md).
- **Gateway** — `proxy/gateway.py` is an optional reverse proxy that fronts every instance
  on one port and is the future auth choke-point (`MNEME_GATEWAY_TOKEN`).
- **Multiple instances** — several proxies can share one memory DB, each on its own port
  with its own chat model. Rule: **same embedder everywhere** (vectors live in one
  semantic space), and cross-machine sharing needs a real shared filesystem (NFSv4+).

## Testing

Deterministic tests (no live model or network — a scripted model stands in, against the
real SQLite/FAISS paths):

```bash
python3 tests/test_tool_loop.py          # 88 tests: tool loop, retrieval, provenance
for t in tests/test_*.py; do python3 "$t"; done   # full suite (~386 tests)
```

## Repository layout

| Path | What it is |
|---|---|
| `proxy/` | The proxy — `mneme_proxy.py` + the `mneme/` modules (tools, curation, templates, chat commands) |
| `scripts/` | `install.sh` (Linux) + `install_mac.sh` (macOS, untested), `mneme_setup.py` (wizard), `mneme_connect.py` (SSH tunnel) |
| `extensions/` | HTTP clients: `swarm/`, `pi/`, `gateways/` |
| `docs/` | Harness guide/spec, model notes, provenance + strategy specs |
| `tests/` | The deterministic suite |
| `AGENTS.md` | Instructions *for AI coding agents* — give it to any agent you point at this repo |
| `mneme.yaml.example` | Annotated reference config — every setting explained |
| `model_templates.yaml` | Shipped model templates |

## Branches

- `agent-harness` — **the full build (this branch, and the default on GitHub)**. Memory
  retrieval, provenance grading, and the full toolset are always on. Two things
  additionally default **on** here: the **strategy/self-improving layer**
  (`storage.memory_only: false`) and the **agent harness** (`harness.enabled: true` —
  durable multi-step runs, the runs dashboard, profiles, approvals, skills).
- `memory-only` — **the release branch**. Memory, provenance grading, and the full
  toolset, with the experimental strategy layer off by default (`storage.memory_only:
  true`). It has no agent harness — that subsystem lives only on `agent-harness`. Start
  here for a conservative, memory-only setup.

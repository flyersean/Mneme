# Mneme — persistent memory and tools for AI agents

> ⚠️ **Experimental / pre-1.0 — actively developed.**
>
> **Written with AI, reviewed by a human.** Mneme was built iteratively with AI
> coding assistants rather than typed line-by-line.

**Mneme is a persistent memory and tool proxy for AI agents.** It sits between an
OpenAI-compatible client and a model backend, archives conversations into
searchable memory, retrieves the relevant parts back into later turns, and gives
the model a persistent tool layer.

It runs against **local Ollama models** or **any hosted OpenAI-compatible
provider**. Point Pi, Open WebUI, a script, or any OpenAI-compatible app at it.

```text
        any OpenAI-compatible client
                     │
                     ▼
        ┌────────────────────────┐
        │      Mneme Proxy       │
        │                        │
        │  memory    tools       │
        │  provenance  MCP       │
        │  context budget        │
        └───────┬────────┬───────┘
                │        │
                ▼        ▼
          SQLite+FAISS   model backend
          (memory DB)    Ollama / hosted API
```

## Why Mneme is not just a chat-history database

Three design decisions separate it from "store the transcript, paste it back":

- **Retrieval has a confidence floor.** Mneme does not inject the nearest memory
  just because it is the nearest. A chunk must clear an absolute similarity
  threshold to be used at all. Irrelevant context is worse than none.

- **Memory carries provenance, not just content.** Every chunk is tagged as
  observed (user input, a fetched page, a tool result) or as a claim (model
  output). Model-generated content is re-injected with an `[UNVERIFIED]` marker
  so the model cannot quietly re-assert its own earlier output as established
  fact — which is how a hallucination, once saved, becomes permanent.

- **Bad memories are correctable.** Any chunk can be marked false and later
  restored, with every action in an audit log. A model can *propose* a chunk is
  wrong; only you can confirm it. This exists because "the model agreed with
  itself earlier" is the main way a memory system poisons itself.

See [How memory works](#how-memory-works) for the mechanism.

## Quick start

Three scripts take you from a fresh machine to a running proxy. Run the first two
on the machine that **hosts** the proxy (a laptop or a GPU pod); run the third on
your **laptop** to reach a remote proxy.

| Script | Where it runs | What it does |
|---|---|---|
| `install.sh` | the host | Installs system + Python deps, Ollama, browser engines and the Hound MCP server, then clones the repo into `~/mneme/repo`. Idempotent — safe to re-run. See below for exactly what it touches. |
| `mneme_setup.py` | the host | Interactive setup wizard: pick the backend (OpenRouter or Ollama), the chat/embed/label models, the context window, optional Pi, and the port. Writes the config + start script, launches the proxy, and health-checks it. |
| `mneme_connect.py` | your laptop | (Only for a remote pod.) Opens a stay-alive SSH tunnel and prints the local URLs to open in your browser. |

### 1. Install (on the host)

```bash
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/install.sh | MNEME_BRANCH=main bash
```

**What the installer touches.** It is more than a `pip install`, so here is the
full list before you run it:

- **Python packages** (system-wide, `--break-system-packages`): flask, flask-cors,
  faiss-cpu, numpy, requests, pyyaml, ddgs, mcp, playwright, patchright,
  `hound-mcp[all]`, and `zstandard` if needed.
- **Ollama** — installed and started, even if you plan to use a hosted backend.
  On a systemd host it also writes a drop-in at
  `/etc/systemd/system/ollama.service.d/10-mneme.conf` pinning keep-alive.
- **Two Chromium builds** (~150 MB each) for the browser-based web tools, plus the
  system libraries they need.
- **Removes conflicting apt packages** (`python3-flask`, `python3-werkzeug`,
  `python3-blinker`) that pin versions incompatible with the pip installs.

**Root vs non-root.** Privileged steps — the apt removal, the systemd drop-in, and
a `/usr/local/bin` helper — are **skipped automatically if you are not root**, with
a message saying what was skipped and how to apply it later. The proxy itself needs
none of them. So a normal laptop install without sudo works; you just lose the
keep-alive pinning and the apt cleanup.

**Prefer to read before running?** Use the git route instead of the pipe:

```bash
git clone https://github.com/flyersean/Mneme.git
cd Mneme && ./scripts/install.sh
```

### 2. Configure (on the host)

```bash
curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/mneme_setup.py && MNEME_BRANCH=main python3 /tmp/setup.py
```

### 3. Connect (on your laptop — only for a remote pod)

```bash
curl -sSL -o /tmp/mneme_connect.py https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/mneme_connect.py && python3 /tmp/mneme_connect.py
```

Once running, the proxy is at `http://localhost:8080/` — chat UI at `/`, OpenAI-compatible API at `/v1`. Skip step 3 if you're running everything on one machine.

### What you get

- **Chat UI** — open `http://localhost:8080/` in a browser. It's a simple light-theme web
  client over the same `/v1/chat/completions` API. Type a message and the proxy runs the
  full tool loop behind it (memory search, `bash`/`write`, web search, file access).
- **Prompt editor** — open `http://localhost:8080/instructions` to see every prompt Mneme
  injects, in the order it fires. Edit any of them inline (Save writes straight back to the
  file), or click "open file" for the raw text. Edits apply to the next message.

### Live edits (no restart)

- **Prompts** are re-read every turn — edit one (or use the `/instructions` editor) and the
  change applies to the next message.
- **Generation settings** (`sampling.*` / `models.*`) are re-read when `mneme.yaml` changes,
  so you can tune temperature/top_p/max_tokens on a running proxy. Structural settings
  (backend, port, db path) still need a restart.
- **Swarm** reads its input folders fresh every step, re-reads `swarm_config.yaml` whenever
  it changes (so edits to steps, prompts, and options apply on the next step), and drives
  Mneme proxies over HTTP — whose prompts and settings hot-reload the same way.
- **MCP tools** are reconciled from the `mcp_servers:` block whenever `mneme.yaml` changes —
  and via `POST` / `DELETE /mcp/servers` at runtime — so you can add or remove a tool
  server with no restart.
- **Lock everything** — set `runtime.hot_reload: false` (a setup-time flag) to freeze
  config, prompts, and `swarm_config.yaml`: changes then require a restart. See
  "Full control" below.

---

## How it fits together

**Memory-only by default, full-featured underneath.** This branch (`main`) ships with the *strategy / self-improving layer* turned **off by default** — the one switch is `storage.memory_only` in the config (env-var equivalent `MNEME_MEMORY_ONLY`). It limits which features are *on by default*, not which features exist: memory retrieval, provenance grading, and the full tool loop always run, and the off-by-default features are **experimental**, not dead. They're developed and tested on the `unified_mneme` branch and merged back into `main` as they stabilize. Set `memory_only: false` (or `MNEME_MEMORY_ONLY=0`) to turn them on here — the config key is **live-reloadable** (edit it and the next request picks it up, no restart). See "Experimental features" below.

**Backend-agnostic.** One config file chooses the backend — local [Ollama](https://ollama.com) or any OpenAI-compatible provider (OpenRouter, OpenAI, DeepSeek, Groq, Together, Mistral, ...). No GPU or model downloads are required when running against a hosted provider.

**Three models, one DB.** Mneme runs three models against a single shared memory store (one SQLite DB + one FAISS index):

- The **chat** model answers you.
- The **embedder** turns text into vectors.
- The **labeler** tags topics.

All three read/write the same DB, so memory is shared and consistent. The chat model is fixed by config — the request's `model` field is not used to route to a different chat model.

```text
Any OpenAI client ──▶ Mneme Proxy (:8080) ──▶ your model backend (Ollama or OpenAI-compatible)
                           │
                           └──▶ SQLite + FAISS memory (injected back into the prompt)
```

Everything lives under one directory, `~/mneme/`:

```text
~/mneme/
  repo/      this repository (git clone)
  env        your OpenRouter API key (chmod 600; only for the hosted backend)
  chunks/    memory DB (mneme.db), per-instance config (instances/<port>/mneme.yaml), and editable prompts
```

## Security

**Mneme is designed for local or otherwise trusted environments.** It is a tool
proxy, not a hardened service. Treat it the way you would treat a shell account.

When enabled, the proxy can expose capabilities that are equivalent to giving a
model access to the host:

- **`bash`** — runs shell commands on the Mneme host
- **`write`** — creates and overwrites files
- **`read_file` / `read_image`** — reads any file the proxy process can read
- **MCP servers** — each one you configure adds its own tools and privileges
- **HTTP endpoints** — including a prompt editor that writes to disk
- **Model-directed tool use** — the model chooses which tools to call

None of this is sandboxed. A tool call the model makes is a real tool call.

**Do not expose the proxy directly to the public internet.** There is no
authentication on the HTTP API. **The proxy binds to `127.0.0.1` (localhost only)
by default** — that is the safe default and you should keep it. If you need remote
access, put it behind an SSH tunnel (`scripts/mneme_connect.py` does this for you),
a VPN, or a reverse proxy that handles auth.

Binding wider is possible but deliberate — you are opting out of the safety net:

```bash
MNEME_BIND=0.0.0.0        # reachable from the network; starts with a warning
```

Only do that on a machine where the network is already trusted and firewalled. The
proxy logs a warning at startup when the bind is not localhost.

**Running a self-editing agent? Lock the config.** If you point an agent at a
task where it can modify its own files — coding, studying, anything that writes
to the repo — set:

```yaml
runtime:
  hot_reload: false
```

This freezes the config, the system prompts, and `swarm_config.yaml` so runtime
edits cannot change the proxy's behaviour until it is restarted. Without it, an
agent that can write files can rewrite the instructions it runs under. The lock
is read once at startup and never re-read, so it cannot be turned back on by a
running proxy.

**What Mneme does *not* protect against.** Memory is retrievable by anything that
can reach the proxy, and the memory DB is a plain SQLite file. Do not put secrets
in conversations you intend to archive. Injection defences (provenance grading,
the similarity floor, retraction) reduce the risk of a bad memory being treated
as fact — they are not a security boundary.

## Model selection

Mneme runs three models — chat, embedder, labeler — and each has one hard requirement:

- **Chat model** — answers you. Any OpenAI-compatible model works; pick it for capability.
- **Embedder** — must be **1024-dim**. It also fixes the similarity scale, so `inject_min_similarity` is embedder-dependent (see "Tune `inject_min_similarity` per embedder" below).
- **Labeler** — a small, fast model that tags topics on *every* turn. It must be **non-thinking** (a thinking labeler stalls the pipeline).

### Thinking models

Recent models (Gemma 3/4 and similar) are "thinking" models that emit a long hidden
reasoning phase before answering — slower, and if misconfigured they can run away and time
out. Mneme handles them for you:

- **Thinking is off by default** — the proxy sends `think: false`, so the model answers
  directly instead of grinding through a hidden reasoning chain.
- The **labeler must be non-thinking** — it runs on every single turn.
- If a thinking model is slow or times out, **suspect the proxy's config before the model.**
  A partial or mis-loaded config can silently leave reasoning on (the exact failure that once
  made a Gemma proxy hang). Mneme guards against it with an atomic config write and a
  retry-on-partial-load at startup.
- To opt back into reasoning, set `MNEME_REASONING_ENABLED=1`.

In short: if a model seems "broken" or hangs, check the proxy's settings and generation
parameters first — it's almost always a settings issue, not the model.

## Features

### Core memory

- Topic-aware chunking with automatic LLM labeling.
- FAISS vector search gated by an absolute `inject_min_similarity` floor.
- Recency-weighted scoring (cycle-based, not wall-clock).
- Source tracking (user, model, tool:*, page:*, document:*).
- Full-page chunking: `fetch_url` stages the ENTIRE fetched page as fine-grained `page:<domain>` chunks (paragraph-aligned, capped below `max_chunk_size`), so a huge wiki article is fully retrievable later via `search_memory` even though the model only ever sees a bounded head+tail window.
- Embedding reliability: startup health check probes the embedder and fails loud on a dim mismatch; a failed embed is stored `pending_embed` and re-embedded on next startup (no silent dead vectors).

### Full control

Every proxy instance is a set of independent toggles, so you can set one up exactly how you want:

- **Memory** — `storage.memory_enabled: false` turns off all memory (no retrieval, no injection, no staging; `search_memory` auto-hides) while keeping the proxy and tools running.
- **Tools** — each built-in tool (`search_memory`, `list_tools`, `read_tool`, `read_file`, `fetch_url`, `web_search`, plus `bash`/`write` via `tools.native`) has an on/off flag.
- **Backend** — `backend.type` + `providers:` (Ollama or any OpenAI-compatible provider); in a swarm, `backend: ollama` runs a raw model with no memory at all.
- **Live-edit lock** — `runtime.hot_reload: false` freezes config + prompts + `swarm_config.yaml`: any change takes effect only after a restart. It's read once at startup and never re-read (setup-time only), so a coding/studying agent that edits its own files can't re-enable live edits on a running proxy.

### MCP tools (install any tool)

The proxy is an **MCP client**, so "install a tool and it just works" holds for any language/runtime — MCP is language-agnostic JSON-RPC. Point the proxy at an MCP server (web, filesystem, database, GitHub, …) and its tools surface to the model like built-in tools.

```yaml
# stdio — the proxy spawns `command args` and owns its lifecycle
mcp_servers:
  - name: filesystem
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /workspace]
  # Hound — free, keyless, local web access (fetch/search/crawl/screenshot/PDF):
  #   pip install hound-mcp[all] && patchright install chromium
  - name: hound
    command: hound
  # HTTP — connect to an already-running streamable-HTTP server
  - name: my-tool
    url: http://localhost:9001/mcp
```

- **Hot-add (no restart):** edit the `mcp_servers:` block — it's reconciled on `mneme.yaml` mtime, like the storage flags. Or `POST /mcp/servers` / `DELETE /mcp/servers/<name>` at runtime (in-memory; write to config to persist). `GET /mcp/servers` lists status.
- **Name collisions:** an MCP tool whose name matches a built-in tool is shadowed by the built-in.
- **A broken server degrades gracefully:** it's logged and skipped, never crashes the proxy, and its tools just don't appear.
- The setup wizard prompts to add MCP servers during install.

### Images (vision)

A vision-capable backend sees images through the proxy, and images are remembered like articles are.

- **Passthrough:** an image in a message reaches the model in each backend's native form — OpenAI/OpenRouter gets the `image_url` array untouched, Ollama gets a separate `images: [base64]` field. (Before this, the proxy flattened content to text and the model saw only `[IMAGE: url]`.)
- **Remembered, not lost:** on archive, an image is saved once to `chunk_dir/images/<sha256>.<ext>` (content-addressed — the same bytes are never saved twice, so re-processing a saved image adds a memory record, not another copy). The chunk stores the path + MIME beside the turn's text; the model's written analysis of the image is the searchable index, as with any other turn.
- **Collected, not orphaned:** an image whose hash is referenced by no chunk (an ingest whose turn never archived, e.g. a crashed turn) is removed by a background sweep — on startup and periodically (`MNEME_IMAGE_GC_INTERVAL`, default 30 min) — with a grace period (`MNEME_IMAGE_GC_GRACE`, default 1 h) so a just-ingested image mid-archive is never deleted.
- **Recall:** an injected chunk that has an image shows a `[IMAGE: <path> — read_image "<hash>" to view]` note, and the `read_image` tool returns the actual image so the model can look at it again.
- **Token accounting:** images are charged their real cost (~85 low-res to ~1440 high-res) in the context budget instead of ~12 placeholder characters.

### Provenance grading

*On — this is memory quality, not learning.*

- Provenance grading: the model tags its sources (`[source: X]` / `[guess]`) and is graded on *honesty*, not answer-correctness — "I don't know" beats fabrication.
- Honest-terminal detection: correct-but-uncitable answers — `undefined`, `market price`, "I don't know", "no such X", a false-premise correction, a clarification — are graded "pass", not fail. The judge misreads them as failures, so they're short-circuited before the judge.
- Trace cross-check: any cited `[source: mem_XXX]` or URL is verified against what the model actually had this turn (injected chunks + search results + the server-side tool trace), with host normalization so `shaws-wharf.com` matches `https://www.shaws-wharf.com/menu`. A fabricated citation fails.
- `[source: input]` tag: facts read from the input file/context handed to the model this turn (e.g. a swarm `read_dir` file) are cited `[source: input]` or `[source: input:<filename>]`. Honest but *unverified* — the file was handed over unchecked, so "from the file" is not "confirmed true". The cross-check verifies a named file actually appears in the input.
- Trust tier: every chunk is tagged `verified` (user/page/tool — observed) or `unverified` (model-generated — a claim, not an observation) at ingest. Unverified chunks are re-injected with an `[UNVERIFIED]` marker so the model doesn't re-assert its own past output as fact — this closes the self-reinforcement loop where a hallucination, once cited, kept coming back as "memory". Swarm `read_dir` input is auto-detected (its `--- <path> ---` headers) and staged as `input` (unverified), so even a large file split into its own chunk can't enter memory as fact.

### Experimental features (off by default)

*These exist and are under active development, but they're **off by default** on this
branch (`memory_only: true`). They are not dead code — they're developed and tested
on the `unified_mneme` branch and merged back into `main` as they stabilize. Set
`memory_only: false` to enable them here.*

- **Strategy / self-improving layer** — strategy learning from tool traces, novel-procedure detection, failure extraction, and belief evolution. Strategies are linked to the source chunk that produced them, and retrieval keys on that linkage (no hand-maintained problem-type taxonomy). A D/F turn distills one imperative directive to prevent recurrence — filtered through a junk-directive guard *and* skipped entirely for honest-terminal answers; SUCCESS strategies save only on a recovery (≥2 consecutive tool failures then success).
- **Capability-edge tracking & overcome** — records a competence edge per problem type; three consecutive tool failures flag it, and the next similar task is routed into **overcome mode** (hard-stop: build a tool, reuse a saved one, or — when the build budget is spent — answer honestly and surface the edge) instead of grinding or silently giving up. A built tool is saved and the edge can be cleared.
- **Thinking & learning modes** — `/mode/think` (novelty: generate a baseline, forbid its modal features, diverge, and grade novelty objectively via embedding distance + pairwise judge — not self-report) and `/mode/learn` (parameter cycling + strategy extraction).

## Usage and connecting clients

### Run the proxy

- **Start / restart the proxy:** `~/mneme/chunks/instances/<port>/start_proxy.sh` (written by setup).
- **Logs:** `tail -f ~/mneme/chunks/instances/<port>/proxy-<port>.log` (append mode — survives restarts and re-runs; cap it with `logging.max_entries`).

### Web interfaces and service URLs

Once the proxy is running with any backend:

| URL | What it is |
|---|---|
| `http://localhost:8080/` (or `/chat`) | **Chat UI** — a light-theme web client over `/v1/chat/completions`. Full native toolset (memory search, built-tool registry, `bash`/`write`). |
| `http://localhost:8080/instructions` | **Prompt editor** — every injected prompt, in the order it fires during a conversation. Read them, edit them inline (Save writes straight back to the file), or click "open file" for the raw text. |
| `http://localhost:8080/v1` | OpenAI-compatible API base (for Pi, Hermes, or any client). |
| `http://localhost:8080/health` | Health check (`curl http://localhost:8080/health`). |

### Pi terminal assistant

Pi is offered during setup. To install or run it by hand:

1. Install it. Pi needs Node.js 22+.

   ```bash
   npm install -g @earendil-works/pi-coding-agent
   ```

2. Point Pi at Mneme. Setup writes `~/.pi/agent/models.json` for you using this instance's actual port, but the shape is:

   ```json
   {
     "providers": {
       "mneme": {
         "baseUrl": "http://localhost:<port>/v1",
         "api": "openai-completions",
         "apiKey": "none",
         "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
         "models": [{ "id": "text-mneme:64k", "name": "Mneme", "contextWindow": 64000, "reasoning": false }]
       }
     }
   }
   ```

3. Run Pi. Setup prints the exact command, using the paths it downloaded to:

   ```bash
   pi --provider mneme --model text-mneme:64k \
     --extension ~/.pi/mneme-extensions/mneme-search-tool.ts \
     --extension ~/.pi/mneme-extensions/mneme-web-tools.ts
   ```

   If you are running from a git clone instead, the same files are in the repo:
   `extensions/pi/mneme-search-tool.ts` and `mneme-web-tools.ts`.

### Connect any other OpenAI client

Anything OpenAI-compatible (Hermes, Open WebUI, etc.) just needs the base URL:

- `http://localhost:8080/v1`

## How memory works

Every turn is staged. When the turn is saved, the proxy:

1. **Labels** message groups into topics with the labeler model.
2. **Embeds** each group with the embedder (1024-dim).
3. **Stores** it in SQLite (`chunks` table) + a FAISS `IndexFlatIP`.
4. On the next request, embeds the query, finds FAISS nearest neighbours, and injects the chunks whose similarity clears `inject_min_similarity`.

The retrieval gate is an **absolute similarity floor**, not a relative one. If nothing in memory scores above `inject_min_similarity`, nothing is injected, avoiding "best guess" noise.

A substring keyword fallback exists but is **off by default** (`keyword_fallback: false`) because it has no semantic score and pollutes context, such as "tool" matching an unrelated "Paramotor Tool" memory.

Retrieval is **two-floor**: a chunk scoring in `[strategy_min_similarity, inject_min_similarity)` isn't injected as memory, but any **strategy linked to that chunk** still is. This is how a learned approach ("verify the menu price on the restaurant's own site") generalizes to a *different* restaurant whose chunk sits just under the memory floor. Strategy retrieval is part of the experimental self-improving layer, so the second floor is inactive in the default memory-only build unless `memory_only: false`.

Retrieval is **topic-switch aware**. When the current turn diverges from the last few turns (a topic switch), injection is hardened for a short grace window — a raised `novel_inject_floor` and no sibling expansion — so a dominant stale topic in a large DB can't steer the model back. `max_per_topic` further caps how many chunks any single `topic_label` may contribute. These four knobs live under `retrieval:` in `mneme.yaml.example` and default to sensible values (set any to `0` to disable).

Memory is **portable** across machines and even across 1024-dim embedders. On startup, the proxy re-embeds any chunk whose stored `embed_model` doesn't match the current one, so you can `scp` the `.db` from a pod to a laptop and it self-heals. Text, grades, and strategies survive; only vectors regenerate.

## Agent workflows & swarms

Mneme includes a **declarative agent workflow engine** (`extensions/swarm`).

Rather than writing a Python driver for every multi-agent task, you describe the
workflow in `swarm_config.yaml`: which agents run, what each one reads, where it
writes its result, how the flow branches, when it retries, and what runs in
parallel.

The result is a small workflow language for repeatable agent pipelines — with the
orchestration *outside* the models rather than negotiated between them.

```text
                      swarm_config.yaml
                             │
                             ▼
                    ┌─────────────────┐
                    │   Orchestrator  │
                    │                 │
                    │ flow / branches │
                    │ retries         │
                    │ file state      │
                    │ parallel steps  │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
           Mneme           Mneme          Ollama
            agent          agent           model
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    filesystem state
                     / shared board
```

### Agents communicate through artifacts, not conversation

The workflow uses the filesystem as a shared blackboard. One agent writes a file,
the next reads it, and a later stage can consume the whole directory.

```text
input/
   │
   ▼
┌──────────────────────────┐
│ parallel critics         │
│   structure.txt          │
│   prose.txt              │
│   factual.txt            │
└────────────┬─────────────┘
             ▼
         synthesis
             ▼
           draft
             ▼
          review
         /      \
    approve     revise
       │           │
       ▼           └──────► review
    publish
```

This is the design choice that matters most: **the workflow defines the
structure; the models perform the cognitive steps.** An agent doesn't need to
understand the whole pipeline — it gets "here is the material, you are the
critic" and writes to a path. Intermediate work stays on disk, so it is visible
while the run is happening and after it finishes.

It is easier to reason about than a swarm of agents negotiating with each other,
and it fails in ways you can see.

### What a workflow can express

- sequential model steps, and `goto` loops
- branching on model output or on filesystem state
- retries on transient model failure, and per-step `delay` pacing
- **per-step generation settings** — `options:` overrides temperature/top_p/top_k
  and backend-specific keys for that one call, without touching the proxy config
- file and directory primitives, including `swap_dir` (below)
- cycle throttling (`every`), `skip_if_empty`, and action-only steps that do not
  spend a model call

Granularity is deliberate: a step that freezes a directory, moves a file, or
checks state costs nothing, because it never touches a model.

### Atomic workflow snapshots and parallel swarms

Two primitives worth calling out specifically.

**`swap_dir`** gives an iterative workflow a transaction-like boundary:

```text
input/
   │
   │ swap_dir
   ▼
input.active/     ← frozen snapshot: what this iteration works on
input/            ← fresh: new work can accumulate for the next iteration
```

Agents in the current cycle see a stable input set, while new material keeps
arriving for the next one. This sounds small and removes a genuinely awkward
problem in iterative agent systems: *what exactly is the input to this pass?*

**`parallel:`** (in `swarm_p_orchestrator.py`) is a map → reduce for agents — fan
out independent work, then let the next step read the whole output directory and
combine the results:

```yaml
- parallel:
    - name: structure
      backend: mneme
      port: 8080
      read_dir: input.active
      write_dir: pass1/structure.txt
    - name: prose
      backend: ollama
      model: qwen2.5:14b
      read_dir: input.active
      write_dir: pass1/prose.txt
```

Worth being precise about "parallel", because it depends on the backend. Against
a **hosted** provider, requests genuinely run concurrently — with one caveat,
rate limits: a wide fan-out can trip them, so keep hosted parallel blocks modest.
On a **single-GPU Ollama** box, different models serialize anyway (VRAM forces a
model swap per step), so fan-out only helps for the *same* model. The thread pool
doesn't pretend threads equal GPU parallelism.

### Mneme is optional

The orchestrator talks to Mneme over `/v1/chat/completions`. It does not import
the proxy or depend on its internals. So a single workflow can mix:

- Mneme-backed agents (memory + tools)
- raw Ollama models (`backend: ollama`, no memory)
- different models for different roles
- several Mneme instances sharing one memory DB
- hosted and local inference in the same run

```text
          ┌──────────────┐
          │  Researcher  │
          │    Mneme     │
          └──────┬───────┘
                 │
          research/*.txt
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
   Critic A             Critic B
    Mneme                Ollama
   (memory)            (no memory)
       │                   │
       └─────────┬─────────┘
                 ▼
             Synthesizer
               Mneme
                 │
                 ▼
              final/
```

### Why the swarm exists

This is not a general-purpose distributed workflow platform, and it isn't trying
to be. It exists to test a specific idea.

**The hypothesis:** can a collection of relatively small, locally runnable
models — given persistent memory, tools, specialized roles, and a structured
workflow — complete work that would normally require one much larger model?

The approach is to move work that would otherwise have to happen *inside* a
large model into the surrounding system:

```text
        large-model approach          Mneme approach

        ┌──────────────────┐          ┌──────────────────┐
        │                  │          │    small LLM     │
        │    large LLM     │          │  local / hosted  │
        │                  │          └────────┬─────────┘
        │  reasoning       │                   │
        │  memory          │      ┌────────────┼────────────┐
        │  tools           │      ▼            ▼            ▼
        │  planning        │   memory       tools       workflow
        │  context         │      │            │            │
        └──────────────────┘      └────────────┼────────────┘
                                               ▼
                                        other agents
```

The workflow engine is the control layer for this. Instead of asking one model to
perform every part of a task, a workflow splits it across specialized calls and
combines the results.

### This is not demonstrated yet

**The hypothesis above has not been shown quantitatively.** Mneme provides the
infrastructure to investigate it; it does not claim the question is settled.

In practice the system is sensitive to configuration, and being honest about that
is more useful than overselling it:

- Model choice, context size, retrieval threshold, generation settings, prompts,
  tool availability, and workflow design all interact.
- Some combinations work well. Some work poorly. **Some model/configuration
  combinations do not work at all.**
- There is no single configuration that works equally well across models and
  tasks. "Model-agnostic" does not mean "model-independent".

If you install Mneme, pair a random 7B with an untuned retrieval threshold, and
get poor results — that is the expected outcome of an untuned configuration, not
necessarily a verdict on the approach. Tuning is currently part of using it.

**Model templates** (see [`model_templates.yaml`](model_templates.yaml)) exist to
reduce that cost: named, known-good settings for specific models, selected during
setup, so you don't start from scratch. They are a starting point, not a
guarantee.

Some open questions this exists to ask:

- Can several small specialized models outperform one small general-purpose model?
- How much does persistent memory actually improve long-running work?
- Which tasks benefit from parallel specialists, and which don't?
- When is a large model genuinely necessary?
- Can tools compensate for a capability a model lacks?

The swarm is itself an **extension** — a self-contained program that talks to Mneme
over HTTP and shares no code with the proxy. It doubles as the reference example for
writing your own; see [Extensions](#extensions) for the integration contract and what
else the API exposes.

Full reference: [`extensions/swarm/README.md`](extensions/swarm/README.md) for a
worked example, [`extensions/swarm/SWARM_REFERENCE.md`](extensions/swarm/SWARM_REFERENCE.md)
for the field-by-field spec.

## Configuration

Everything is in one file — `$MNEME_CHUNK_DIR/mneme.yaml` (default `~/mneme/chunks/instances/<port>/mneme.yaml`) — plus a few env vars.

Settings are resolved in this order:

**env var > config file > built-in default**

The proxy logs a `[CONFIG]` line at startup showing the final value of every setting, so a typo or an overriding env var is visible, not silent.

The knobs you'll actually touch are listed below. See `mneme.yaml.example` for full comments.

| Setting | Default | What it does |
|---|---|---|
| `backend.type` / `backend.provider` | `openai` / `openrouter` | which backend + which `providers:` entry to use |
| `providers.<name>.model` | `deepseek/deepseek-v4-flash` | main chat model |
| `providers.<name>.embed_model` | `voyageai/voyage-4-lite` | embedding model (must be 1024-dim) |
| `providers.<name>.label_model` | `meta-llama/llama-3.2-3b-instruct` | topic-labeling model (must be non-thinking) |
| `sampling.temperature` | `0.2` | creativity — lower is more deterministic |
| `retrieval.inject_min_similarity` | `0.45` | **the main knob** — minimum cosine similarity for a memory to be injected. Below it, inject *nothing*. Raise = fewer/higher-confidence; lower = more recall. **Embedder-dependent** — see the note below. |
| `retrieval.strategy_min_similarity` | `0.40` | second, lower floor — chunks in `[strategy_min, inject_min)` don't inject as memory, but their **linked strategies** still do (a learned approach generalizes to same-concept queries just under the memory floor). Must stay below `inject_min_similarity`; embedder-dependent too. |
| `retrieval.max_injected_tokens` | `8000` | token budget for memory stuffed into the prompt |
| `sampling.ctx_tokens` | `65536` | the model's context window (`num_ctx`) — must match the model's actual capability. The wizard's "64K" preset writes `64000`, leaving headroom |
| `sampling.completion_reserve` | `8192` (setup writes `ctx/8`) | tokens held back for the model's reply — never touched by input |
| `caps.tool_followup_tokens` | `10000` (setup writes `ctx/6`) | tokens reserved for tool results inside the loop |
| `storage.memory_enabled` | `true` | master switch — `false` disables ALL memory (no retrieval/injection/staging, `search_memory` off) while keeping tools |
| `tools.search_memory` / `tools.list_tools` / `tools.read_tool` / `tools.read_file` / `tools.fetch_url` / `tools.web_search` | `true` each | per-tool on/off — set any to `false` to hide it from the model |
| `storage.inject_enabled` | `true` | `false` = **save-only**: stop injecting memory, but keep saving + `search_memory` + `/search` (see "Memory modes") |
| `runtime.hot_reload` | `true` | `false` = **lock** config/prompts/swarm_config — changes take effect only after a restart (setup-time only) |
| `logging.max_entries` | unset | log size cap in entries/lines — `0` = logging off, `N` = keep the newest N lines, unset/omit = no limit |
| `mcp_servers` | `[]` | MCP servers to connect (stdio `command`+`args` or HTTP `url`) — see "MCP tools" |

Full reference: [`mneme.yaml.example`](mneme.yaml.example).

### Context budget — no more overruns

The proxy bounds its input against the model's context window with a single derived budget, so the three input consumers can never sum past the window:

- `sampling.ctx_tokens` is the whole window.
- `sampling.completion_reserve` is held back for the reply.
- `caps.tool_followup_tokens` is reserved for tool results in the loop.
- The **recent-context window** (conversation turns) gets the remainder: `ctx_tokens - completion_reserve - tool_followup_tokens`.

Each consumer — injected memory, the recent window, and tool results — is bounded, and they sum below the window, so the model always has room to answer. The window is **token-bounded** (large turns are evicted, not just old ones). When the tool loop needs more room it drops oldest tool results first, then oldest turns; the completion reserve is never touched. The setup wizard derives `completion_reserve` and `tool_followup_tokens` from the context window you pick, so the generated config is already coherent.

### Tune `inject_min_similarity` per embedder

This is the one setting you must NOT copy blindly between deployments. Every embedding model has its own similarity scale, so a threshold that works for one silently drops most relevant memories for another.

Measure yours by embedding a few obviously-relevant and obviously-irrelevant queries and set the floor just above the noise.

Reference scales:

- `voyage-4-lite` noise ~0.48 / relevant ~0.70 → use ~0.62.
- `snowflake-arctic-embed2` noise ~0.32 / relevant ~0.40 → use ~0.45 (the default).

`strategy_min_similarity` must always stay below it.

### Memory-only mode

`storage.memory_only: true` (the default on this branch) turns off the experimental strategy/self-improving layer while keeping memory retrieval, provenance grading, and the full toolset. It is a *default on/off switch*, not a removal — the code stays present and tested.

`storage.memory_only: false` enables the experimental layer; the `unified_mneme` branch ships that way. The config key is **live-reloadable** — edit `mneme.yaml` and the next request picks up the change without restarting the proxy. (The env var `MNEME_MEMORY_ONLY` is the equivalent override, and takes precedence over the config key if you export it manually.)

### Memory modes — the flags + the floor

Three flags and one floor decide what memory does each turn. From most to least drastic:

| Setting | Off / raised | What still works |
|---|---|---|
| `storage.memory_enabled` (master) | `false` = nothing: no injection, no saving, `search_memory` + `/search` off | system prompt + tool loop only |
| `storage.inject_enabled` | `false` = **save-only**: no memory injected, but turns archived AND `search_memory` + `/search` still work | saving, search, tools |
| `storage.memory_only` | `true` = strategy/learning layer off | memory retrieval, grading, tools |
| `retrieval.inject_min_similarity` | raised = fewer memories injected | saving, `/search` (but **also gates `search_memory`**) |

`inject_enabled` vs a high `inject_min_similarity` — the difference matters:

- `inject_enabled: false` is a **hard off for auto-injection only**. Search keeps working, saving keeps working, and no embed + FAISS work is wasted each turn. This is the "swarm" mode: models aren't bombarded with context, but their work is persisted and pullable on demand.
- A high `inject_min_similarity` is a **soft off** (nothing clears the floor), but it *also* silences the `search_memory` tool — because both auto-injection and `search_memory` share the same `route_query` floor — and the proxy still embeds + searches every turn before discarding the results.

So for "don't bombard the models, but keep saving and let them search on demand", use `storage.inject_enabled: false`. All three flags are live-reloadable from `mneme.yaml` (env equivalents `MNEME_MEMORY_ENABLED`, `MNEME_INJECT_ENABLED`, `MNEME_MEMORY_ONLY` take precedence if exported).

### Legacy thresholds

`route_threshold` and `classify_threshold` in the config are legacy:

- `route_threshold` is only used by the `/search` debug endpoint.
- `classify_threshold` is unused.
- Injection is governed by `inject_min_similarity`.

This is called out in the example's comments too.

## Multiple instances — one DB, many models

Mneme can run several proxy instances against one shared memory DB. Each instance listens on its own port (8080, 8082, 8083, …) and runs its own chat model + backend; they all read/write the same memory.

> **Avoid port 8081 on RunPod** — nginx reserves it there, so a proxy on 8081 will fail to bind. Elsewhere it is fine. The setup wizard's port picker probes for a free port and will normally skip it.

To add an instance, run the setup wizard again and point it at the same DB directory. It detects the existing DB and offers **"Add another proxy instance"**. The wizard auto-picks the next free port, asks for the new instance's chat model (and backend), and writes a per-instance start script (`start_proxy_<port>.sh`).

Two hard rules apply:

1. **Same embedder everywhere.** Every instance sharing a DB must use the SAME embedding model. The vectors in one DB live in one semantic space. If instance A embeds with `snowflake-arctic-embed2` (Ollama) and instance B with `voyage-4-lite` (OpenRouter), both are 1024-dim so FAISS won't crash — but similarity across them is garbage. The startup health check flags "different embed model, same dim" but does not prevent it. The wizard locks the embedder (and labeler) to the first setup's choice; don't change them on a shared DB.

2. **Same machine is rock solid; cross-machine needs a real shared filesystem.** On one machine, instances share the DB with plain file locking (SQLite WAL + fcntl on the FAISS index). Across machines, the DB directory must live on shared storage (NFS/S3-mount), and the fcntl lock is only reliable on NFSv4 — on NFSv3 or a plain S3 mount, concurrent writes aren't safely serialized. So "one instance on RunPod + one on an Ollama pod" needs a proper shared filesystem, not just network reachability.

## API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Debug search: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/` / `/chat` | Built-in chat UI |
| GET | `/instructions` | Prompt reference + editor (the injected prompts) |
| GET | `/list` | List all chunks with metadata |
| GET/POST | `/capabilities` | *(experimental)* List capability-edge records / flag or clear |
| POST | `/mode/think` | *(experimental)* Novelty thinking mode (escape mode collapse) |
| POST | `/mode/learn` | *(experimental)* Learning mode (parameter cycling + strategy extraction) |
| GET/POST | `/preferences` | Read / set user preferences |
| GET | `/mcp/servers` | List connected MCP servers + their tools and status |
| POST | `/mcp/servers` | Add (or replace) an MCP server at runtime — `{"name", "command"?, "args"?, "env"?, "url"?}` |
| DELETE | `/mcp/servers/<name>` | Remove an MCP server at runtime |
| POST | `/search` | Direct memory retrieval without a generation — `{"query", "top_k"?}` |
| GET | `/detail/<chunk_id>` | Full detail for one chunk (messages, grade, topics, provenance) |
| GET | `/models`, `/v1/models` | List available models |
| POST | `/memory/retract`, `/memory/restore` | Mark a chunk false / undo — `{"chunk_id", "reason"?}` |
| GET | `/memory/proposals` | Chunks the model proposed as wrong, awaiting review |
| POST | `/memory/proposals/<chunk_id>/confirm` or `/deny` | Act on a proposal |
| GET | `/memory/log` | Audit log of every curation action |

## Testing

Run the deterministic regression tests with:

```bash
python3 tests/test_tool_loop.py
```

The whole suite (all files) takes a couple of minutes:

```bash
for t in tests/test_*.py; do echo "--- $t"; python3 "$t"; done
```

The installer installs dependencies system-wide (no virtualenv), so `python3` is
the interpreter to use. If you added your own venv, use that interpreter instead.

The tests require no live model or network. A scripted model stands in for the LLM, and the real SQLite/FAISS + retrieval paths run against an in-memory DB.

They cover:

- The tool-calling loop:
  - search → answer
  - search → web_search hand-off
  - search-loop exhaustion
- The injection gate (`inject_min_similarity` floor, keyword fallback off).
- The step-back ladder.
- The capability-edge → overcome routing.
- The injected-prompt materializer.
- Provenance grading:
  - honest-terminal detection
  - source/URL normalization
  - tool-trace URL extraction
  - fabricated-citation fails
- The two-floor retrieval helpers.
- The token-based context budget (recent-window eviction, followup compaction).
- Per-tool disable flags and the `memory_enabled` master switch.
- Memory curation: retraction/restore state, the model-propose vs user-confirm
  split, recurrence tiers, self-confirmation detection, and the decision log.
- Model templates: the merge priority (env > template > config > default) and
  validation that rejects unknown keys instead of silently ignoring them.
- In-chat commands (`<<SETTINGS>>`, `<<RETRIEVAL>>`), including that the config
  rewrite preserves comments and neighbouring keys.

83 tests in `tests/test_tool_loop.py`.

The full suite is **292 tests** across 18 files. Beyond the tool loop:

| File | Tests | Covers |
| --- | --- | --- |
| `test_tool_loop.py` | 83 | tool loop, retrieval gate, provenance, budgets |
| `test_swarm_orchestrator.py` | 47 | swarm control flow, primitives, per-step options |
| `test_images.py` | 18 | image handling in memory chunks |
| `test_curation.py` | 28 | retraction, recurrence, provenance chains, decision log |
| `test_chatcmd.py` | 27 | `<<SETTINGS>>` / `<<RETRIEVAL>>`, config rewrite safety |
| `test_templates.py` | 26 | model-template merge + validation |
| `test_trust.py` | 15 | provenance/trust grading |
| `test_model_config.py` | 11 | per-model overrides, sampler option mapping |
| `test_logfile.py` | 7 | log routing |
| `test_config_load.py` | 5 | config precedence |
| `test_mcp_client.py` | 5 | MCP connect/list/call/remove (needs `mcp`) |
| `test_generated_config.py` | 4 | every wizard-emitted key is valid |
| `test_swarm_skip_throttle.py` | 4 | swarm `skip_if_empty` / `every` |
| `test_hot_reload_lock.py` | 3 | config/prompt lock |
| `test_mcp_endpoints.py` | 3 | MCP hot-add endpoints |
| `test_swarm_p_orchestrator.py` | 2 | parallel orchestrator |
| `test_db_write_retry.py` | 2 | DB write retry |
| `test_inject_flag.py` | 2 | injection on/off switch |

`tests/test_mcp_client.py` requires the `mcp` package (`pip install mcp`); its
tests are skipped/failed without it.

The live-model capability benchmark (a separate harness that runs a scripted model through capability-edge tasks and scores the outcome) lives on the `unified_mneme` branch — it exercises the experimental layer, not the default memory-only path.

## Architecture

Mneme is a single-file Flask proxy (`proxy/mneme_proxy.py`) with module-level state (FAISS index, SQLite connection, staging buffer) and threaded daemon archival.

The backend is selected by config (`backend.type` + `providers:`):

- `query_model` → chat completions
- `embed` → embeddings
- the labeler → chat completions

All are OpenAI-compatible, so swapping providers is a config edit, not a code change.

Two deliberate runtime details are worth knowing:

- **Prefix-cache-stable context.** The fixed instruction block (system prompt + meta-principles) sits at the front of every request as a byte-stable prefix; all *variable* content (memory, strategies, preferences, tool hints) is appended at the tail, never inserted mid-prefix — so any prefix-caching backend (OpenRouter, Ollama, etc.) can reuse the cached prefix across turns.
- **Serialized writes.** The one SQLite connection is shared by the request thread + two archival workers, so every write+commit pair is guarded by a re-entrant lock — no "cannot commit, no transaction is active" races.

## Extensions

Non-core consumers of Mneme live in `extensions/`. They are **separate programs that
talk to the proxy over HTTP** — not plugins, and not part of the proxy stack. Nothing
in `extensions/` is imported by `proxy/`, and an extension never imports the proxy:
the only connection is the network API.

See [`extensions/README.md`](extensions/README.md) for the full integration guide.

That separation is deliberate, and it is what makes the API the contract:

```text
     ┌────────────────┐   ┌────────────────┐   ┌────────────────┐
     │  Pi extension  │   │  swarm         │   │  your tool     │
     └───────┬────────┘   └───────┬────────┘   └───────┬────────┘
             │                    │                    │
             └────────────────────┼────────────────────┘
                                  │  HTTP
                       ┌──────────┴──────────┐
                       │   Mneme proxy       │
                       │   /v1/chat/...      │
                       │   /search /list     │
                       │   /memory/...       │
                       └─────────────────────┘
```

Because the boundary is HTTP, an extension can be written in any language, can run on
a different machine from the proxy, and cannot break the proxy by changing. Equally,
the proxy can be upgraded or restarted without touching an extension. Multiple
extensions can point at the same proxy, or at different instances sharing one memory
DB.

### What an extension can use

| Surface | Endpoint | Use it for |
| --- | --- | --- |
| Chat | `POST /v1/chat/completions` | Full agent turns — memory retrieval, injection, the tool loop, and grading all happen proxy-side |
| Chat (native) | `POST /api/chat` | Same, in Ollama's native shape |
| Memory search | `POST /search` | Direct retrieval without a model call — `{"query", "top_k"?}` |
| Recent chunks | `GET /list`, `GET /detail/<chunk_id>` | Browsing what memory actually contains |
| Memory curation | `POST /memory/retract`, `/memory/restore` | Marking a stored chunk false, or undoing it |
| Review queue | `GET /memory/proposals`, plus `POST .../confirm` and `.../deny` | Acting on a chunk the model proposed as wrong |
| Audit log | `GET /memory/log` | Every curation action, who did it, and why |
| Prompts | `GET/POST /instructions*` | Reading or editing the system prompts |
| Health / models | `GET /health`, `/models`, `/v1/models` | Discovery, readiness |

The important one is `POST /v1/chat/completions`: an extension gets memory, tools,
provenance, and the tool loop **for free** by sending a normal OpenAI-shaped request.
It does not have to know how any of that works. That is the whole point of putting the
memory layer in a proxy rather than in a library.

### The swarm is the reference example

`extensions/swarm` is a complete, working extension — and the intended template for
writing others. It is the best guide in the repo because it exercises the contract
properly rather than trivially:

- **Zero coupling.** It imports no Mneme code. Its entire integration is a plain HTTP
  POST to `/v1/chat/completions` (see `call_mneme()` in `swarm_orchestrator.py` — that
  method is the only touchpoint with Mneme, and it is ~20 lines).
- **It shows both modes.** `backend: mneme` drives a proxy (memory + tools);
  `backend: ollama` drives a raw model over Ollama's native API. A workflow can mix
  them, which demonstrates that Mneme is optional rather than required.
- **It shows the per-step override pattern.** A step can pass generation settings for
  that one call without touching the proxy's config.
- **It is self-contained.** One config file, two scripts, its own docs.

Read `extensions/swarm/swarm_orchestrator.py` if you want to write your own extension:
find `call_mneme()`, and you have seen the whole integration surface.

**Things an extension can do that the swarm does not**, which are also worth copying
from the endpoint table above: calling `/search` directly for retrieval without a
generation, writing to the memory curation endpoints, or reading `/health` to wait for
a proxy to come up before starting work.

### Pi (`extensions/pi`)

Pi coding-agent tools that let Pi call Mneme's memory and web tools. The setup wizard can
install Pi and point it at Mneme as a provider (see "Pi terminal assistant" above). A
second, much smaller example of the same contract — useful for seeing how little is
required to integrate.

### Swarm (`extensions/swarm`)

The declarative agent workflow engine — see [Agent workflows & swarms](#agent-workflows--swarms)
above for what it is and why it exists. Implementation notes that belong here:

**Two orchestrators.**

- `swarm_orchestrator.py` — the **serial** driver. Runs one step at a time through the flow:
  control flow (`goto` / `if`), folder primitives, pacing (`delay`), retry, and both
  backends (`mneme` and `ollama`).
- `swarm_p_orchestrator.py` — the **parallel** driver. Extends the serial one with a single
  new step form, a `parallel:` block that runs a list of independent sub-steps concurrently
  (a thread pool). Everything else is inherited unchanged, so a config written for the
  serial driver also runs here. See the note on hosted vs single-GPU parallelism above.

**How the config works.**

`swarm_config.yaml` has three top-level keys plus the step list:

| key | meaning |
|---|---|
| `ollama_url` | base URL for `backend: ollama` steps (default `http://localhost:11434`) |
| `timeout` | default per-call timeout in seconds (default 600) |
| `max_steps` | safety cap on total step executions — catches an infinite `goto`/`if` loop (default 0 = no limit) |
| `steps` | the ordered list of steps |

A step calls a model **only** when it has `write_dir`, `append_dir`, or a *string* `if`
(which branches on output). Action-only steps — folder actions, `goto`, a *filesystem-state*
`if`, or `exec` — never call a model, so you can sequence the loop without burning a
generation.

Key step fields:

- `name` — optional label; a jump target and a log tag.
- `backend` — `mneme` (default; needs `port`) or `ollama` (needs `model`).
- `read_dir` — a directory (or a **list** of directories, concatenated into one context
  blob) to read context from. `write_dir` / `append_dir` — where output goes; a path with an
  extension is a full file path, otherwise it's a directory (`output.txt` inside it).
- Folder primitives: `swap_dir` (atomic freeze — rename to `<dir>.active` and recreate),
  `copy_dir` + `copy_to`, `move_dir` + `move_to` (atomic promote), `clear_dir` (wipe; a
  list wipes several at once).
- Control flow: `goto` jumps to a named step; `if` branches on the step's model output
  (`contains` / `equals` / `startswith` / `endswith` / `matches`) or on filesystem state
  (`count_ge` / `count_lt` / `empty` / `exists`), with `then` / `else` targets.
- `delay` (pause N seconds first), `retry` (re-issue on a transient failure), `timeout`
  (per-step override), `options` (per-call generation override), `exec` (run a shell
  command, no model call — e.g. `scripts/now.py` to stamp a shared timestamp).
- `every: N` — run only once every N visits (a per-step cycle throttle); `skip_if_empty:
  true` — skip the model call when `read_dir` yields nothing.

The reserved label `END` stops the run. The config is hot-reloaded on change (edit a step's
prompt or options and it applies to the next step), exactly like the proxy's own config.

See `extensions/swarm/README.md` for a worked example that exercises every primitive, and
`extensions/swarm/SWARM_REFERENCE.md` for the complete field reference.

## Repository layout

| Path | What it is |
| --- | --- |
| `proxy/` | The proxy itself — `mneme_proxy.py` plus the `mneme/` modules (tools, curation, templates, chat commands) |
| `scripts/` | `install.sh`, `mneme_setup.py` (wizard), `mneme_connect.py` (SSH tunnel), `run_openrouter.sh` |
| `tests/` | The deterministic suite (~292 tests) — see [Testing](#testing) |
| `extensions/` | Separate HTTP clients: `swarm/` (the reference example) and `pi/` — see [Extensions](#extensions) |
| `docs/` | Design and model notes: `model-notes.md` (which models misbehave and why), `strategy-retrieval-spec.md`, `provenance-and-chunk-lifecycle-plan.md` (planned: lineage tracking + soft-delete/purge), per-model write-ups |
| `experiments/` | Standalone probes used to develop the provenance work — not part of the runtime; kept so the measurements are reproducible |
| `launch.sh` | Convenience launcher for a **local** machine: starts the proxy in the background, then runs Pi; exiting Pi stops the proxy |
| `AGENTS.md` | Instructions written *for AI coding agents* — how to stand up an instance and how to author an extension. If you are pointing a coding agent at this repo, give it this file |
| `mneme.yaml.example` | Annotated reference config — every setting with a plain-English comment |
| `model_templates.yaml` | Known-good per-model generation settings — see [Model selection](#model-selection) |

## Branches

- `main` — **the release branch** (this branch). Memory retrieval, provenance grading, and the full toolset on; the experimental strategy/self-improving layer off by default (`memory_only: true`). Start here.
- `unified_mneme` — **the full build**. Same code with the experimental layer enabled by default (`memory_only: false`), plus the live-model capability benchmark harness. This is where the experimental features are developed and tested before being merged back into `main`.

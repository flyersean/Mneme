# Mneme — conversational memory proxy

> ⚠️ **Work in progress — vibe-coded and under active development.** This works,
> but it may have bugs and it changes as it's developed. Expect rough edges and
> occasional breakage. Feedback and bug reports welcome.

## Quick start

Three scripts take you from a fresh machine to a running proxy. Run the first two
on the machine that **hosts** the proxy (a laptop or a GPU pod); run the third on
your **laptop** to reach a remote proxy.

| Script | Where it runs | What it does |
|---|---|---|
| `install.sh` | the host | Installs Python dependencies + Ollama and clones the repo into `~/mneme/repo`. Idempotent — safe to re-run. |
| `mneme_setup.py` | the host | Interactive setup wizard: pick the backend (OpenRouter or Ollama), the chat/embed/label models, the context window, optional Pi, and the port. Writes the config + start script, launches the proxy, and health-checks it. |
| `mneme_connect.py` | your laptop | (Only for a remote pod.) Opens a stay-alive SSH tunnel and prints the local URLs to open in your browser. |

### 1. Install (on the host)

```bash
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/install.sh | MNEME_BRANCH=main bash
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

---

Mneme is a proxy that sits between an AI agent and its model backend, archives every conversation into searchable memory, and injects relevant past context on each turn. It grades its own epistemic honesty through provenance, not answer-correctness.

**Memory-only by default, full-featured underneath.** This branch (`main`) ships with the *strategy / self-improving layer* turned **off by default** — that's the one knob `MNEME_MEMORY_ONLY=1` controls. It limits which features are *on by default*, not which features exist: memory retrieval, provenance grading, and the full tool loop always run, and the off-by-default features are **experimental**, not dead. They're developed and tested on the `unified_mneme` branch and merged back into `main` as they stabilize. Set `MNEME_MEMORY_ONLY=0` to turn them on here (see "Experimental features" below).

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

### Provenance grading

*On — this is memory quality, not learning.*

- Provenance grading: the model tags its sources (`[source: X]` / `[guess]`) and is graded on *honesty*, not answer-correctness — "I don't know" beats fabrication.
- Honest-terminal detection: correct-but-uncitable answers — `undefined`, `market price`, "I don't know", "no such X", a false-premise correction, a clarification — are graded "pass", not fail. The judge misreads them as failures, so they're short-circuited before the judge.
- Trace cross-check: any cited `[source: mem_XXX]` or URL is verified against what the model actually had this turn (injected chunks + search results + the server-side tool trace), with host normalization so `shaws-wharf.com` matches `https://www.shaws-wharf.com/menu`. A fabricated citation fails.

### Experimental features (off by default)

*These exist and are under active development, but they're **off by default** on this
branch (`MNEME_MEMORY_ONLY=1`). They are not dead code — they're developed and tested
on the `unified_mneme` branch and merged back into `main` as they stabilize. Set
`MNEME_MEMORY_ONLY=0` to enable them here.*

- **Strategy / self-improving layer** — strategy learning from tool traces, novel-procedure detection, failure extraction, and belief evolution. Strategies are linked to the source chunk that produced them, and retrieval keys on that linkage (no hand-maintained problem-type taxonomy). A D/F turn distills one imperative directive to prevent recurrence — filtered through a junk-directive guard *and* skipped entirely for honest-terminal answers; SUCCESS strategies save only on a recovery (≥2 consecutive tool failures then success).
- **Capability-edge tracking & overcome** — records a competence edge per problem type; three consecutive tool failures flag it, and the next similar task is routed into **overcome mode** (hard-stop: build a tool, reuse a saved one, or — when the build budget is spent — answer honestly and surface the edge) instead of grinding or silently giving up. A built tool is saved and the edge can be cleared.
- **Thinking & learning modes** — `/mode/think` (novelty: generate a baseline, forbid its modal features, diverge, and grade novelty objectively via embedding distance + pairwise judge — not self-report) and `/mode/learn` (parameter cycling + strategy extraction).

## Usage and connecting clients

### Run the proxy

- **Start / restart the proxy:** `~/mneme/chunks/instances/<port>/start_proxy.sh` (written by setup).
- **Logs:** `tail -f /tmp/mneme.log`.

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

2. Point Pi at Mneme. Setup writes `~/.pi/agent/models.json` for you, but the shape is:

   ```json
   {
     "providers": {
       "mneme": {
         "baseUrl": "http://localhost:8080/v1",
         "api": "openai-completions",
         "apiKey": "none",
         "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
         "models": [{ "id": "text-mneme:64k", "name": "Mneme", "contextWindow": 64000, "reasoning": false }]
       }
     }
   }
   ```

3. Run Pi:

   ```bash
   pi --provider mneme --model text-mneme:64k \
     --extension ~/mneme/repo/extensions/pi/mneme-search-tool.ts \
     --extension ~/mneme/repo/extensions/pi/mneme-web-tools.ts
   ```

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

Retrieval is **two-floor**: a chunk scoring in `[strategy_min_similarity, inject_min_similarity)` isn't injected as memory, but any **strategy linked to that chunk** still is. This is how a learned approach ("verify the menu price on the restaurant's own site") generalizes to a *different* restaurant whose chunk sits just under the memory floor. Strategy retrieval is part of the experimental self-improving layer, so the second floor is inactive in the default memory-only build unless `MNEME_MEMORY_ONLY=0`.

Memory is **portable** across machines and even across 1024-dim embedders. On startup, the proxy re-embeds any chunk whose stored `embed_model` doesn't match the current one, so you can `scp` the `.db` from a pod to a laptop and it self-heals. Text, grades, and strategies survive; only vectors regenerate.

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
| `sampling.ctx_tokens` | `65536` | the model's context window (`num_ctx`) — must match the model's actual capability |
| `sampling.completion_reserve` | `8192` (setup writes `ctx/8`) | tokens held back for the model's reply — never touched by input |
| `caps.tool_followup_tokens` | `10000` (setup writes `ctx/6`) | tokens reserved for tool results inside the loop |
| `storage.memory_enabled` | `true` | master switch — `false` disables ALL memory (no retrieval/injection/staging, `search_memory` off) while keeping tools |
| `tools.search_memory` / `tools.list_tools` / `tools.read_tool` / `tools.read_file` / `tools.fetch_url` / `tools.web_search` | `true` each | per-tool on/off — set any to `false` to hide it from the model |

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

`MNEME_MEMORY_ONLY=1` (the default on this branch) turns off the experimental strategy/self-improving layer while keeping memory retrieval, provenance grading, and the full toolset. It is a *default on/off switch*, not a removal — the code stays present and tested.

`MNEME_MEMORY_ONLY=0` enables the experimental layer; the `unified_mneme` branch ships that way.

### Legacy thresholds

`route_threshold` and `classify_threshold` in the config are legacy:

- `route_threshold` is only used by the `/search` debug endpoint.
- `classify_threshold` is unused.
- Injection is governed by `inject_min_similarity`.

This is called out in the example's comments too.

## Multiple instances — one DB, many models

Mneme can run several proxy instances against one shared memory DB. Each instance listens on its own port (8080, 8081, 8082, …) and runs its own chat model + backend; they all read/write the same memory.

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

## Testing

Run the deterministic regression tests with:

```bash
~/mneme/venv/bin/python tests/test_tool_loop.py
```

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

69 tests.

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

Non-core consumers of Mneme live in `extensions/` — they use the proxy over HTTP but are
not part of the proxy stack:

- `extensions/pi` — Pi coding-agent tools that let Pi call Mneme's memory/web tools.
- `extensions/swarm` — a config-driven orchestrator that drives several Mneme proxies
  (and/or raw Ollama models) through a loop defined in a YAML config. It's a worked example
  of how to build a consumer of the proxy: it talks to proxies only over HTTP, with control
  flow (`goto`/`if`), folder primitives (`swap_dir`/`copy_dir`/`move_dir`/`clear_dir`,
  multi-directory reads, `append_dir`), pacing, retry, and both backends. See
  `extensions/swarm/README.md`.

## Branches

- `main` — **the release branch** (this branch). Memory retrieval, provenance grading, and the full toolset on; the experimental strategy/self-improving layer off by default (`MNEME_MEMORY_ONLY=1`). Start here.
- `unified_mneme` — **the full build**. Same code with the experimental layer enabled by default (`MNEME_MEMORY_ONLY=0`), plus the live-model capability benchmark harness. This is where the experimental features are developed and tested before being merged back into `main`.

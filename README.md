# Mneme — conversational memory proxy

A proxy that sits between an AI agent and its model backend, archives every
conversation into searchable memory, and injects relevant past context on each
turn. It grades its own epistemic honesty (provenance, not answer-correctness)
and evolves strategies through a self-improving loop.

**Backend-agnostic.** One config file chooses the backend — local
[Ollama](https://ollama.com) or any OpenAI-compatible provider (OpenRouter,
OpenAI, DeepSeek, Groq, Together, Mistral, ...). No GPU or model downloads
required when running against a hosted provider.

**Three models, one DB.** Mneme runs three models against a single shared memory
store (one SQLite DB + one FAISS index): the **chat** model (answers you), the
**embedder** (turns text into vectors), and the **labeler** (tags topics). All
three read/write the same DB, so memory is shared and consistent. The chat model
is fixed by config — the request's `model` field is not used to route to a
different chat model.

```
Any OpenAI client ──▶ Mneme Proxy (:8080) ──▶ your model backend (Ollama or OpenAI-compatible)
                           │
                           └──▶ SQLite + FAISS memory (injected back into the prompt)
```

Everything lives under one directory, `~/mneme/`:

```
~/mneme/
  repo/      this repository (git clone)
  env        your OpenRouter API key (chmod 600; only for the hosted backend)
  chunks/    memory DB (mneme.db), config (mneme.yaml), log, and editable prompts
```

---

## Install + setup (two commands)

One unified path — the same two commands on a laptop or a pod. The installer
prepares the machine; the setup wizard asks how you want to run it.

### 1. Install (dependencies + Ollama + proxy code)

```bash
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/install.sh | bash
```

Installs the Python dependencies, Ollama (idempotent — harmless even for a
hosted backend), and clones the proxy into `~/mneme/repo`. Safe to re-run; it
only fills in what's missing.

### 2. Setup (backend, models, Pi)

```bash
curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_setup.py && python3 /tmp/setup.py
```

The wizard walks four steps:

1. **Backend** — OpenRouter (hosted; needs an API key) or Ollama (local/private).
2. **Models** — chat / embedder / labeler (per backend; Ollama pulls them for you).
3. **Pi** — optional terminal assistant. Saying **no** still leaves the built-in
   chat page and any OpenAI-compatible client working.
4. **Port** + whether to inject Mneme's system instructions.

It writes one config (`~/mneme/chunks/mneme.yaml`) and a start script
(`~/mneme/chunks/start_proxy.sh`), then launches the proxy and health-checks it.

---

## The two web pages

Once the proxy is running (any backend):

| URL | What it is |
|---|---|
| `http://localhost:8080/` (or `/chat`) | **Chat UI** — a light-theme web client over `/v1/chat/completions`. Full native toolset (memory search, built-tool registry, `bash`/`write`). |
| `http://localhost:8080/instructions` | **Prompt editor** — every injected prompt, in the order it fires during a conversation. Read them, edit them inline (Save writes straight back to the file), or click "open file" for the raw text. |
| `http://localhost:8080/v1` | OpenAI-compatible API base (for Pi, Hermes, or any client). |
| `http://localhost:8080/health` | Health check (`curl http://localhost:8080/health`). |

---

## Run it / connect

- **Start / restart the proxy:** `~/mneme/chunks/start_proxy.sh` (written by setup).
- **Logs:** `tail -f /tmp/mneme.log`.

### Pi (terminal assistant)

Pi is offered during setup. To install or run it by hand:

1. Install (needs Node.js 22+):

   ```bash
   npm install -g @earendil-works/pi-coding-agent
   ```

2. Point Pi at Mneme — setup writes `~/.pi/agent/models.json` for you, but the
   shape is:

   ```json
   {
     "providers": {
       "mneme": {
         "baseUrl": "http://localhost:8080/v1",
         "api": "openai-completions",
         "apiKey": "none",
         "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
         "models": [{ "id": "text-mneme:64k", "name": "Mneme", "contextWindow": 32000, "reasoning": false }]
       }
     }
   }
   ```

3. Run:

   ```bash
   pi --provider mneme --model text-mneme:64k \
     --extension ~/mneme/repo/extensions/pi/mneme-search-tool.ts \
     --extension ~/mneme/repo/extensions/pi/mneme-web-tools.ts
   ```

### Connect to a remote pod

If Mneme runs on a pod (RunPod, etc.), run the standalone connect app on your
laptop to open a stay-alive SSH tunnel and get the local URLs:

```bash
curl -sSL -o /tmp/mneme_connect.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_connect.py && python3 /tmp/mneme_connect.py
```

It prompts for the pod address + SSH port, opens the tunnel (with keep-alive),
then prints the OpenAI API base URL, the chat URL, and the prompt-editor URL to
open in your browser.

### Any other OpenAI client

Anything OpenAI-compatible (Hermes, Open WebUI, etc.) just needs the base URL:

- `http://localhost:8080/v1`

---

## Configuration

Everything is one file — `$MNEME_CHUNK_DIR/mneme.yaml` (default
`~/mneme/chunks/mneme.yaml`) — plus a few env vars. Settings are resolved
**env var > config file > built-in default**, and the proxy logs a `[CONFIG]`
line at startup showing the final value of every setting, so a typo or an
overriding env var is visible, not silent.

The knobs you'll actually touch (see `mneme.yaml.example` for full comments):

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

Full reference: [`docs/config-spec.md`](docs/config-spec.md).

> **Tuning `inject_min_similarity` per embedder.** This is the one setting you
> must NOT copy blindly between deployments: every embedding model has its own
> similarity scale, so a threshold that works for one silently drops most
> relevant memories for another. Measure yours by embedding a few
> obviously-relevant and obviously-irrelevant queries and set the floor just
> above the noise. Reference scales: `voyage-4-lite` noise ~0.48 / relevant
> ~0.70 → use ~0.62; `snowflake-arctic-embed2` noise ~0.32 / relevant ~0.40 →
> use ~0.45 (the default). `strategy_min_similarity` must always stay below it.

> Note: `route_threshold` and `classify_threshold` in the config are legacy —
> `route_threshold` is only used by the `/search` debug endpoint and
> `classify_threshold` is unused. Injection is governed by
> `inject_min_similarity`. (This is called out in the example's comments too.)

---

## How memory works

Every turn is staged, then on save the proxy:

1. **Labels** message groups into topics with the labeler model.
2. **Embeds** each group with the embedder (1024-dim).
3. **Stores** in SQLite (`chunks` table) + a FAISS `IndexFlatIP`.
4. On the next request: embeds the query, finds FAISS nearest neighbours, and
   injects the chunks whose similarity clears `inject_min_similarity`.

The retrieval gate is an **absolute similarity floor**, not a relative one:
if nothing in memory scores above `inject_min_similarity`, nothing is injected
(no "best guess" noise). A substring keyword fallback exists but is **off by
default** (`keyword_fallback: false`) because it has no semantic score and
pollutes context (e.g. "tool" matching an unrelated "Paramotor Tool" memory).

Retrieval is **two-floor**: a chunk scoring in `[strategy_min_similarity,
inject_min_similarity)` isn't injected as memory, but any **strategy linked to
that chunk** still is. This is how a learned approach ("verify the menu price on
the restaurant's own site") generalizes to a *different* restaurant whose chunk
sits just under the memory floor. Strategies are retrieved by their **source
chunk** — the chunk that produced them — not by a hand-maintained taxonomy, so a
strategy goes wherever its source chunk is relevant.

Memory is **portable** across machines and even across 1024-dim embedders: on
startup the proxy re-embeds any chunk whose stored `embed_model` doesn't match
the current one, so you can `scp` the `.db` from a pod to a laptop and it
self-heals. Text, grades, and strategies survive; only vectors regenerate.

---

## Multiple instances — one DB, many models

Mneme can run several proxy instances against one shared memory DB. Each
instance listens on its own port (8080, 8081, 8082, …) and runs its own chat
model + backend; they all read/write the same memory.

To add an instance, run the setup wizard again and point it at the same DB
directory. It detects the existing DB and offers **"Add another proxy instance"**.
The wizard auto-picks the next free port, asks for the new instance's chat model
(and backend), and writes a per-instance start script (`start_proxy_<port>.sh`).

Two hard rules:

1. **Same embedder everywhere.** Every instance sharing a DB must use the SAME
   embedding model. The vectors in one DB live in one semantic space. If
   instance A embeds with `snowflake-arctic-embed2` (Ollama) and instance B with
   `voyage-4-lite` (OpenRouter), both are 1024-dim so FAISS won't crash — but
   similarity across them is garbage. The startup health check flags "different
   embed model, same dim" but does not prevent it. The wizard locks the embedder
   (and labeler) to the first setup's choice; don't change them on a shared DB.

2. **Same machine is rock solid; cross-machine needs a real shared filesystem.**
   On one machine, instances share the DB with plain file locking (SQLite WAL +
   fcntl on the FAISS index). Across machines, the DB directory must live on
   shared storage (NFS/S3-mount), and the fcntl lock is only reliable on NFSv4 —
   on NFSv3 or a plain S3 mount, concurrent writes aren't safely serialized. So
   "one instance on RunPod + one on an Ollama pod" needs a proper shared
   filesystem, not just network reachability.

---

## Features

**Core memory**
- Topic-aware chunking with automatic LLM labeling
- FAISS vector search gated by an absolute `inject_min_similarity` floor
- Recency-weighted scoring (cycle-based, not wall-clock)
- Source tracking (user, model, tool:*, page:*, document:*)
- Embedding reliability: startup health check probes the embedder and fails
  loud on a dim mismatch; a failed embed is stored `pending_embed` and
  re-embedded on next startup (no silent dead vectors)

**Learning & strategy layer**
- Provenance grading: the model tags its sources (`[source: X]` / `[guess]`) and
  is graded on *honesty*, not answer-correctness — "I don't know" beats
  fabrication.
- Honest-terminal detection: correct-but-uncitable answers — `undefined`,
  `market price`, "I don't know", "no such X", a false-premise correction, a
  clarification — are graded "pass", not fail. The judge misreads them as
  failures, so they're short-circuited before the judge.
- Trace cross-check: any cited `[source: mem_XXX]` or URL is verified against
  what the model actually had this turn (injected chunks + search results + the
  server-side tool trace), with host normalization so `shaws-wharf.com` matches
  `https://www.shaws-wharf.com/menu`. A fabricated citation fails.
- Source-chunk linkage: every strategy is linked to the chunk that produced it,
  and retrieval keys on that linkage (see "How memory works") — no
  problem-type taxonomy to maintain.
- Novel-procedure detection: a working new technique is detected from the tool
  trace, graded "great", and saved as a strategy.
- Failure extraction: a D/F turn distills one imperative directive to prevent
  recurrence — filtered through a junk-directive guard *and* skipped entirely
  for honest-terminal answers, so a false-positive fail can't teach the model
  to avoid a correct approach. SUCCESS strategies save only on a recovery
  (≥2 consecutive tool failures then success), not every pass.

**Capability-edge tracking & overcoming** — records a competence edge per problem
type; three consecutive tool failures flag it, and the next similar task is
routed into **overcome mode** (hard-stop: build a tool, reuse a saved one, or —
when the build budget is spent — answer honestly and surface the edge) instead
of grinding or silently giving up. A built tool is saved and the edge can be
cleared.

**Thinking & learning modes** — `/mode/think` (novelty: generate a baseline,
forbid its modal features, diverge, and grade novelty objectively via embedding
distance + pairwise judge — not self-report) and `/mode/learn` (parameter
cycling + strategy extraction).

---

## Testing

```bash
~/mneme/venv/bin/python tests/test_tool_loop.py
```

Deterministic regression tests — no live model or network. A scripted model
stands in for the LLM, and the real SQLite/FAISS + retrieval paths run against
an in-memory DB. They cover the tool-calling loop (search → answer, search →
web_search hand-off, search-loop exhaustion), the injection gate
(`inject_min_similarity` floor, keyword fallback off), the step-back ladder,
the capability-edge → overcome routing, the injected-prompt materializer,
provenance grading (honest-terminal detection, source/URL normalization,
tool-trace URL extraction, fabricated-citation fails), and the two-floor
retrieval helpers. 58 tests.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Debug search: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/` / `/chat` | Built-in chat UI |
| GET | `/instructions` | Prompt reference + editor (the injected prompts) |
| GET | `/list` | List all chunks with metadata |
| GET/POST | `/capabilities` | List capability-edge records / flag or clear |
| POST | `/mode/think` | Novelty thinking mode (escape mode collapse) |
| POST | `/mode/learn` | Learning mode (parameter cycling + strategy extraction) |
| GET/POST | `/preferences` | Read / set user preferences |

---

## Architecture

Single-file Flask proxy (`proxy/mneme_proxy.py`) with module-level state (FAISS
index, SQLite connection, staging buffer) and threaded daemon archival. The
backend is selected by config (`backend.type` + `providers:`); `query_model`
→ chat completions, `embed` → embeddings, and the labeler → chat completions,
all OpenAI-compatible, so swapping providers is a config edit, not a code change.

Two deliberate runtime details worth knowing:

- **Prefix-cache-stable context.** The fixed instruction block (system prompt +
  meta-principles) sits at the front of every request as a byte-stable prefix;
  all *variable* content (memory, strategies, preferences, tool hints) is
  appended at the tail, never inserted mid-prefix — so any prefix-caching
  backend (OpenRouter, Ollama, etc.) can reuse the cached prefix across turns.
- **Serialized writes.** The one SQLite connection is shared by the request
  thread + two archival workers, so every write+commit pair is guarded by a
  re-entrant lock — no "cannot commit, no transaction is active" races.

---

## Branches

- `unified_mneme` — **current development branch** (this README). The full build:
  strategy/self-improving layer enabled by default (`MNEME_MEMORY_ONLY=0`).
- `main` — **memory-only build**: the same latest code with the strategy/
  self-improving layer disabled by default (`MNEME_MEMORY_ONLY=1`). Memory
  retrieval, provenance grading, and the full toolset stay on. Start here if you
  only want memory. Install it with `MNEME_BRANCH=main` (see install step).
- `novelty-thinking` — experimental learning/strategy layer (Ollama, Muse 30B).
- `openrouter-backend` — earlier hosted-OpenRouter branch (superseded by
  `unified_mneme`'s provider registry).
- `build-roadmap`, `dev-chunks`, `dev-v2` — legacy / restore points.

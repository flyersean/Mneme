# Mneme — conversational memory proxy

A proxy that sits between an AI agent and its model backend, archives every
conversation into searchable memory, and injects relevant past context on each
turn. It grades its own epistemic honesty (provenance, not answer-correctness)
and evolves strategies through a self-improving loop.

**Backend-agnostic.** One config file chooses the backend — local
[Ollama](https://ollama.com) or any OpenAI-compatible provider (OpenRouter,
OpenAI, DeepSeek, Groq, Together, Mistral, ...). No GPU or model downloads
required when running against a hosted provider.

```
Any OpenAI client ──▶ Mneme Proxy (:8080) ──▶ your model backend (Ollama or OpenAI-compatible)
                           │
                           └──▶ SQLite + FAISS memory (injected back into the prompt)
```

Everything lives under one directory, `~/mneme/`:

```
~/mneme/
  repo/      this repository (git clone)
  venv/      Python virtualenv (faiss / numpy / flask / requests / pyyaml)
  env        your API key (chmod 600; never in the repo)
  chunks/    memory DB (mneme.db), config (mneme.yaml), log, and editable prompts
```

---

## Quick start — pick your path

| You want… | Path |
|---|---|
| Hosted models, no GPU, no downloads (recommended for a laptop) | **Path A — OpenRouter** (one command) |
| Local models, private, free | **Path C — Ollama** |

Both end at the same place: a proxy on `http://localhost:8080` with a built-in
chat page and an editable-prompt page.

---

## The two web pages

Once the proxy is running (any path):

| URL | What it is |
|---|---|
| `http://localhost:8080/` (or `/chat`) | **Chat UI** — a light-theme web client over `/v1/chat/completions`. Full native toolset (memory search, built-tool registry, `bash`/`write`). |
| `http://localhost:8080/instructions` | **Prompt editor** — every injected prompt, in the order it fires during a conversation. Read them, edit them inline (Save writes straight back to the file), or click "open file" for the raw text. |
| `http://localhost:8080/v1` | OpenAI-compatible API base (for Pi, Hermes, or any client). |
| `http://localhost:8080/health` | Health check (`curl http://localhost:8080/health`). |

---

## Getting started — step by step

### Path A — OpenRouter (hosted, one command)

Fully hosted: the chat model, embedder, and labeler all run on OpenRouter, so
there is nothing to download except the ~250 MB venv. The wizard validates your
key, lets you pick the three models (sensible cheap defaults), creates the venv,
writes the config, and starts the proxy:

```bash
curl -sSL -o /tmp/setup_or.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_setup_openrouter.py && python3 /tmp/setup_or.py
```

You'll need an OpenRouter key (https://openrouter.ai/keys). Then open
`http://localhost:8080/` to chat.

### Path B — manual setup (clone + venv + key + config)

If you prefer to do it yourself (or the wizard is unavailable):

```bash
# 1. Clone the current branch into the ~/mneme/ layout
git clone --branch unified_mneme https://github.com/flyersean/Mneme.git ~/mneme/repo
cd ~/mneme/repo

# 2. venv (one-time, ~250 MB)
python3 -m venv ~/mneme/venv
~/mneme/venv/bin/pip install faiss-cpu numpy flask flask-cors requests pyyaml

# 3. API key (chmod 600; never committed)
echo 'OPENROUTER_API_KEY=sk-or-v1-...' > ~/mneme/env && chmod 600 ~/mneme/env

# 4. Config (copy the commented example; defaults already point at OpenRouter)
mkdir -p ~/mneme/chunks
cp mneme.yaml.example ~/mneme/chunks/mneme.yaml

# 5. Launch
./launch.sh
```

### Path C — Ollama (local models, private)

The proxy defaults to the Ollama backend — you just need Ollama running with the
three models pulled (chat, embedder, labeler):

```bash
# install + start Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama serve   # or it may already be running

# pull the three models (chat / embedder / labeler)
ollama pull <your-chat-model>          # e.g. qwen3:32b, llama3.1:70b, …
ollama pull snowflake-arctic-embed2    # 1024-dim embedder
ollama pull qwen2.5:0.5b               # tiny topic labeler

# run the proxy (no API key needed)
cd ~/mneme/repo
export MNEME_BACKEND=ollama                                      # use local Ollama
export MNEME_MODEL=<your-chat-model>                             # e.g. qwen3:32b, llama3.1:70b, …
export EMBED_MODEL=snowflake-arctic-embed2                       # 1024-dim embedder
export LABEL_MODEL=qwen2.5:0.5b                                  # tiny topic labeler
~/mneme/venv/bin/python -uB proxy/mneme_proxy.py
```

Then open `http://localhost:8080/`. To make it permanent, set
`backend.type: ollama` and `backend.ollama_url: http://localhost:11434` in
`~/mneme/chunks/mneme.yaml` (Ollama is the default only when no config
overrides it — the example config targets OpenRouter, so set `backend.type`
explicitly), and put the model names in the `MNEME_MODEL` / `EMBED_MODEL` /
`LABEL_MODEL` env vars.

> The embedder must stay 1024-dim (the FAISS index is built at 1024). The
> startup health check probes the embedder and fails loud on a dimension
> mismatch, so a wrong embedder is caught immediately, not silently.

---

## Running through Pi

[Pi](https://github.com/earendil-works/pi) is a terminal AI coding assistant.
`launch.sh` starts the proxy **and** launches Pi against it in one go:

```bash
cd ~/mneme/repo
./launch.sh      # starts the proxy, waits for health, then launches Pi
```

`./launch.sh` does the Pi work for you, but if you want to set it up by hand:

1. **Install Pi** (needs Node.js 22+):

   ```bash
   npm install -g @earendil-works/pi-coding-agent
   ```

2. **Point Pi at Mneme** — add a `mneme` provider in `~/.pi/agent/models.json`:

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

3. **Run Pi with the Mneme tools** (memory search + web search):

   ```bash
   pi --provider mneme --model text-mneme:64k \
     --extension ~/mneme/repo/extensions/pi/mneme-search-tool.ts \
     --extension ~/mneme/repo/extensions/pi/mneme-web-tools.ts
   ```

The proxy alone (no Pi) is `scripts/run_openrouter.sh`. Exiting Pi stops the
proxy started by `launch.sh`.

### Connect any other OpenAI client

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
| `retrieval.inject_min_similarity` | `0.62` | **the main knob** — minimum cosine similarity for a memory to be injected. Below it, inject *nothing*. Raise = fewer/higher-confidence; lower = more recall |
| `retrieval.max_injected_tokens` | `8000` | token budget for memory stuffed into the prompt |

Full reference: [`docs/config-spec.md`](docs/config-spec.md).

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

Memory is **portable** across machines and even across 1024-dim embedders: on
startup the proxy re-embeds any chunk whose stored `embed_model` doesn't match
the current one, so you can `scp` the `.db` from a pod to a laptop and it
self-heals. Text, grades, and strategies survive; only vectors regenerate.

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
- Trace cross-check: any cited `[source: mem_XXX]` or URL is verified against
  what the model actually had this turn; a fabricated citation fails.
- Novel-procedure detection: a working new technique is detected from the tool
  trace, graded "great", and saved as a strategy.
- Failure extraction: a D/F turn distills one imperative directive to prevent
  recurrence — filtered through a junk-directive guard so hallucinated
  "strategies" never enter memory.

**Capability-edge tracking & overcoming** — records a competence edge per problem
type; two poor grades flag it, and the next similar task is routed into
**overcome mode** (hard-stop: build a tool, reuse a saved one, or honestly
declare the missing capability) instead of grinding or silently giving up. A
built tool is saved and the edge can be cleared.

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
the capability-edge → overcome routing, and the injected-prompt materializer.

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

---

## Branches

- `main` — stable, stripped-down memory-only build (Ollama). Start here if you
  only want memory.
- `unified_mneme` — **current branch.** `novelty-thinking`'s learning/strategy
  layer on top of the config-file + backend generalization (one `mneme.yaml`,
  `providers:` registry, `ollama | openai`). This README describes it.
- `novelty-thinking` — experimental learning/strategy layer (Ollama, Muse 30B).
- `openrouter-backend` — earlier hosted-OpenRouter branch (superseded by
  `unified_mneme`'s provider registry).
- `build-roadmap`, `dev-chunks`, `dev-v2` — legacy / restore points.

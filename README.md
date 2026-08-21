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

---

## Getting started

### 1. Clone

```bash
git clone --branch unified_mneme https://github.com/flyersean/Mneme.git
cd Mneme
```

### 2. One-time setup: API key + venv

The proxy needs a Python venv with a few deps, and (for hosted backends) an API key.

```bash
# venv (one-time, ~250MB)
python3 -m venv ~/mneme-venv
~/mneme-venv/bin/pip install faiss-cpu numpy flask flask-cors requests pyyaml

# API key — pick ONE:
#   a) save it where launch.sh looks for it (chmod 600):
#        echo 'OPENROUTER_API_KEY=sk-...' > ~/.mneme/openrouter.env
#   b) or just export it each time:
#        export OPENROUTER_API_KEY=sk-...
```

The API key is never written into the repo — it lives in an env var or a
`~/.mneme/openrouter.env` file that is gitignored by convention.

> `scripts/mneme_setup_openrouter.py` also exists and can validate your key and
> show your credit balance, but it writes a legacy `setup_config.json`. The
> proxy now reads `mneme.yaml` (below) instead — prefer the manual steps above.

### 3. Configure (optional)

The proxy auto-loads a config file from next to the memory DB
(`$MNEME_CHUNK_DIR/mneme.yaml`, default `~/mneme_chunks/mneme.yaml`). Copy the
example to get started:

```bash
mkdir -p ~/mneme_chunks
cp mneme.yaml.example ~/mneme_chunks/mneme.yaml
```

The example is fully commented — every setting explains what it does. The
defaults already point at OpenRouter with `deepseek/deepseek-v4-flash`, so a
fresh copy works as-is for a hosted setup. See **Configuration** below for the
knobs you'll actually tune.

### 4. Launch

```bash
./launch.sh
```

This starts the proxy (backgrounded, logging to `~/mneme_chunks/mneme.log`),
waits for it to be healthy, then launches Pi with the Mneme search extensions.
Exiting Pi stops the proxy. To run the proxy alone:

```bash
scripts/run_openrouter.sh        # proxy only, no Pi
```

The proxy is OpenAI-compatible at `http://localhost:8080/v1`. Verify with
`curl http://localhost:8080/health`.

### Connect any OpenAI client

- **Pi**: `pi --provider mneme --model text-mneme:64k --extension extensions/pi/mneme-search-tool.ts --extension extensions/pi/mneme-web-tools.ts`
  (point Pi's `mneme` provider at `http://localhost:8080/v1`.)
- **Hermes / anything OpenAI-compatible**: set the base URL to `http://localhost:8080/v1`.

---

## Configuration

Everything is one file — `$MNEME_CHUNK_DIR/mneme.yaml` — plus a few env vars.
Settings are resolved **env var > config file > built-in default**, and the
proxy logs a `[CONFIG]` line at startup showing the final value of every
setting, so a typo or an overriding env var is visible, not silent.

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

**Capability-edge tracking** — records a competence edge per problem type; two
poor grades flag it and the next similar task injects a "propose the tool,
don't grind" directive.

**Thinking & learning modes** — `/mode/think` (novelty: generate a baseline,
forbid its modal features, diverge, and grade novelty objectively via embedding
distance + pairwise judge — not self-report) and `/mode/learn` (parameter
cycling + strategy extraction).

---

## Testing

```bash
~/mneme-venv/bin/python tests/test_tool_loop.py
```

Deterministic regression tests — no live model or network. A scripted model
stands in for the LLM, and the real SQLite/FAISS + retrieval paths run against
an in-memory DB. They cover the tool-calling loop (search → answer, search →
web_search hand-off, search-loop exhaustion) and the injection gate
(`inject_min_similarity` floor, keyword fallback off).

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Debug search: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
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

# Mneme — openrouter-backend (hosted, no local models)

> ⚠️ **This is the OpenRouter backend branch.** It forks from `novelty-thinking`
> (learning/strategy layer, epistemic grading, capability-edge tracking) but swaps
> all three model roles — main LLM, embedder, and labeler — to **OpenRouter**.
> No Ollama, no GPU, no model downloads. Runs on a laptop.

Conversational memory proxy between AI agents and OpenRouter. Archives conversations,
classifies by topic, injects relevant past context, and — like the parent branch —
grades its own epistemic honesty and evolves strategies through a self-improving loop.
Model-agnostic. Self-improving.

## How It Works

```
Any OpenAI client → Mneme Proxy (:8080) → OpenRouter (hosted models)
                         ↕
                 SQLite + FAISS memory
```

Every conversation turn is staged, then on save the proxy:
1. Classifies messages into topic groups via LLM labeling (`meta-llama/llama-3.2-3b-instruct`)
2. Embeds each group with `voyageai/voyage-4-lite` (1024-dim)
3. Stores in SQLite (chunks table) + FAISS (IndexFlatIP)
4. On future requests: searches FAISS + keyword fallback, injects top matches

The main model (`deepseek/deepseek-v4-flash` by default) does the chatting, the
grading, and the strategy discovery. All three roles are hosted — nothing runs locally
except the proxy itself.

## Getting Started (local machine)

### Step 1: Clone and run the setup wizard

```bash
git clone --branch openrouter-backend https://github.com/flyersean/Mneme.git
cd Mneme
python3 scripts/mneme_setup_openrouter.py
```

The wizard:

1. **Asks for your OpenRouter API key**, validates it against OpenRouter, shows your
   remaining credit balance, and saves it to `~/.mneme/openrouter.env` (`chmod 600` —
   it is never written into the repo).
2. Lets you pick the main / embedder / labeler models (sensible cheap defaults).
3. Creates a venv (`~/mneme-venv`) with faiss/numpy/flask/requests if missing.
4. Writes `setup_config.json` + a `start_proxy.sh` next to the memory DB, launches the
   proxy, and health-checks it.

Alternatively, run it via curl (the script auto-clones the repo if it can't find one):

```bash
curl -sSL -o /tmp/setup_or.py https://raw.githubusercontent.com/flyersean/Mneme/openrouter-backend/scripts/mneme_setup_openrouter.py && python3 /tmp/setup_or.py
```

After setup the proxy is at `http://localhost:8080` with an OpenAI-compatible API at `/v1`.

### Step 2: Connect an agent

- **Pi**: `pi --provider mneme --model text-mneme:64k --extension extensions/pi/mneme-search-tool.ts --extension extensions/pi/mneme-web-tools.ts`
  (point Pi's `mneme` provider at `http://localhost:8080/v1`, `api: openai-completions`.)
- **Hermes / any OpenAI client**: connect to `http://localhost:8080/v1`.

### Manual start

```bash
export OPENROUTER_API_KEY=$(grep -iE '^OPENROUTER_API_KEY=' ~/.hermes/profiles/deep1/.env | cut -d= -f2-)
scripts/run_openrouter.sh
```

Or use the generated `~/mneme_chunks/start_proxy.sh` (sources the saved key for you).

### Multi-instance (shared DB)

Run several proxy instances against one memory DB by giving each a distinct
`MNEME_PORT` and the same `MNEME_CHUNK_DIR`. Nothing is killed — you decide what stays up:

```bash
MNEME_PORT=8080 scripts/run_openrouter.sh   # instance 1
MNEME_PORT=8081 scripts/run_openrouter.sh   # instance 2, same DB
```

All instances must use the **same embedder and labeler** — the embeddings share one
FAISS index with a fixed 1024 dimension.

## Models

| Role     | Default                            | Notes |
|----------|------------------------------------|-------|
| Main LLM | `deepseek/deepseek-v4-flash`       | cheap thinking MoE ($0.064/M in) |
| Embedder | `voyageai/voyage-4-lite`           | 1024-dim (matches FAISS), $0.02/M |
| Labeler  | `meta-llama/llama-3.2-3b-instruct` | small, non-thinking |

The labeler **must be non-thinking** — a thinking model (e.g. deepseek-v4-flash) burns
its token budget on reasoning and returns empty `content` for a trivial "3-5 word label"
task, so labels fall back to the heuristic. The embedder **must output 1024-dim**
(or you must change `DIM` and re-embed).

## Portability (local ↔ pod, different embedders)

Memory is portable between machines even when they use different embedding models,
as long as both are 1024-dim. On startup the proxy compares each chunk's stored
`embed_model` against the current `EMBED_MODEL`; mismatched chunks (and wrong-dim
vectors) are marked `pending_embed` and re-embedded automatically. So:

```
scp the .db from the pod to your laptop → restart → it self-heals
```

Text, grades, and strategies survive; only the vectors regenerate.

## Features

**Core Memory**
- Topic-aware chunking with automatic LLM labeling
- FAISS vector search + SQLite keyword fallback (hybrid retrieval)
- Noise-floor calibration at startup (subtracts baseline from cosine scores)
- Recency-weighted scoring (cycle-based, not wall-clock)
- Source tracking (user, model, tool:*, page:*, document:*)
- Embedding reliability: startup health check probes the embedder and fails loudly on a
  dim mismatch; a failed embed is stored `pending_embed` and re-embedded on next startup
  (no silent dead vectors)

**Learning & Strategy Layer** (inherited from `novelty-thinking`)
- Pass/fail/great grading: the model tags provenance (`[source: X]` / `[guess]`) and is
  graded on *honesty*, not answer correctness — "I don't know" beats fabrication.
- Trace cross-check: any `[source: mem_XXX]` or URL the model cites is verified against
  what it actually had this turn; a fabricated citation grades fail.
- Novel-procedure detection: a working NEW technique is detected from the tool trace,
  graded "great", and saved as a strategy with cost metadata.
- Strategy ranking: injected grade-first, then cheaper-wins.
- Failure extraction: a D/F turn distills one imperative directive to prevent recurrence.

**Capability-Edge Tracking** — records a competence edge per problem type; two poor
grades flag it and the next similar task injects a "propose the tool, don't grind"
directive.

**Novelty & Divergence Modes** — `/mode/think`, `/mode/divergent`, and
adversarial-collaboration endpoints that grade novelty objectively (embedding distance +
pairwise judge), not by self-report.

**v2 Architecture** — persona-free system prompt, `<<COMMAND>>` stripping, content
normalization (string and array content).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Search memory: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/list` | List all chunks with metadata |
| GET | `/capabilities` | List capability-edge records (GET) / flag or clear (POST) |

## Architecture

Single-file Flask proxy (`proxy/mneme_proxy.py`). Module-level state (FAISS index,
SQLite connection, staging buffer). Threaded server with daemon archival threads.
`MNEME_BACKEND=openrouter` routes `query_model` → OpenRouter `/chat/completions`,
`embed` → `/embeddings`, and the labeler → `/chat/completions` (all OpenAI-compatible);
the default `MNEME_BACKEND=ollama` path is unchanged and still works.

## Branches

- `main` — stable, stripped-down memory-only build (Ollama). Start here if you're new.
- `novelty-thinking` — experimental learning/strategy layer (Ollama, Muse 30B).
- `openrouter-backend` — **this branch.** `novelty-thinking` on a fully hosted OpenRouter
  backend (no Ollama, no GPU). Local-machine friendly.
- `build-roadmap`, `dev-chunks`, `dev-v2` — legacy / restore points.

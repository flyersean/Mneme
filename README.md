# Mneme — novelty-thinking (experimental)

> ⚠️ **This is the EXPERIMENTAL branch.** It includes the learning/strategy layer,
> epistemic grading, and capability-edge tracking. For the stripped-down,
> memory-only build, see the [`main` branch README](https://github.com/flyersean/Mneme/blob/main/README.md).

Conversational memory proxy between AI agents and Ollama. Archives conversations,
classifies by topic, injects relevant past context, and — in this branch — grades
its own epistemic honesty and evolves strategies through a self-improving loop.
Model-agnostic. Self-improving.

## How It Works

```
Any OpenAI client → Mneme Proxy (:8080) → Ollama (:11434)
                         ↕
                 SQLite + FAISS memory
```

Every conversation turn is staged, then on save the proxy:
1. Classifies messages into topic groups via LLM labeling (qwen2.5:0.5b)
2. Embeds each group with arctic-embed2 (1024-dim)
3. Stores in SQLite (chunks table) + FAISS (IndexFlatIP)
4. On future requests: searches FAISS + keyword fallback, injects top matches

## Getting Started

### Step 1: Install dependencies (pipe-safe, one command)

```bash
curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/scripts/install_deps.sh | bash
```

Idempotent — installs Ollama, Python packages, proxy code. Detects and skips
anything already present. Safe to run repeatedly.

### Step 2: Run setup wizard

```bash
rm -f /tmp/setup.py; curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/scripts/mneme_setup.py && python3 /tmp/setup.py
```

Interactive wizard: model → context size → chat interface → embedding → labeling.
Both scripts self-update on every run.

The wizard's model menu includes a **Muse Glimmer 30B** option (recommended for
this branch) that auto-provisions the model: it pulls the Blackfrost abliterated
GGUF and applies the corrected chat template via `ollama create muse-glimmer:30b`
(without this template the model stalls at ~3 tokens — see
`docs/muse-glimmer-model.md`). Pick 32K context for Muse.

After setup, the proxy is at `http://localhost:8080` with an OpenAI-compatible
API at `/v1`.

### Local Connect

Run on your laptop to SSH-tunnel into the pod and launch an agent:

```bash
python3 scripts/mneme_connect.py
```

Prompts for pod IP/port, then:
- **Hermes**: creates a new profile, displays connection info, launches `hermes --profile mneme`
- **Pi**: writes config, launches `pi --provider mneme --model text-mneme:64k`

### Manual Start

```bash
cd /workspace
MNEME_MODEL="muse-glimmer:30b" \
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \
  OLLAMA_KEEP_ALIVE=24h PYTHONDONTWRITEBYTECODE=1 \
  python3 -uB proxy/mneme_proxy.py
```

For large context windows, set `OLLAMA_FLASH_ATTENTION=1` and
`OLLAMA_KV_CACHE_TYPE=q8_0` before starting Ollama.

### Multi-Instance (shared DB)

Run several proxy instances — e.g. one per model — against a single memory DB.
Each instance is a separate process on its own port, all pointing at the same
`MNEME_CHUNK_DIR`; the proxy coordinates them with fcntl locks on FAISS, a
disk-persisted index, and SQLite WAL mode. Chunks written by any instance are
visible to all.

**Add a second model via the wizard (easiest):** re-run the setup script on a
pod that already has a DB. It detects the existing DB and offers "Add another
model (new proxy instance sharing this DB)", which launches a new instance on
the next free port (8081, 8082, …) and leaves the running instance untouched.

**Manual** — give each instance a distinct `MNEME_PORT` and the same
`MNEME_CHUNK_DIR`:

```bash
# Instance 1 — model A on 8080
MNEME_PORT=8080 MNEME_MODEL="muse-glimmer:30b" \
  MNEME_CHUNK_DIR=/workspace/mneme_chunks \
  python3 -uB proxy/mneme_proxy.py

# Instance 2 — model B on 8081, same DB
MNEME_PORT=8081 MNEME_MODEL="qwen3.6-35b" \
  MNEME_CHUNK_DIR=/workspace/mneme_chunks \
  python3 -uB proxy/mneme_proxy.py
```

Clients connect to the port for the model they want. All instances must use the
**same embedder and labeler** — the embeddings share one FAISS index with a
fixed dimension, and the startup health check will flag a dim mismatch.

## Features

**Core Memory**
- Topic-aware chunking with automatic LLM labeling
- FAISS vector search + SQLite keyword fallback (hybrid retrieval)
- Noise-floor calibration at startup (subtracts baseline from cosine scores)
- Recency-weighted scoring (cycle-based, not wall-clock)
- Source tracking (user, model, tool:*, page:*, document:*)
- Embedding reliability: startup health check probes the embedder and fails
  loudly on a dim mismatch; a failed embed is stored `pending_embed` and
  re-embedded on the next startup (no silent dead vectors)

**Learning & Strategy Layer** (experimental — this branch)
- Pass/fail/great grading: the model tags provenance (`[source: X]` / `[guess]`)
  and is graded on *honesty*, not answer correctness — "I don't know" beats
  fabrication. See `docs/grading-redesign.md`.
- Trace cross-check: any `[source: mem_XXX]` or URL the model cites is verified
  against what it actually had this turn; a fabricated citation grades fail.
- Novel-procedure detection: a working NEW technique (custom HTTP header, site
  API endpoint, method override) is detected from the tool trace, graded
  "great", and saved as a strategy with cost metadata — so the model reuses a
  trick it discovered instead of re-finding it each session.
- Strategy ranking: injected grade-first, then cheaper-wins, so a lower-cost
  technique (API JSON vs full-HTML scrape) takes the injection slot.
- Failure extraction: a D/F turn distills one imperative directive to prevent
  the same failure next time.

**Capability-Edge Tracking**
- Every graded turn records a competence edge per problem type (`compute`,
  `code`, `web_retrieval`, `memory_operation`, …). Two poor grades flag the edge;
  the next similar task injects a "propose the tool, don't grind" directive. See
  `docs/capability-edge-tracking.md`.

**Novelty & Divergence Modes** (experimental)
- `/mode/think`, `/mode/divergent`, and adversarial-collaboration endpoints that
  push the model off modal/average answers and grade novelty objectively (embedding
  distance + pairwise judge), not by self-report. See
  `docs/learning-critical-modes.md`.

**v2 Architecture (persona-free)**
- No "You are..." identity — Mneme describes itself as a system, not a personality
- `<<COMMAND>>` stripping — `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>` processed
  server-side, stripped from model context
- Content normalization — handles string and array content (OpenAI, Pi, Hermes)

**Harness Integration**
- **Hermes**: full support with all tools, memory, compression enabled
- **Pi**: `search_memory` + `web_search`/`web_scrape` via extensions
  (`extensions/pi/`), with proxy intercept + pass-through tool calls
- **Any OpenAI client**: connect to `http://localhost:8080/v1`

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

Single-file Flask proxy. Module-level state (FAISS index, SQLite connection,
staging buffer). Threaded server with daemon archival threads.

**Required Ollama models:**
- Main model (any): via `MNEME_MODEL` env var
- Labeler: `qwen2.5:0.5b` — generates topic labels
- Embedder: `snowflake-arctic-embed2` — 1024-dim vectors

## Branches

- `main` — **stable, stripped-down memory-only build** (no learning/strategy
  layer). Use this for a simple, dependable setup. Start here if you're new.
- `novelty-thinking` — **this branch.** Experimental: learning/strategy layer,
  epistemic grading, capability-edge tracking, novelty modes.
- `build-roadmap` — previous development branch (legacy).
- `dev-chunks` — previous development branch (v2 architecture).
- `dev-v2` — restore point, do not modify.

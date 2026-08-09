# Mneme

Conversational memory proxy between AI agents and Ollama. Transparent layer that archives conversations, classifies by topic, injects relevant past context, and evolves its own strategies through a self-improving feedback loop. Model-agnostic. Self-improving.

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

### Pod Setup (one command)

Run this on a fresh RunPod or any Linux machine with a GPU:

```bash
curl -sSL -o /tmp/setup.sh https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/setup.sh && bash /tmp/setup.sh
```

The interactive wizard walks through:
- Model selection (Qwen 3.6 35B, Qwen 2.5 7B/14B, or custom)
- Context window size (32K, 129K, or custom)
- Chat interface (Pi terminal agent, or proxy-only)
- Embedding model (arctic-embed2, nomic-embed-text, or custom)

After setup, the proxy is at `http://localhost:8080` with an OpenAI-compatible API at `/v1`.

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
MNEME_MODEL="qwen3.6-35b-120k:latest" \
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \
  OLLAMA_KEEP_ALIVE=24h PYTHONDONTWRITEBYTECODE=1 \
  python3 -uB proxy/mneme_proxy.py
```

For large context windows, set `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` before starting Ollama.

## Features

**Core Memory**
- Topic-aware chunking with automatic LLM labeling
- FAISS vector search + SQLite keyword fallback (hybrid retrieval)
- Noise-floor calibration at startup (subtracts baseline from cosine scores)
- Recency-weighted scoring (cycle-based, not wall-clock)
- Source tracking (user, model, tool:*, page:*, document:*)

**Self-Grading & Strategy Learning**
- Models append `[GRADE: A-F]` after every response
- Proxy parses grades from model output, stores in DB
- Proxy-driven strategy lifecycle: success (A/B) triggers mini-convo, failure (C/D/F) auto-creates boilerplate with FAISS dedup

**v2 Architecture (persona-free)**
- No "You are..." identity in system prompt — Mneme describes itself as a system, not a personality
- `<<COMMAND>>` stripping — `<<SAVE>>`, `<<DETAIL>>`, `<<REVISE>>` processed server-side, stripped from model context
- Content normalization — handles both string and array content formats (OpenAI, Pi, Hermes)

**Harness Integration**
- **Hermes**: Full support with all tools, memory, compression enabled. SEARCH_MEMORY_TOOL appended by proxy (Hermes doesn't validate tools client-side).
- **Pi**: Streaming support with search_memory via extension + proxy intercept. Extension at `extensions/pi/mneme-search-tool.ts`.
- **Any OpenAI client**: Connect to `http://localhost:8080/v1`

## Benchmarks

**LoCoMo (Long Conversation Memory) benchmark — August 2026:**

| Conversations | Questions | Model | Result |
|--------------|-----------|-------|--------|
| 1 | 5 (session summaries) | Qwen 3.6 35B (32K) | 100% (5/5) |
| 1 | 10 (individual turns) | Qwen 3.6 35B (32K) | 100% (10/10)* |

*\*Later determined LoCoMo is in Qwen's training data — results not meaningful for memory testing.*

**Custom 2026 Events benchmark (post-training-cutoff data) — August 2026:**

20 questions across 4 types: needle-in-haystack, temporal reasoning, trick questions, cross-conversation.

| Type | Score |
|------|-------|
| Needle (fact recall) | 10/15 (67%) |
| Trick (contradictions) | 2/2 (100%) |
| Cross-conversation | 0/1 (0%) |
| Temporal | 0/2 (0%) |
| **Total** | **12/20 (60%)** |

Judge: gpt-4o-mini via OpenRouter. Model: Qwen 3.6 35B (32K). 3 conversations, 20 individual turns ingested, 29 DB chunks.

Benchmark runner: `benchmarks/locomo_runner.py`

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Search memory: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/list` | List all chunks with metadata |
| GET | `/search?q=...` | GET-based keyword search |
| GET | `/detail/<chunk_id>` | Full JSON for one chunk |

## Architecture

Single-file Flask proxy. Module-level state (FAISS index, SQLite connection, staging buffer). Threaded server with daemon archival threads.

**Required Ollama models:**
- Main model (any): via `MNEME_MODEL` env var
- Labeler: `qwen2.5:0.5b` — generates topic labels
- Embedder: `snowflake-arctic-embed2` — 1024-dim vectors

## Branches

- `main` — stable release
- `dev-chunks` — active development (proxy code, v2 architecture, setup scripts, benchmarks)
- `pi` — Pi-specific extension and testing
- `dev-v2` — restore point, do not modify

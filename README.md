# Mneme

Conversational memory proxy between AI agents and Ollama. Transparent layer that archives conversations, classifies by topic, and injects relevant past context into future sessions. Model-agnostic. Multi-model. Self-improving.

## How It Works

```
Hermes (or any OpenAI client) → Mneme Proxy (:8080) → Ollama (:11434)
                                      ↕
                              SQLite + FAISS memory
```

Every conversation turn is staged, then on `/save` the proxy:
1. Classifies messages into topic groups via LLM labeling (qwen2.5:0.5b)
2. Embeds each group with arctic-embed2 (1024-dim)
3. Stores in SQLite (chunks table) + FAISS (IndexFlatIP)
4. On future requests: searches FAISS + keyword fallback, injects top matches

## Features (August 2026)

**Core Memory**
- Topic-aware chunking with automatic LLM labeling
- FAISS vector search + SQLite keyword fallback (hybrid retrieval)
- Noise-floor calibration at startup (subtracts baseline from cosine scores)
- Recency-weighted scoring (cycle-based, not wall-clock)
- Source tracking (user, model, tool:*, page:*, document:*)

**Self-Grading & Strategy Learning**
- Models append `[GRADE: A-F]` after every response
- Models create `[STRATEGY: ...]` for sub-A grades
- Proxy parses both from model output, stores in DB + FAISS
- Strategies injected as PROVEN STRATEGIES in future sessions
- Always-inject fallback: top-graded strategies always appear regardless of semantic match
- Cross-model: strategies from qwen help gemma, and vice versa

**Session Awareness**
- Auto-generated session IDs for new conversations
- Chunks tagged with session_id
- Injection headers show `[session:conv_abc]`
- Cross-session bleed as feature: multi-agent teams see each other's work
- System prompt teaches models about multi-session operation

**Multi-Proxy / Multi-Model**
- Timestamp-based chunk IDs eliminate collisions across proxy instances
- Two proxies on different ports sharing one DB confirmed working
- gemma4:26b + qwen2.5:7b tested simultaneously
- `HTTP_PORT` env var for per-instance port control

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat (also `/api/chat/completions`, `/chat/completions`) |
| POST | `/save` | Flush staging buffer to persistent storage |
| POST | `/search` | Search memory: `{"query": "...", "top_k": 3}` |
| GET | `/health` | `{"status": "ok", "chunks": N, "backend": "model"}` |
| GET | `/list` | List all chunks with metadata |
| GET | `/search?q=...` | GET-based keyword search |
| GET | `/detail/<chunk_id>` | Full JSON for one chunk |

## Quick Start (RunPod)

```bash
cd /workspace && MNEME_MODEL=qwen2.5:7b nohup python3 -uB proxy/mneme_proxy.py > /tmp/mneme.log 2>&1 &
```

For two models sharing one DB:
```bash
MNEME_MODEL=gemma4:26b HTTP_PORT=8080 nohup python3 -uB proxy/mneme_proxy.py > /tmp/gemma.log 2>&1 &
MNEME_MODEL=qwen2.5:7b HTTP_PORT=8082 nohup python3 -uB proxy/mneme_proxy.py > /tmp/qwen.log 2>&1 &
```

## Testing

Dual-model novel test verified August 2026:
1. Qwen researches topic → saves to shared DB
2. Qwen creates chapter outline → saves
3. Gemma writes Chapter 1 using qwen's research from injected memory
4. Qwen writes Chapter 2 using Gemma's Chapter 1 from injected memory
5. Gemma writes Chapter 3 using all prior content

Result: all 5 phases preserved as 5 distinct chunks in shared DB. All 11 injected facts survived across both models with zero hallucinations. Cross-model strategy transfer confirmed (qwen's verification strategies used by gemma).

## Architecture

Single-file Flask proxy (~1,900 lines). Module-level state (FAISS index, SQLite connection, staging buffer). Threaded server with daemon archival threads.

**Models needed on Ollama:**
- Main model (any): via `MNEME_MODEL` env var
- Labeler: `qwen2.5:0.5b` — generates topic labels
- Embedder: `snowflake-arctic-embed2` — 1024-dim vectors

## Branches

- `main` — stable release
- `dev-chunks` — active development (this branch)
- `dev-v2` — restore point, do not modify

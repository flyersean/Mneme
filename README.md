# Mneme

Conversational memory system for LLMs. Archives conversations, classifies by topic, and injects relevant past context into future sessions. Transparent proxy between your agent and Ollama.

Named after Mneme, the Greek muse of memory.

## Quick Start

```bash
git clone https://github.com/flyersean/Mneme.git /tmp/mneme
cd /tmp/mneme && git checkout dev-chunks && bash setup_pod.sh
```

## Architecture

```
Agent (Hermes / any OpenAI client) → Mneme proxy (:8080) → Ollama (:11434)
                                        │
                                  SQLite + FAISS
                                  (1024-dim vectors)
```

## Features

| Feature | Status |
|---------|--------|
| Streaming + non-streaming tool calls | ✓ |
| FAISS memory injection with noise-normalized routing | ✓ |
| Sequential chunk IDs (mem_1, mem_2, ...) with next-chunk hints | ✓ |
| Silent page ingestion (auto-stage + save) | ✓ |
| Dynamic topic labels from content (no fixed clusters) | ✓ |
| LLM-based semantic labeling (qwen2.5:0.5b) | ✓ |
| 500-char injection / 8000-char storage | ✓ |
| Save-cycle recency weighting | ✓ |
| Source field on all chunks (user, model, tool, page) | ✓ |
| Score normalization (baseline noise floor subtraction) | ✓ |
| Chunk+pool embedding (arctic-embed2) | ✓ |
| POST /search — FAISS search with source/cycle fields | ✓ |
| GET /list — recent 50 chunks | ✓ |
| GET /detail/<id> — full chunk content with source/cycle | ✓ |
| Memory Strategies (learned from failures, auto-injected) | ✓ |
| Multi-turn query context (last 3 user messages) | ✓ |
| Per-message character trimming (prevents CUDA OOM) | ✓ |
| Sliding window (32 messages) | ✓ |

## Hermes Integration

```yaml
model:
  default: text-mneme:64k
  provider: custom
  base_url: http://localhost:8080/v1
  api_key: none
memory:
  memory_enabled: false
  user_profile_enabled: false
```

## Endpoints

- `GET /health` — status, chunk count, model
- `POST /v1/chat/completions` — OpenAI-compatible chat
- `GET /list` — recent 50 chunks
- `POST /search` — FAISS search `{"query": "...", "top_k": 10}`
- `GET /detail/<chunk_id>` — full chunk content
- `POST /save` — force archive

## Pod Commands

```bash
bash restart_proxy.sh           # kill old proxy, start fresh
bash setup_pod.sh               # full pod setup
```

## Testing Methodology

Tests run on RunPod A40 (48GB VRAM) with Qwen 3.6 35B (Q4_K_M).

### Phase 1: Precision/Recall (API-level)
Inject known facts (Ebola, earthquake, hurricane, World Cup) via chat, then validate `/search` returns correct chunks. Tests noise rejection via random-string queries and dynamic score normalization.

### Phase 2: Break-finding
Rapid-fire injection, 5K-char messages, empty/whitespace/unicode queries, 10-way concurrent searches, save-cycle consistency, health endpoint stability. All 26 tests passing.

### Phase 3: Long-conversation (Echoes of 2026)
Novel-writing stress test — 30-turn session, 13 chapters, 58K chars. Indirect prompting ("I'm thinking about writing a novel... set in 2026"). Tests cross-chapter memory persistence and proxy stability. Model used training data predominantly — indirect prompts didn't trigger real memory injection.

### Phase 4: Targeted fiction (Fever Dreams)
Steers model toward saved topics — "WHO doctor, outbreak zone, central Africa." Model writes 38K chars explicitly citing Mbandaka, June 2026, and case numbers from stored memory. Final chapter self-audits which details came from memory vs training data.

## Results (2026-08-02)

| Test | Result |
|------|--------|
| Precision/recall API tests | 26/26 passed |
| Score normalization (noise rejection) | Nonsense queries return 0-2 results |
| Save-cycle ordering | Marburg (cycle 4) ranks above Ebola (cycle 1) |
| Cross-topic switching | All stored topics returned on targeted query |
| CUDA crash threshold | Discovered at ~4,600 chars; fixed with per-message cap |
| Long conversation stability | 30 turns, no memory leaks, no CUDA crashes |
| Indirect memory prompting | Fails — model uses training data unless prompts overlap stored topics |
| Targeted fiction injection | Works — model self-audits 7 real facts from memory |
| Cross-contamination | Story 1's fictional characters NOT pulled into story 2 |

## Known Issues

- arctic-embed2 noise floor ~0.26 — requires score normalization, limits recall of short/vague queries
- CUDA crash on Qwen 35B above ~4,600 chars total prompt — requires aggressive trimming
- Memory strategies generate noise from CUDA crash "failures" — needs real failure data
- Save endpoint can timeout during model generation (single-threaded proxy)
- arctic-embed2 can't bridge semantic gaps (e.g. "cascading disasters" ≠ "Ebola outbreak") — see Phase 3 results
- Growing conversation history currently trimmed to last 3-4 messages of 800 chars each

## Dependencies

- Ollama (chat model + snowflake-arctic-embed2 + qwen2.5:0.5b)
- Python 3.11+, Flask, FAISS, SQLite, NumPy

## License

MIT

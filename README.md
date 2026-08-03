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
| Hybrid search: FAISS + SQLite LIKE keyword fallback | ✓ |
| Sequential chunk IDs (mem_1, mem_2, ...) with next-chunk hints | ✓ |
| Silent page ingestion (auto-stage + save) | ✓ |
| Dynamic topic labels from content (no fixed clusters) | ✓ |
| LLM-based semantic labeling (qwen2.5:0.5b) | ✓ |
| 500-char injection / 8000-char storage | ✓ |
| Save-cycle recency weighting | ✓ |
| Source field on all chunks (user, model, tool, page) | ✓ |
| Session awareness — auto-generated session IDs, cross-session labeling | ✓ |
| Score normalization (baseline noise floor subtraction) | ✓ |
| Smarter history truncation (keeps task context + last 2 turns) | ✓ |
| Strategy noise filter (skips CUDA/timeout failures) | ✓ |
| Self-grading: model outputs [GRADE: A-F] after every response | ✓ |
| Strategy system: model creates [STRATEGY: ...], proxy parses and saves | ✓ |
| Knowledge classification: KNOWN/RECALLED/UNKNOWN with confidence 1-10 | ✓ |
| Chunk+pool embedding (arctic-embed2) | ✓ |
| POST /search — hybrid search with source/session/cycle/method fields | ✓ |
| GET /list — recent 50 chunks | ✓ |
| GET /detail/<id> — full chunk content with source/session/cycle | ✓ |
| Memory Strategies (learned from failures, auto-injected) | ✓ |
| Multi-turn query context (last 3 user messages) | ✓ |
| Per-message character trimming (prevents CUDA OOM) | ✓ |

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
- `POST /v1/chat/completions` — returns `session_id` for new conversations
- `GET /list` — recent 50 chunks
- `POST /search` — hybrid FAISS+keyword search `{"query": "...", "top_k": 10}`, returns `method`, `source`, `session_id`, `cycle`
- `GET /detail/<chunk_id>` — full chunk content with source, session, cycle
- `POST /save` — force archive

## Pod Commands

```bash
bash restart_proxy.sh           # kill old proxy, start fresh
bash setup_pod.sh               # full pod setup
```

## Model Behavior

### Self-Grading
After every response, the model appends `[GRADE: A/B/C/D/F]`. Grade A = drawn from memory, verified. Grade F = conflicts with memory or hallucinated.

### Strategy Learning
Model writes `[STRATEGY: what went wrong + how to fix]` when grading C or below. Proxy parses and saves to strategies table. Future sessions see PROVEN STRATEGIES in injected context.

### Knowledge Classification
Every factual answer begins with `KNOWN 9/10`, `RECALLED 7/10`, or `UNKNOWN 2/10` with confidence score. KNOWN means verified from memory or tools. RECALLED means training data only. UNKNOWN means no source.

### Session Awareness
New conversations get auto-generated session IDs (`conv_a1b2c3d4_12345`). Memory chunks are tagged with their originating session. Injection headers show `[session:conv_abc]` so the model knows where information came from. Cross-session memory is enabled — multiple agents can share knowledge in real time.

### Ambiguity Detection
Model notes contradictions between injected memory and training data. When facts conflict, memory takes priority and the model grades F.

## Testing Methodology

Tests run on RunPod A40 (48GB VRAM) with Qwen 3.6 35B (Q4_K_M) and Gemma4 26B.

### Phase 1: Precision/Recall (API-level)
Inject known facts, validate search returns correct chunks. Tests noise rejection and score normalization.

### Phase 2: Break-finding
Rapid-fire injection, edge-case queries, concurrent searches, save-cycle consistency. 26/26 passing.

### Phase 3: Long-conversation (58K-char novel)
30-turn session, 13 chapters. Cross-chapter memory persistence, proxy stability.

### Phase 4: Targeted fiction
Steers model toward saved topics — model writes 38K chars citing memory facts. Final chapter self-audits memory vs training data sources.

### Phase 5: Hybrid search validation
Keyword fallback when FAISS returns sparse results. 11/11 passing.

### Phase 6: Strategy system
Model self-grades, creates strategies, proxy parses and saves. Strategies auto-injected into future sessions.

### Phase 7: Multi-session (cross-conversation bleed)
Two simultaneous Hermes conversations sharing one memory pool. Model correctly identifies information from other sessions and adapts its worldview accordingly.

## Results (2026-08-02—2026-08-03)

| Test | Result |
|------|--------|
| Precision/recall API tests | 26/26 passed |
| Hybrid search tests | 11/11 passed |
| Score normalization (noise rejection) | Nonsense queries return 0 results |
| Save-cycle ordering | Works across multiple save events |
| CUDA crash threshold | Fixed with per-message cap + trimming |
| Long conversation stability | 30 turns, no memory leaks |
| Indirect memory prompting | FAISS fails; keyword fallback partially helps |
| Targeted fiction injection | Works — model self-audits memory facts |
| Cross-contamination | Fictional characters NOT pulled between stories |
| Strategy creation + parsing | 4 strategies saved, 2 from model self-grading |
| Session awareness | Unique IDs generated, cross-session labels in headers |
| Knowledge classification | Model outputs RECALLED 9/10, KNOWN 10/10 as appropriate |
| gemma4 vs qwen comparison | Different reasoning styles — gemma4 methodical, qwen instinctive |

## Known Issues

- arctic-embed2 noise floor ~0.26 — mitigated by hybrid keyword fallback
- CUDA crash on Qwen 35B above ~4,600 chars — per-message trimming in place
- Indirect prompting fails FAISS injection — keyword fallback helps but embedding gap remains
- gemma4 training weights strong — sometimes ignores injected 2026 data in favor of 2016 training
- Strategy injection uses problem_type matching — model strategies sometimes missed due to type mismatch
- Save endpoint can timeout during model generation (single-threaded proxy)
- Hallucination memory loop: model's wrong answers get archived as "memory" and re-injected

## Dependencies

- Ollama (chat model + snowflake-arctic-embed2 + qwen2.5:0.5b)
- Python 3.11+, Flask, FAISS, SQLite, NumPy

## License

MIT

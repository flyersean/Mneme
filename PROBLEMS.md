# Mneme — Issue Tracker

## Status: Tested — hybrid search working, indirect prompting needs work

dev-chunks branch. Tested 2026-08-02 on RunPod A40 with Qwen 3.6 35B.

## ✓ Resolved

- Streaming tool_calls, FAKE_MODEL_ID, system prompt preservation
- OpenAI/Ollama dual-format responses
- Embedding: arctic-embed2 1024-dim, chunk+pool
- Dynamic topic labels from content (no fixed clusters)
- LLM-based semantic labeling (qwen2.5:0.5b)
- Silent page staging: large tool outputs auto-staged
- 500-char chunk/injection alignment, 8000-char DB storage
- Score normalization: baseline noise floor subtraction
- Save-cycle recency weighting
- Source field on all chunks
- Sequential chunk IDs (mem_1, mem_2, ...) with next-chunk hints
- /search, /list, /detail endpoints with source/cycle fields
- Multi-turn query context (last 3 user messages)
- Per-message character trimming (prevents CUDA OOM)
- CUDA crash threshold identified at ~4,600 chars — fixed
- All three prompts aligned and trimmed to safe size
- **Hybrid search: FAISS + SQLite LIKE keyword fallback** (2026-08-02)
- **Smarter history truncation: keeps first user msg as task context** (2026-08-02)
- **Strategy noise filter: skips CUDA/timeout failures** (2026-08-02)
- **Memory Strategies: stored and injection path verified**

## Active

### P1: arctic-embed2 noise floor limits FAISS recall
Noise floor ~0.26. Dynamic calibration working but narrows effective search window. **Mitigated by hybrid keyword fallback** — short queries ("Japan", "2026") now return results via SQLite LIKE. P1 priority reduced from blocker to performance note. Root cause remains: generic embedder with dense semantic space. Options: purpose-trained embedder for better precision (not just recall).

### P2: Indirect prompting fails to trigger injection
Phase 3 test: "write a novel set in 2026 about cascading disasters" — zero real event chunks injected via FAISS. FAISS matches old story chunks (which contain "disasters" literally) but not "Ebola outbreak Mbandaka" chunks. **Keyword fallback partially helps** — "disaster" keyword matches find related content. But the embedding gap ("cascading disasters" ≠ "Ebola outbreak") remains. Phase 4 confirms targeted prompts ("WHO doctor, outbreak zone") DO trigger correct injection. See Known Issues in README.

### P3: Save endpoint timeout during generation
Single-threaded Flask can't handle concurrent requests during model generation. Save requests time out at 10s when model is actively generating. Non-critical — data is eventually saved on next flush cycle.

## Future

- Purpose-trained embedder for better semantic bridging
- Hermes configuration script for one-command Mneme setup
- Image handling
- FAISS IndexIVFFlat at scale
- Config file for user-tunable params
- Split monolith into modules
- Real failure-driven strategy generation (filtered but still unused — see P3 resolved above)

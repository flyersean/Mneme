# Mneme — Issue Tracker

## Status: Tested — targeted injection working, indirect prompting needs work

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
- Memory Strategies (stored, injection path verified)
- Multi-turn query context (last 3 user messages)
- Per-message character trimming (prevents CUDA OOM)
- CUDA crash threshold identified at ~4,600 chars — fixed
- All three prompts aligned and trimmed to safe size

## Active

### P1: arctic-embed2 noise floor limits recall
Noise floor ~0.26. Dynamic calibration working but narrows effective search window. Short queries ("Japan", "2026") often return 0 results because normalized scores fall below threshold. Mitigated by multi-turn query concatenation. Root cause: generic embedder with dense semantic space. Options: purpose-trained embedder, richer query construction.

### P2: Indirect prompting fails to trigger injection
Phase 3 test: "write a novel set in 2026 about cascading disasters" — zero real event chunks injected. FAISS matches old story chunks (which contain "disasters" literally) but not "Ebola outbreak Mbandaka" chunks. Result: model writes from training data only. Embedder can't bridge "cascading disasters" → "Ebola outbreak." Phase 4 confirms targeted prompts ("WHO doctor, outbreak zone") DO trigger correct injection. See Known Issues in README.

### P3: Memory strategies generate noise
4 strategies stored, all from CUDA crash "FAILURE" classifications. Grade B on all. Never injected because problem_type "error" never matches query type "other." Needs real failure data to produce useful strategies. Consider: auto-grade strategies based on subsequent success/failure of the same problem type.

### P4: Growing conversation history truncation
Per-message cap at 800 chars, last 3-4 messages kept. Total history ~3,200 chars. For story-length content, this means the model only sees the last few paragraphs of context. Memory injection should compensate but requires working indirect prompting (see P2).

## Future

- Purpose-trained embedder for better semantic bridging
- Image handling
- FAISS IndexIVFFlat at scale
- Config file for user-tunable params
- Split monolith into modules
- Real failure-driven strategy generation (not CUDA crash noise)
- Hybrid injection: combine FAISS with keyword matching for better recall

# Mneme — Issue Tracker

## Status: Multi-session tested — strategy system operational, session awareness live

dev-chunks branch. Tested 2026-08-02—2026-08-03 on RunPod A40 with Qwen 3.6 35B and Gemma4 26B.

## ✓ Resolved

- Streaming tool_calls, system prompt preservation
- FAISS memory injection, noise normalization, hybrid keyword fallback
- Dynamic topic labels, LLM-based semantic labeling
- Silent page staging, 500-char injection / 8000-char storage
- Save-cycle recency weighting, source field on all chunks
- Sequential chunk IDs, next-chunk hints
- Score normalization with dynamic calibration
- Smarter history truncation (task context preserved)
- CUDA crash threshold (~4,600 chars) — fixed with per-message trimming
- Strategy noise filter (skips CUDA/timeout failures)
- All three prompts aligned

### 2026-08-03 additions
- **Self-grading system**: model outputs [GRADE: A-F] after every response
- **Strategy creation + parsing**: model writes [STRATEGY: ...], proxy parses and saves to DB — 4 strategies active
- **Session awareness**: auto-generated session IDs, cross-session labeling in injection headers
- **Knowledge classification**: KNOWN/RECALLED/UNKNOWN with confidence 1-10 from old main branch merged into prompt
- **Ambiguity detection**: model instructed to note contradictions between memory and training

## Active

### P1: gemma4 training weights override injected memory
Gemma4 26B has strong training on the real 2016 Kumamoto earthquake (273 deaths). Even when 2026 earthquake data (38 deaths) is injected, the model defaults to training. Classifies correctly as "RECALLED 9/10" but doesn't use memory. Root cause: training weight dominance for high-certainty topics. Possible fixes: stronger prompt enforcement, embedding-based re-ranking, or model-specific prompt tuning.

### P2: Hallucination memory loop
Model's wrong answers get archived as chunks, then re-injected as "memory." Example: gemma4 said 2016 earthquake had 273 deaths → chunk labeled "April 2016 Kumamoto Earthquakes" → injected back → model confirms its own hallucination. Needs: source confidence scoring on chunks (user-injected facts > model-generated content).

### P3: Strategy injection problem_type mismatch
Model-generated strategies use type "model" but injection queries are typed "other" or "error." Fallback to "other" added but still misses model strategies. Fix: inject ALL recent strategies regardless of problem_type, or align model strategy type with query types.

### P4: DB WAL loss on proxy restart
FAISS index and SQLite WAL not checkpointed before `fuser -k 8080/tcp`. Chunks accumulated during session can be lost. Fix: force WAL checkpoint in /save endpoint or add graceful shutdown handler.

## Future

- Purpose-trained embedder for better semantic bridging
- Hermes configuration script for one-command Mneme setup
- Image handling
- FAISS IndexIVFFlat at scale
- Config file for user-tunable params
- Split monolith into modules
- Session ID echo-back: client stores and returns session_id on subsequent turns
- Chunk confidence scoring to prevent hallucination loop
- Per-model prompt tuning (gemma4 vs qwen need different emphasis)

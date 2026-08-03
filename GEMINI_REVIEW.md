# Gemini Architecture Review — 2026-08-03

Saved for future reference. Gemini has not seen the codebase — analysis based on system description only.

## Source Tiering (P2 fix)
Add `indexable` boolean to chunks. Only user/page/tool chunks go to FAISS. Model chunks only indexed if GRADE: A. Prevents hallucination echo loop.

## Epistemic Framing (P1 fix)
Replace generic "[MEMORY — reference only]" header with explicit override rules telling the model that injected memory overrides training data. Stronger than current prompt-based instructions.

## Strategy Injection (P3 fix)
Two options: vectorize strategies into mini FAISS index, or inject top-3 most recent strategies regardless of problem_type. Second option is one line of code.

## Graceful Shutdown (P4 fix)
Register SIGTERM/SIGINT handlers to checkpoint WAL and save FAISS index before exit. Replace `fuser -k` in restart script with `pkill -15` then fallback to `fuser -k`.

## RRF Hybrid Search
Replace current score subtraction with Reciprocal Rank Fusion — combines FAISS and keyword rankings without needing scale normalization. Better noise rejection.

## Bidirectional Session Echo-Back
Return X-Session-ID in response headers. Client echoes it back on next turn. Eliminates the "continuing conversation = default" heuristic.

## Grade-Gated Archiving
Model responses only indexed into FAISS if GRADE: A. C/D/F responses stored in SQLite for turn history but excluded from vector search.

## Strategy Vectorization
Store strategies in a separate mini FAISS index. Embed the query/error and retrieve semantically matching strategies instead of rigid problem_type matching.

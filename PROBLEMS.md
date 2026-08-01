# Mneme — Issue Tracker

## Status: Working — topic-aware archive engine live (2026-08-01)

dev-chunks branch. Tested with Qwen3.6 35B + Hermes agent.

## ✓ Resolved

- Streaming tool_calls, FAKE_MODEL_ID, system prompt preservation
- OpenAI/Ollama dual-format responses
- Embedding: arctic-embed2 1024-dim, chunk+pool
- Hallucination guard: KNOWN/RECALLED/UNKNOWN
- Force save: POST /save, <<SAVE>> chat trigger
- Large document chunking: auto-loop reads big pages silently
- Topic-aware archive engine: splits by message topic, caps at 10K
- 0.5B classifier removed — embedding-based classification instead
- Strategy generation no longer calls model
- DB storage cap: 500→8000 chars per message
- Embedding cap: 300→5000 chars per message
- Raw page text staged for FAISS archival via staging.add
- 32-message sliding window prevents predict budget exhaustion
- Empty query guard: embed("") returns zero vector
- Chunk IDs visible in injection headers (id:Topic_v1 format)
- <<DETAIL id:chunk_id>> handler working in proxy
- Web_content vectors re-embedded with _clean_content (browser wrapper stripped)

## Active issues

### P0: Stale FAISS index after re-embedding
DB vectors are clean but in-memory FAISS index loads at proxy startup.
Restart required after any re-embedding. Fix: auto-reload or incremental update.
Status: Manual restart works. Need `fuser` on pod for clean restart script.

### P1: Pod missing fuser/lsof
No way to kill process by port. `ss -tlnp | grep 8080` works for discovery
but killing requires manual PID lookup. Install `fuser` or `lsof` for a single
reusable startup command: `fuser -k 8080/tcp && proxy restart`.

### P2: ROUTE_THRESHOLD tuning
Currently 0.08. Lowered from 0.3 → 0.15 → 0.08. Spain chunks score ~0.76.
May need per-topic thresholds or dynamic adjustment as DB grows.

### P3: `_clean_content` only strips first 600 chars
Browser wrapper varies in length. Regex extraction of content after
`"result":` would be more robust than fixed-offset strip.

## Future

- Image input handling: multimodal messages pass through but memory pipeline
  assumes string content. Store image refs in chunk metadata.
- Small context model test: verify Hermes respects actual model limits.
- Config file for mneme: user-tunable params (max_history, thresholds, etc.)
- FAISS scaling: switch to IndexIVFFlat at ~10K+ chunks to avoid O(n) linear degradation.
- Split monolith: staging.py, archiver.py, faiss_engine.py, routes.py once API stabilizes.

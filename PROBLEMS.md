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

## Active issues

### P0: Browser wrapper pollutes embedding vectors
Web_content chunks have clean page data but the embedding text starts with
`<untrusted_tool_result source="browser_console">...` — 600 chars of boilerplate
before the article text. The _clean_content() strip function is in the code
but old saves don't have clean vectors. Need to re-save key pages.
Fix: re-read and re-save affected pages with clean vector code running.

### P1: <<DETAIL>> syntax not explicit in system prompt
System prompt says "the DETAIL tag" but model doesn't know exact format.
Fix: added `<<DETAIL id:chunk_id>>` example to system_prompt.md.

### P2: ROUTE_THRESHOLD may need tuning
Dropped from 0.3 to 0.15 — wider net. May need per-topic thresholds.

### P3: Recall works from conversation summaries but not raw pages
The topic-aware split puts raw page data in web_content chunks and
conversation in politics_news chunks. FAISS often selects the conversation
chunks (which have the model's summary) instead of the raw data chunks.
Root cause: browser wrapper noise in web_content vectors (see P0).

## Future

- Image input handling: multimodal messages pass through but memory pipeline
  assumes string content. Store image refs in chunk metadata.
- Small context model test: verify Hermes respects actual model limits.
- Config file for mneme: user-tunable params (max_history, thresholds, etc.)
- FAISS scaling: switch to IndexIVFFlat at ~10K+ chunks to avoid O(n) linear degradation.

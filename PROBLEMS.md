# Mneme — Issue Tracker

## Status: Working — silent page staging live (2026-08-01)

dev-chunks branch. Tested with Qwen3.6 35B + Hermes agent.

## ✓ Resolved

- Streaming tool_calls, FAKE_MODEL_ID, system prompt preservation
- OpenAI/Ollama dual-format responses
- Embedding: arctic-embed2 1024-dim, chunk+pool
- Hallucination guard: KNOWN/RECALLED/UNKNOWN
- Force save: POST /save, <<SAVE>> chat trigger
- Topic-aware archive engine: splits by message topic, caps at 10K
- Embedding-based classification (no model call)
- DB storage cap: 8000 chars per message
- Embedding cap: 5000 chars per message
- Silent page staging: large tool outputs auto-staged into chunks
- Chunk IDs visible in injection headers (id:Topic_v1 format)
- <<DETAIL id:chunk_id>> handler working
- Descriptive topic labels from page content
- 32-message sliding window
- _archive_group indentation bug fixed
- All three prompts aligned (SOUL.md, AGENTS.md, proxy system_prompt.md)
- Pod restart script at /workspace/restart_proxy.sh (fuser installed)
- Chunking disabled — model operates natively, proxy handles storage silently

## Active issues

### P1: Page recall needs full-page saves
Model reads pages but only 50K chars via browser_console .slice(0,50000).
Pages > 50K lose content. Silent staging saves what's available but
model needs to extract > 50K by using offset to get remaining text.
Can be addressed by system prompt instruction to re-extract with offset.

### P2: FAISS scaling
IndexFlatIP is O(n). At ~10K+ chunks, switch to IndexIVFFlat.
Current: ~40 chunks — not urgent.

## Future

- Image input handling
- Small context model testing
- Config file for user-tunable params
- Split monolith into modules once API stabilizes

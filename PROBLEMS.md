# Mneme — Issue Tracker

## Status: Architecture redesign — dynamic topics + injection alignment

dev-chunks branch. Pod offline.

## ✓ Resolved

- Streaming tool_calls, FAKE_MODEL_ID, system prompt preservation
- OpenAI/Ollama dual-format responses
- Embedding: arctic-embed2 1024-dim, chunk+pool
- Hallucination guard: KNOWN/RECALLED/UNKNOWN
- Force save: POST /save, <<SAVE>> chat trigger
- Topic-aware archive engine: splits by message topic
- Embedding-based classification (no model call)
- Silent page staging: large tool outputs auto-staged
- <<DETAIL id:chunk_id>> handler
- Descriptive topic labels from page content
- 32-message sliding window
- All three prompts aligned
- /search and /list endpoints
- /detail/<chunk_id> endpoint
- Memory Strategies instructions in system prompt

## Active — P0: Code review findings

### 1. save_chunk drops messages (`messages[-6:]`)
Line 401: Only saves last 6 messages per chunk. Multi-page archives silently lose content.
Fix: Remove [-6:] cap or raise to 50.

### 2. build_context injects 300 chars per message
Line 672: `m["content"][:300]` — supposed to be 1500. Model sees 300-char snippets
of 1500-char chunks. Huge blind spot for recall.
Fix: Match chunk size (set to 300-500).

### 3. _trim_chunks also uses 300-char truncation  
Line 605: Token estimation uses truncated text — inaccurate budget.
Fix: Match chunk size.

### 4. Topic dedup regex broken
`re.sub(r'_v\d+$', '', cid)` strips "v1" but not "p24" in "politics_news_p24_v1".
Every chunk gets unique topic — dedup is useless.
Fix: Strip internal numbering too, or use topic_label from DB directly.

### 5. Fixed 12-cluster _classify_message → "other" for everything new
Ebola, hurricanes, earthquakes, Nvidia earnings — all map to "other".
No new topics can form. Topic count is frozen.
Fix: Replace with _generate_topic_label (content-derived labels).

## Architecture plan (to implement)

### A. Dynamic topic labels
Replace `_classify_message` with `_generate_topic_label` in `_topic_split`.
Labels derived from actual content words. New domains auto-create new topics.
No "other" bucket. Unlimited topic growth.

### B. Smaller chunks = more injection diversity
Chunk size: 300-500 chars. 10-15 chunks fit in 4K token budget vs 2-3.
Cross-topic synthesis gets multiple sources.

### C. Injection matches chunk size
build_context, _trim_chunks, save_chunk all use same cap as CHUNK_SIZE.
No truncated snippets. Model sees full chunk or nothing.

## Future

- Image handling
- Small context model testing
- Config file
- FAISS IndexIVFFlat at scale
- Split monolith into modules

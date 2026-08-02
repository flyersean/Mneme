# Mneme — Issue Tracker

## Status: Architecture implemented — performance issues found

dev-chunks branch. Pod offline.

## ✓ Implemented (by Kimi)

- Dynamic topic labels: _generate_topic_label replaces 12-cluster _classify_message
- 500-char chunk/injection alignment: CHUNK_SIZE=500, COMPRESS_THRESHOLD=500
- build_context, _trim_chunks, save_chunk all use CHUNK_SIZE
- save_chunk [-6:] truncation removed
- Topic dedup fixed: uses DB topic_label instead of regex on chunk_id
- All three prompts updated (SOUL.md, AGENTS.md, system_prompt.md)

## P0: Performance — proxy slows to crawl after several turns

Found during code review of dev-chunks branch.

### 1. O(n²) DB thrashing in build_context
- Line 655: `sorted(all_ids, key=lambda c: (-grade_priority(c), c))` — grade_priority() runs SQLite per chunk. With 100 sibling chunks, that's 100 DB queries just for the sort key.
- Line 686: load_chunk() called for every sibling during struct_ref scanning. Another query per chunk.
- Each call to load_chunk deserializes JSON messages — with many small 500-char chunks, this scales poorly.

### 2. Duplicate _advance_chunk call
Lines 1411 and 1414 both call _advance_chunk(messages). Waste since it's a no-op pass-through, but indicates stale cleanup.

### 3. _model_loop_read_all guard always false
Line 1427: `getattr(compress_large_tool_results, '_buffer', {})` — the current compress_large_tool_results stores `_staged_hashes`, not `_buffer`. The `_buffer` attribute only exists on the _OLD function. The loop guard always sees an empty dict. Dead code.

### 4. Exponential sibling growth from dynamic labels
500-char chunks × dynamic topic labels = many unique topics. A 50K page creates ~100 chunks with ~100 unique labels. route_query returns top-3, get_siblings loads ALL per topic. With 100 topics of 1 sibling each, that's 300 DB queries in build_context. Over multiple turns, the DB grows linearly but query time grows quadratically.

## Future

- Fix DB thrashing: batch load chunks, cache grade_priority
- Consolidate topic labels to reduce sibling explosion
- FAISS IndexIVFFlat at scale
- Image handling
- Config file
- Split monolith into modules

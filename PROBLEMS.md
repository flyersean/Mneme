# Mneme — Issue Tracker

## Status: Cross-model collaboration verified, persistence bugs resolved

dev-chunks branch. Tested August 3-4, 2026 on RunPod A40 with qwen2.5:7b + gemma4:26b.

## Resolved (August 2026 session)

### P0: Chunk ID collision across proxy instances [RESOLVED]
- **Cause:** `_chunk_seq` was per-process, restarting at 0. Two proxies on same DB produced identical `mem_1`, `mem_2` IDs.
- **Fix:** Timestamp-based chunk IDs (`mem_{int(time.time()*1e6)}`). Guaranteed unique across any number of proxies.
- **Verified:** 5-phase dual-model novel test — all 5 chunks survived, zero collisions.

### P0: `chunk_id=""` passed to save_chunk [RESOLVED]
- **Cause:** `_archive_single_chunk` computed correct ID but passed empty string.
- **Fix:** Thread generated `chunk_id` through to `save_chunk`.

### P0: INSERT placeholder count mismatch [RESOLVED]
- **Cause:** Schema migration added columns (session_id, indexable) but INSERT VALUES had wrong count.
- **Fix:** Aligned 15 placeholders with 15 values.

### P0: Column order misalignment [RESOLVED]
- **Cause:** INSERT VALUES order didn't match schema column order after ALTER TABLE migrations.
- **Fix:** Explicit column ordering: source, cycle, created_at, session_id, indexable.

### P1: Grade pipeline [RESOLVED]
- **Cause:** Grades parsed from model output but never written to DB.
- **Fix:** Full pipeline: model output `[GRADE: X]` → process_chat parsing → staging → archive → save_chunk → DB.
- **Verified:** gemma4 outputs `[GRADE: A]`, DB shows `grade=A`.

### P1: Strategy type mismatch [RESOLVED]
- **Cause:** Strategies keyed on `problem_type` string matching — never matched in practice.
- **Fix:** Always inject top-3 highest-graded strategies regardless of problem_type. FAISS semantic match as bonus.
- **Verified:** Cross-model strategy transfer confirmed (qwen strategies injected into gemma context).

### P2: Hallucination memory loop [MITIGATED]
- **Source-tiered indexing:** model-sourced chunks with grade C/D/F excluded from FAISS.
- **Combined trust scoring:** source + grade weights in `route_query` ranking.
- **Not fully eliminated:** gemma4 still sometimes defaults to training data over injected memory.

### P3: Stale DB chunks on restart [RESOLVED]
- **Cause:** WAL not checkpointed before proxy kill.
- **Fix:** SIGTERM handler with `PRAGMA wal_checkpoint(TRUNCATE)`.

### P4: Dead code (~400 lines) [RESOLVED]
- **Removed:** `classify_chunk`, `_archive_split`, `_segment_by_user` orphan, `_compress_large_tool_results_OLD`, `_advance_chunk_OLD`, `_model_loop_read_all_OLD`, `query_model_stream`, `CLASSIFY_PROMPT`.
- **Impact:** 326 lines removed. Code from 2,214 → 1,888 lines.

## Active Issues

### P2: Training weight dominance (persistent)
Models with strong training data on a topic (e.g., 2016 Kumamoto earthquake) ignore injected 2026 data. Known across all models. Epistemic framing in system prompt helps but doesn't fully solve.

### P2: Concurrent writer safety (Kimi Bug #2-3)
Single SQLite connection shared across Flask threads + background archival threads with no write lock. Rare under low concurrency but will corrupt under load. Fix: `_db_lock` or single-writer queue.

### P2: FAISS ghost vectors on REPLACE (Kimi Bug #5)
When INSERT OR REPLACE fires (legacy collision case), FAISS holds old + new vectors for same chunk_id. Fixed by timestamp IDs eliminating collisions, but _id_map doesn't deduplicate on reload.

### P3: Deterministic topic labels (Kimi Bug #6)
LLM labeler with `temperature=0.0` produces identical labels for similar content. Combined with `_merge_small_groups`, different conversations get merged into same topic group. Low impact with timestamp IDs but semantically ugly.

### P3: Streaming dead code
`_chat_stream()` buffers full response then re-chunks into 16-char SSE deltas — fake streaming. True SSE generator `query_model_stream()` exists but is unused. TTFB = full generation latency.

### P4: Single-file monolith
1,888 lines in one module. Module-level mutable state shared across Flask threads. Would benefit from splitting into storage/embedding/routing/injection/HTTP modules.

## Not Issues (verified correct)

- WAL journaling works — `db.commit()` persists correctly.
- Cycle counter increments per flush — correct.
- `/save` returns proper error codes — not swallowing failures.
- FAISS `_idx_lock` covers read + write paths — thread-safe for this code path.

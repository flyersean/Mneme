# Mneme Fix Plan — August 2026

Work branch: dev-chunks ONLY. dev-v2 is restore point — do not touch.

All three code reviews (Kimi, GPT-5.6, Opus 5) agree on priorities.
Ordered by dependency: each fix unlocks the next.

## Phase 1: Fix Persistence (Bug A + Bug B)

### 1A. Fix chunk_id — stop overwriting chunks [CRITICAL, 30 min]
- Problem: `_archive_single_chunk` passes `chunk_id=""` to `save_chunk`
- `INSERT OR REPLACE` on empty PK keeps ONE row forever
- Fix: thread generated `chunk_id` through to `save_chunk`
- Delete existing DB and rebuild (it's corrupted by construction)
- Verify: inject 3 facts → `/list` shows 3+ distinct chunks

### 1B. Fix column misalignment [15 min]
- Problem: INSERT passes values in wrong order after session_id migration
- session_id string → cycle column, cycle int → created_at, created_at → session_id
- Fix: use explicit column names in INSERT statement
- Fix: add migration to repair any existing broken rows

## Phase 2: Fix Grading (make grade-priority system real)

### 2A. Parse [GRADE: X] from model responses [30 min]
- Problem: model outputs `[GRADE: A]` but proxy never extracts it
- Fix: parse grade alongside existing `[STRATEGY:]` parser in route handler
- Store grade on assistant chunk: `UPDATE chunks SET grade=? WHERE chunk_id=?`
- Only update if grade is A/B/C/D/F (defensive regex)
- Default grade for un-parsed chunks: D (conservative — assume unverified)

### 2B. Fix grade display in injection [15 min]
- Problem: injection headers show topic but not grade or source
- Fix: add grade and source to header: `[mem_N src:user grade:A] topic`
- Model can see trust signals directly

## Phase 3: Source Trust (kill hallucination loop P2)

### 3A. Source-tiered FAISS indexing [30 min]
- Problem: model hallucinations archived and re-injected as memory
- Fix: add `indexable` boolean column to chunks
- user/page/tool chunks → indexable=True (always go to FAISS)
- model chunks → indexable=True only if grade in (A, B)
- model chunks with grade C/D/F → indexable=False (SQLite only, no FAISS)
- `_load_index` respects `indexable` flag on rebuild

### 3B. Source + grade combined trust in ranking [15 min]
- Problem: all chunks ranked identically regardless of provenance
- Fix: `trust = source_weight + grade_weight`
- source_weight: user=0.4, page=0.3, tool=0.2, model=0.0
- grade_weight: A=0.4, B=0.3, C=0.1, D=0.0, F=0.0
- Trust applied in `combined()` as boost factor: `sim * (0.7 + 0.3 * trust)`
- Model-sourced A-graded chunks still get injected (trust=0.4)
- Model-sourced F-graded chunks blocked entirely (indexable=False)

## Phase 4: Strategy System (enable learning)

### 4A. Strategy embedding retrieval [1 hr]
- Problem: strategies keyed on rigid `problem_type` string — never matches
- Fix: embed strategies into same FAISS index as special chunks (`strat_*` prefix)
- At injection time: search FAISS for matching strategies alongside chunks
- Keep strategies in injection under PROVEN STRATEGIES header
- Remove `problem_type` exact-matching — it's the P3 bug

### 4B. Strategy cross-model learning [30 min]
- Problem: strategies typed "model" never matched query types
- Fix: with embedding-based retrieval, all strategies match regardless of origin model
- gemma4 strategies retrieved when qwen asks, and vice versa
- Verify: create strategy with gemma4 → restart with qwen → strategy appears in injection

## Phase 5: Cleanup + Stability

### 5A. Dead code removal [30 min]
- Remove: `_archive_split`, `_archive_single`, `_segment_by_user` orphan
- Remove: `_compress_large_tool_results_OLD`, `_advance_chunk_OLD`, `_model_loop_read_all_OLD`
- Remove: `classify_chunk` (has `msgs` NameError), `query_model_stream` (dead)
- Remove: shadow `import re` in hot paths (already at module level)
- Verify: `python3 -m py_compile` + tests still pass

### 5B. Graceful shutdown [30 min]
- Problem: `fuser -k` sends SIGKILL, WAL lost, staging buffer lost
- Fix: SIGTERM handler: checkpoint WAL + flush staging + save FAISS
- Fix: `restart_proxy.sh`: `pkill -15` first, then `fuser -k` fallback after 3s

### 5C. Unify injection budget [30 min]
- Problem: 2048-token cap + 3000-char scissors fighting each other
- Fix: single token budget, enforce in `_trim_chunks`, remove `context[:3000]` guillotine
- Trim whole chunks, never mid-chunk

## Phase 6: Test

### 6A. Re-run precision/recall test battery
- All 26 tests from Phase 1-2
- All 11 hybrid search tests
- Verify: results should be DIFFERENT (actual multi-chunk DB now)

### 6B. Strategy cycle test
- Inject facts → model self-grades → create strategies
- Restart with different model → verify strategies injected
- Verify cross-model learning (gemma strategies help qwen)

### 6C. Hallucination loop test
- Model says something wrong → archived → ask again → should NOT confirm
- Verify: source/model chunks with low grades excluded from FAISS

### 6D. Graceful shutdown test
- Proxy running with active chunks → `pkill -15` → restart → chunks survive
- Verify: chunk count before and after restart matches

## Dependencies

```
Phase 1 (persistence) → Phase 2 (grading) → Phase 3 (trust) → Phase 4 (strategies)
                                                                   ↓
                                              Phase 5 (cleanup) ← Phase 6 (test)
```

## What we DON'T touch

- dev-v2 branch (restore point)
- Chunk sizes, embedding model, noise calibration
- System prompt (already good after merge)
- Agent interface (Hermes config)
- Session ID generation (functional, improve later)
- Streaming path (defer to future session)
- Monolith split (defer to future session)
- RRF hybrid search (premature per all three reviewers)

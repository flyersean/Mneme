# Cross-Review Comparison — August 2026

Three independent architectural reviews: Kimi (K3), GPT-5.6 Sol, Claude Opus 5.
Gemini had README + PROBLEMS only (no code access).

## Bug A (chunk_id="") — all three found it independently

| Reviewer | Found? | Severity |
|----------|--------|----------|
| Kimi     | Yes    | CRITICAL — "DB never holds more than ONE chunk" |
| GPT-5.6  | Yes    | CRITICAL — "retrieval always returns the latest chunk" |
| Opus 5   | Yes    | Implicit in review (noted in save_chunk flow) |
| Gemini   | No     | Couldn't see code |

Triple confirmation. This is the #1 fix.

## Bug B (column misalignment) — Kimi only

Only Kimi traced the exact INSERT column order mismatch where `session_id` lands in the `cycle` column. GPT and Opus didn't catch the ordering issue specifically.

## Agreement Points (all three agree)

1. **Fix chunk_id first** — everything else depends on it
2. **Source-tiering** — gate FAISS indexing on source, not model self-grade
3. **Graceful shutdown** — SIGTERM handler + WAL checkpoint for P4
4. **Session echo-back** — client should store and return session_id
5. **Dead code cleanup** — ~400-500 lines of corpses
6. **Double budget** — 2048-token cap + 3000-char scissors fighting each other

## Unique Insights Per Reviewer

**Kimi:**
- Column misalignment (Bug B) — session_id/int/created_at scrambled
- MEMORY_DISCLAIMER contradicts system_prompt (reference-only vs authoritative)
- _load_index re-adds stale vectors on restart
- query_model drops tool-call/response pairs

**GPT-5.6 Sol:**
- Streaming is fake — buffers then re-chunks; real SSE generator dead code
- Strategy regex truncates on `]` — breaks strategies with bracket chars
- Grades never parsed — entire grade-priority system is a no-op
- MAX_PROMPT_CHARS defined 400 lines after use
- 800-char tool truncation is likely real "indirect prompting" failure cause
- 10-item ranked fix list with effort estimates

**Claude Opus 5:**
- Source-prior in combined() ranking — use source as ranking feature, not just label
- _segment_by_user orphan — function body with no `def` at module indent
- query_model silently drops middle turns without telling model
- _keyword_search LIKE patterns unescaped
- Concurrency: single writer queue is the right long-term fix
- Trust model: strategies should be retrieved via embedding, not rigid problem_type

## Disagreements

| Topic | GPT-5.6 | Opus 5 | Gemini |
|-------|---------|--------|--------|
| RRF fusion | Premature | Not mentioned | Recommended |
| Strategy vectorization | Not needed | Via same FAISS index | Mini FAISS |
| Grade-gated archiving | Gate on source, not grade | Use source as ranking prior | Gate on GRADE: A |
| Fix order | Bugs → grading → shutdown | Hygiene → concurrency → injection → trust | Not prioritized |

## Bottom Line

Fix Bug A first. Then source-tiered indexing. Then parse grades. Then everything else. The system has been operating with a 1-chunk database — all test results need revalidation after the fix.

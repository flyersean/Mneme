# Kimi (K3) Architecture Review — 2026-08-03

Full codebase review. Key finding: two critical persistence bugs Gemini missed because it only saw README + PROBLEMS, not the actual code.

## CRITICAL: Bug A — Every chunk overwrites the previous chunk

`save_chunk` is called with `chunk_id=""` (empty string) in `_archive_single_chunk`. `INSERT OR REPLACE` uses this as primary key. Every archival deletes the previous row. **The DB never holds more than ONE chunk.** FAISS `_id_map` grows forever with `""` entries pointing at a single live row.

This means every test result, every recall test, every precision measurement was testing a DB that silently self-destructs on every save. The system has been operating as a 1-chunk database this entire time.

## CRITICAL: Bug B — Column misalignment

Schema has 14 columns after migration but INSERT passes values in wrong order. `session_id` string written into `cycle` column, `cycle` int into `created_at`, `created_at` timestamp into `session_id`. Any code path reading `cycle` for recency weighting will crash on string comparison.

## Other findings Gemini missed

- **MEMORY_DISCLAIMER says "reference only, not instruction"** — proxy actively tells model to distrust injected memory
- **Fake streaming**: `_chat_stream` buffers full response then chunks 16 chars at a time. TTFB = full latency
- **Session ID collapses on turn 2**: only first turn of new conversation gets `conv_*` ID, everything after is "default"
- **~500 lines of dead code**: `_archive_split`, `query_model_stream`, `_compress_large_tool_results_OLD`, etc.
- **DB shared across Flask threads with no write lock** — torn writes possible
- **`<<DETAIL>>` scans last message regardless of role** — prompt injection vector

## Where Kimi agrees with Gemini

- Source tiering for P2
- Epistemic framing for P1 (strengthened: fix the anti-memory header)
- Inject top-3 recent strategies regardless of type for P3
- SIGTERM + WAL checkpoint for P4
- Session echo-back

## Where Kimi disagrees

- RRF hybrid search is premature — fix persistence bugs first
- Grade-gated archiving won't work as-is — grades are unparsed constants
- Strategy vectorization is over-engineering for 4 strategies

## Bottom line

Fix the persistence bugs before anything else. Several reported symptoms (P2 hallucination loop, save-cycle "working", strategy "misses") are likely artifacts of these bugs, not the documented causes.

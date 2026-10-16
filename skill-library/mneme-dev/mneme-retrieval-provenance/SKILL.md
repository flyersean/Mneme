---
name: mneme-retrieval-provenance
description: Diagnose "fact is in memory but search won't find it" and "model fabricates memory citations" — search forensics + provenance grading + poisoning guard.
tags: [mneme, memory, retrieval, search, provenance, grading, fabrication]
---

# Mneme retrieval & provenance integrity

## Search failures — verify against the log/DB, NOT the model's self-report
Models HALLUCINATE tool failures AND outages. When a model claims "search returned no matches" or "the embedder is down / 50% pending", check ground truth first:
- The DB directly: `bash` + `python3 -c "import sqlite3; ..."` to check whether the fact is present and the pending/embedded counts.
- The proxy log for `[SEARCH-TOOL]` lines (what was searched, method, result count). If the log shows "found N results" every time but the model claims "no matches", the model confabulated.

## Common root causes
- Keyword fallback is a BOOLEAN flag (default off) that only gates the AUTOMATIC fallback; `method="keyword"` always works. "keyword disabled" is the wrong diagnosis.
- The search tool returned head-of-chunk previews capped at 30 lines — the model physically could NOT read all chunks (skimming was forced). Fix: query-biased snippets (`_query_snippet` returns the window around the matched term) + explicit count in the result header + remove the 30-line cap.
- Codify "read ALL returned chunks (or refine the query if N is large, e.g. >50)" STRUCTURALLY — not via a prompt the model forgets. The durable fix is the query-biased snippet: the fact is either visible or not, independent of how carefully the model reads.

## Provenance grading (fabricated citations)
- `_has_fake_source` (trace cross-check in `proxy/mneme/grading.py`) validates `[source: mem_XXXX]` citations against what was actually retrieved this turn — it catches fabricated citations and grades the turn F.
- The gap that mattered: the F grade was logged but never surfaced. Fix: mechanically append `[GRADE: F — fabricated or unverifiable citation]` to the reply (models report their own grade inconsistently — never rely on them to).
- Poisoning guard: flag chunks that cite an F-graded/retracted chunk (proactive at archive via `flag_if_cites_poisoned`, retroactive via `flag_poisoned_descendants` on flag_bad_memory). Non-destructive (proposed_retract for human review).

## Pitfalls
- The grade is post-hoc (computed on the turn, stored on the saved chunk) — it can't stop mid-turn fabrication, only surface it. The mechanical reply-tag is what makes it visible.
- When retracting, feed "un-cited" not "F/fabricated" — the wording matters to the model.
- `derived_from` (citations) and `injected_chunk_ids` (what was in context) are the two provenance signals; `curation.lineage()` walks both transitively.

## Verify
- `bash`: grep the instance log (`chunks/instances/8080/restart.log` or `proxy-8080.log`) for `[FAKE-SOURCE]` / `[GRADE] fail: F`.
- `bash`: query the DB for the fact's chunk and its `grade`/`derived_from`.
- Use `search_memory` + `read_chunk` yourself to confirm a fact is (or isn't) retrievable before trusting a model's claim about it.

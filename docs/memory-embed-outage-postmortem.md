# Postmortem — memory embedder outage (2026-10-10 → 2026-10-14)

## TL;DR

For five days the embedder (`qwen/qwen3-embedding-8b` via OpenRouter) was failing on
every call (HTTP 401 and 400), so new memory chunks were **saved but never embedded**.
Unembedded chunks are invisible to semantic search, so Mneme was effectively running with
most of its memory turned off — and it never surfaced this anywhere. The symptom was a
gradual slide into hallucination (answering from no real recall), followed by a hard crash
(HTTP 500 on every chat message) caused by a latent FAISS bug that the outage exposed.

## Timeline

| Date | Embedding health |
|------|------------------|
| Oct 09 | 489 embedded / 154 pending |
| Oct 10 | 0 embedded / 278 pending  ← embedder starts failing (401/400) |
| Oct 11 | 0 embedded / 647 pending |
| Oct 12 | 0 embedded / 1,248 pending |
| Oct 13 | 0 embedded / 26 pending |
| Oct 14 | 0 embedded / 25 pending |
| Oct 15 | 10 embedded / 109 pending (recovering) |
| Oct 16 | working — backlog draining |

At the worst point ~2,594 of ~2,987 chunks (~87%) were `pending_embed=1` (unembedded).

## Root cause

The embedder calls (OpenRouter `qwen/qwen3-embedding-8b`) started returning 401
Unauthorized and 400 Bad Request on Oct 10 and stayed broken for five days. Chunks are
saved transactionally regardless of embed success, so the pipeline kept *storing* memory
while silently failing to make it *searchable*. The outage was an upstream/provider failure;
the damage was the **silent accumulation** of a huge unembedded backlog.

## Why it went unnoticed for five days

- `/health` hard-coded `"ok"` and never reported embed health.
- `/status` was in-memory only (no pending-embed counter).
- The embed health probe only ran at startup and printed to the log — never surfaced to a
  human or to the model.
- The model had no tool to ask "is my memory healthy?".

In other words: the system had a heartbeat, but the heartbeat was lying.

## Symptoms — how it degraded the model

Broken memory injection caused hallucination through three distinct modes:

1. **Keyword-noise during the outage.** Semantic search returned nothing, so retrieval fell
   back to keyword matching — noisy, low-precision results injected as if they were recall.
2. **~87% missing memory after recovery.** Once the embedder came back, only the ~13% of
   chunks that had ever been embedded were reachable by semantic search. The model was
   answering from a fraction of its actual memory.
3. **Total retrieval failure (the crash).** During the re-embed catch-up, a latent bug in
   `_cosine_search` raised `IndexError` on empty FAISS results, returning HTTP 500 on every
   chat message — memory injection went from "degraded" to "gone".

## Bugs found and fixed

1. **FAISS empty-result IndexError** (`proxy/mneme_proxy.py`, `_cosine_search`). When the
   FAISS search returned no rows (which happened constantly during re-embed churn), the code
   indexed `scores[0]` / `idxs[0]` blindly → `IndexError: list index out of range` → HTTP 500.
   Fix: return `[]` on empty results, and skip any index row whose id is out of range
   (covers `_index` / `_id_map` desync).

2. **Re-embed backlog never drained** (`_reembed_pending(limit=200)`). Only 200 chunks were
   re-embedded per startup, so a 2,500-chunk backlog could never catch up — every restart
   re-did the same first 200. Fix: loop until the backlog is zero, so an embedder outage
   self-heals once the provider recovers.

## Fixes applied

- `53f801d` fix(proxy): guard empty FAISS search results; drain full re-embed backlog
- `7f5b549` feat(health): surface memory/embedder health on /health + /dashboard/status;
  add mneme_health model tool
- `7d193d4` feat(health): add visual health page at /health/ui + nav link

## Prevention — the new health system (so this can't hide again)

- `/health` and `/dashboard/status` now report memory health: total / embedded / pending /
  % unembedded, plus per-backend (embed/chat/label) ok/error status.
- A `mneme_health` **model tool** lets the agent ask "is my memory healthy?" and report
  DEGRADED state instead of silently fabricating.
- A visual page at `/health/ui` shows the same live.

The re-embed backlog is drained by a full-sweep loop, so the next provider outage degrades
gracefully and recovers on its own — and now it's visible the moment it starts.

## Still deferred (not done)

- A persistent embed-failure counter + watchdog cron that alerts a human when the embedder
  has been failing beyond a threshold. This postmortem's "why it went unnoticed" list is the
  spec for that future work.

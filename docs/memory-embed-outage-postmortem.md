# Postmortem: 5-day Embedder Outage (Oct 10-14)

## Summary

For approximately five days (Oct 10 - Oct 14), the embedding backend
`qwen3-embedding-8b` via OpenRouter returned 401/400 errors for every
request. No new or backlogged chunks could be embedded. Semantic search
silently degraded to keyword-only fallback, and health checks continued
to report "ok" throughout, so the outage went undetected until manual
inspection.

## Impact

- ~2,594 of 2,987 chunks (~87%) were left unembedded at the peak of
  the backlog. Semantic queries matched only the embedded minority plus
  keyword matches, producing noisy, off-topic context injections.
- Nobody noticed for five days because the health endpoint did not
  check the embedder at all.

## Root cause

1. **Backend failure:** `qwen3-embedding-8b` via OpenRouter started
   returning 401/400 responses on Oct 10 and continued through Oct 14.
2. **Health blind spot:** `/health` returned `ok` without checking the
   embedder backend, so a total embedding outage was invisible.
3. **Keyword-search fallback masked the failure:** retrieval fell back
   to keyword matching, which still returned *some* results - noisy
   injection instead of an obvious error.
4. **Crash on empty FAISS results:** `_cosine_search` raised an
   `IndexError` when the FAISS search returned no results. **Fixed:**
   `_cosine_search` now returns an empty result set gracefully.
5. **Backlog never drained:** background re-embedding was capped at
   200 chunks per startup, so whenever the backlog exceeded 200 (it was
   in the thousands), each restart re-embedded a small slice and never
   caught up. **Fixed:** re-embed now performs a full drain of the
   pending backlog.

## Fixes now in place

- `/health` reports total / embedded / pending chunk counts and
  per-backend status (so an embedder outage is visible immediately).
- The `mneme_health` tool exposes the same figures to the agent
  (total/embedded/pending, plus embedder/chat/labeler backend status).
- `/health/ui` page exists for human inspection.
- `_cosine_search` empty-result crash fixed (graceful empty return).
- Re-embed backlog drain is no longer capped.

## Deferred

- Proactive watchdog alerting (actively notifying on backend failures,
  rather than just reporting via `/health`) is deferred to a future
  cycle.

## Detection & Timeline

- The outage went unnoticed for its full five-day duration because
  `/health` returned `ok` regardless of embedder state.
- Detected Oct 16, via manual inspection of the memory figures after the
  `mneme_health` tool and health figures landed — not by any automated
  signal (none existed).
- Discovered state at detection: ~2,594 of ~2,987 chunks (~87%)
  unembedded, the backlog having grown across restarts.

## Residual gaps (recorded for honesty)

- The drain is still *startup-only*: `_reembed_pending` is enqueued once
  at startup. If the embedder recovers without a restart, the backlog
  sits until the next restart. The 200 limit is now per-batch, not per
  startup, but a mid-flight recovery does not self-heal.
- Over-length text can leave chunks `pending_embed` silently even when
  the embedder is healthy — an outage-compounding behavior.
- `embed.at=""` right after startup means "never attempted", which is
  indistinguishable from healthy on the first request.
- Deferred: proactive watchdog alerting on backend failures.

## Verification (as of Oct 16)

- `mneme.db` (main instance A): total=3126, pending=0 — backlog drained.
- `chunks/dev/mneme.db` (instance B): total=37, pending=0 — drained.
- Reproduction recipe for the original blind spot: point `embed_backend`
  at an invalid key, restart, and confirm `/health` now shows
  `embed.ok: false` and `pending > 0`.

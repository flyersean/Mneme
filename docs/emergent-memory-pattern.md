# Emergent Memory Pattern — Files-as-Artifacts + Mneme-as-Process-Thread

Observed (not designed), August 15 2026. In a real-world coding session the
model split its own memory across two tiers unprompted. Status: single-session
observation — needs replication on other project shapes before it's treated as
a stable pattern.

## The observation

In a simple coding session the model did three things on its own:

1. Saved the spec and the code to files via Pi's write/bash tools.
2. Used Mneme's `search_memory` to remember what the project is and where the
   process had left off.
3. On a later session, searched Mneme to locate those files, then read them
   back for the full content.

The division of labor that fell out:

- **Files = artifacts.** The spec and code — the "what we built."
- **Mneme = process thread.** The project's identity and progress state — the
  "where we were and what comes next."
- **`search_memory` = the index.** The lookup that points from a vague
  recollection to the right file on disk.

## Why this matters

It's the correct two-tier architecture (fast semantic index → durable bulk
storage), and the model arrived at it without anyone wiring it up. It also
names Mneme's real value: continuity, not content storage. A file tells you
the spec exists; it does not tell you "the spec is done and we were mid-parser
when we stopped." That temporal/process state is exactly what a pile of files
cannot reconstruct, and it's what the model used Mneme for.

This is a payoff for machinery that has been in the codebase for a while — the
date-prefix on embeddings, `session_id` tracking, cycle-based recency, and
topic labels — all of which exist to capture "where we were in the process"
rather than just "what was said."

It's also a data point for the learning thesis: the model built a
self-organizing memory (index + store) as emergent behavior, not from explicit
instruction.

## What still needs testing

- Only one coding session so far. Repeat with other project shapes: research,
  multi-file refactors, long-running multi-day work, non-coding tasks.
- Does the every-turn injection still add value, or is `search_memory` +
  file-read doing the heavy lifting now that the model has a working
  file-index loop? Watch whether the model cites `[source: mem_XXXX]` (injected
  chunk) vs. a file path.
- Related: this pattern may have been seeded by the `<<SAVE>>` leak (P12),
  where the model interpreted the command as "write the convo to files." That
  side effect produced the artifacts this loop now re-finds.

## Related

- PROBLEMS.md P12 — bare `<<COMMAND>>` leak (the `<<SAVE>>` behavior that seeded
  the file-writing).
- The `search_memory` → synthesis pipeline (proxy re-queries the model with
  search results so it produces a tagged answer).

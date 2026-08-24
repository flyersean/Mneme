# Strategy Retrieval & Categorization — Source-Chunk Linkage

Branch: `unified_mneme`
Status: SPEC (pre-implementation). Supersedes the `problem_type` taxonomy described
in `docs/strategy-roadmap.md` §2 as the retrieval mechanism for strategies.

---

## Why this exists

Strategies are currently categorized with a hand-written keyword taxonomy
(`_classify_problem_type` -> error / code / compute / live_data / web_retrieval /
memory_operation / other) and retrieved by exact string-equality of `problem_type`.
This is broken in three ways:

1. **"other" is unsearchable.** Most queries and most saved strategies fall into
   "other" (no keyword hit). "other" is not a category, it is "none of the above",
   so it provides zero retrieval signal and groups unrelated things together.

2. **The taxonomy is brittle.** Substring keyword matching produces false positives
   ("fix" matched "fixed", so a price answer archived as `code`; "url" matched
   "curl"; "now" matched "snow"). Patching each word is whack-a-mole — the method
   itself is wrong, not the individual words.

3. **The categories do not partition the space.** live_data vs web_retrieval
   overlap (a price lookup *is* a web retrieval); compute vs code overlap; "error"
   is an outcome, not a task type. Exact-match retrieval over overlapping,
   non-exhaustive buckets is both too strict (misses) and too loose (catch-all).

The fix is not to tune the taxonomy — it is to stop categorizing strategies
with a taxonomy at all, and retrieve them the way memory already works: by
embedding similarity against the context that produced them.

---

## Evidence (measured, voyage-4-lite, 1024-dim)

Cosine similarity of a menu-price memory chunk and the strategy text, vs a
different restaurant's price query. Memory inject floor is `0.62`.

| comparison                                              | sim    | vs 0.62     |
|---------------------------------------------------------|--------|-------------|
| pulled-pork chunk  vs  "cheeseburger at Bob's Diner"    | 0.615  | knife-edge  |
| pulled-pork chunk  vs  "pepperoni pizza at Tony's"      | 0.430  | miss        |
| abstracted strategy (directive)  vs burger              | 0.456  | miss        |
| abstracted strategy (question)   vs burger              | 0.515  | miss        |
| abstracted strategy (statement)  vs burger              | 0.456  | miss        |
| vague <<LEARN>> strategy          vs burger             | 0.121  | junk        |
| "capital of France"               vs chunk              | 0.111  | unrelated   |

Two conclusions drive the design:

1. **The source chunk is the best retrieval key for a strategy.** It generalizes
   across restaurant name and item (0.615 / 0.430), and it generalizes *better*
   than the strategy's own abstracted text in any form (0.46–0.52). The embedder
   separates question-form from advice-form, so a query matches another question
   better than it matches a directive. Therefore: retrieve strategies by linking
   them to the chunk that produced them, not by embedding the strategy text.

2. **Same-concept similarity sits at the current floor.** ~0.43–0.62, with the
   memory inject floor at 0.62. Memory is tuned for same-topic (high-sim)
   retrieval; strategies are meant to generalize (same-concept, medium-sim). So
   strategies need a more permissive floor than memory.

---

## Design

### Part 1 — Delete the taxonomy as a retrieval key

- Remove `problem_type = ?` from strategy injection (the `_strategy_block` queries).
- Remove `_classify_problem_type` from the strategy save path and the "error"->
  "other" and "model"->"other" shims.
- `_classify_problem_type` remains ONLY where it is genuinely needed — capability-
  edge tracking (`_record_capability`) — and that is a separate concern, not
  strategy retrieval. TBD whether even that should eventually go.
- No more "other" category. Strategies have no discrete category.

### Part 2 — Link each strategy to its source chunk

- `source_chunk` already exists on the `strategies` table. Make it authoritative:
  every save path MUST populate it with the chunk id of the turn that produced
  the strategy.
- Today `_save_strategy` inserts `source_chunk=""`; the archive-failure path and
  the every-turn lifecycle need to pass the chunk id through. (Currently three of
  five save paths pass a task type, none pass a source chunk.)
- A strategy without a source chunk (e.g. imported) is still storable but is
  inert for linkage retrieval until it is attached.

### Part 3 — Retrieve by chunk similarity, with two floors

On injection, strategies are pulled by the similarity of their source chunk:

- **Memory floor (0.62, unchanged):** a chunk whose similarity clears 0.62 injects
  as memory context AND its linked strategies inject.
- **Strategy floor (~0.55, new, lower):** a chunk below 0.62 but at/above the
  strategy floor does NOT inject as memory, but its linked strategies DO inject.

Net effect: a lesson learned from "pulled pork price" fires on "cheeseburger at
Bob's Diner" (0.615) even though the chunk itself sits under the memory floor.
The strategy tier has a broader net than the memory tier.

Implementation requires `route_query` to also return per-chunk scores (it already
computes them; expose them), then two lookups:
`SELECT ... FROM strategies WHERE source_chunk IN (<chunk ids >= memory floor>)`
and the same for `<chunk ids in [strategy floor, memory floor)>`.

### Part 4 — What survives the taxonomy

The only discrete fields that remain on a strategy:

- `outcome` (SUCCESS / FAILURE) — the "do this vs do NOT do this" axis; drives the
  two injection headers. Not a category, a valence flag.
- `grade` (A/B/C/D/F) — ranking within the injected set (A before B).
- `source_chunk` — the retrieval linkage.
- `strategy_text` — display content only (NOT the retrieval key).

Injection format is unchanged: two headers, each emitted only when its group is
non-empty ("STRATEGIES THAT WORKED" / "STRATEGIES THAT FAILED — do NOT do this").

---

## Save-time rule — the failure ladder, the strategy trigger, "bad tool call"

This is the save half of the strategy system, parallel to the retrieval half
above. Retrieval decides WHEN a strategy surfaces; this section decides WHEN a
strategy is CREATED. The strategy text stays model-extracted (the rich lesson,
not a boilerplate "use tool B"); only the TRIGGER is deterministic.

### Part 1 — Define "bad tool call" (the floor everything stands on)

One notion: a tool call did not advance the task toward a correct answer. It has
an objective core and a subjective edge:

  HARD failure (deterministic) — empty result, or a failure marker: "no results",
    "blocked", "captcha", "cloudflare", "access denied", "forbidden",
    "rate limit", "timed out", "connection refused", "command not found",
    "no such file", "permission denied", 403/404/429/502/503, ... (this is
    `_classify_tool_outcome` / `_FAILURE_MARKERS`).

  SOFT failure (semantic) — returned content, but wrong / irrelevant / stale /
    misleading. Detected by the model's own `[TOOL:FAILURE: reason]` tag, which
    the system prompt already requires after every tool call.

  bad tool call  =  HARD failure  OR  SOFT failure.

Known gap: a soft failure the model forgets to tag is invisible — the length
heuristic (>= 100 chars -> SUCCESS) will call a 150-char wrong page a success.
The fix is not a bigger keyword list; it is to treat the model's tag as
authoritative and "no tag + deterministic success" as success. Honest tagging
closes the definition; no keyword list will.

### Part 2 — The failure ladder (one signal, escalating responses)

Consecutive bad tool calls within one turn form a streak. The streak drives
escalating responses that MUST be coordinated (Part 4):

  streak 1      -> nothing (a flaky request is not a lesson)
  streak 2      -> recovery window: soft nudge ("try a different tool"), but the
                   model keeps going and is allowed to recover
  streak >= 3   -> OVERCOME: STOP and build a tool (declare_edge is NOT a valid
                   response). The build path terminates at the tool-chain limit;
                   failing to build there is what surfaces the edge to the user.
  >= 6 rounds   -> hard stop (loop bound, no final answer)

2 / 3 / 6 are starting points, to be validated against the real streak
distribution (see "Threshold sweep").

Principle: an edge is not a response trigger — it is a trigger to build a tool.
The model cannot "declare" an edge; it can only act on it (build or reuse a
tool), and the edge is surfaced by the build path terminating at its budget.

### Part 3 — Strategy triggers (what gets saved)

Two save rules for regular conversation, replacing the five current triggers
(`_strategy_lifecycle`, archive-failure, tool-trail, novel-procedure; <<LEARN>>
unchanged):

  SAVE a SUCCESS strategy when, within one turn:
    1. a streak of >= 2 consecutive bad tool calls, AND
    2. the turn then succeeded via a DIFFERENT tool / URL / query than the
       failures (approach change — "retried the same thing and it worked" is not
       a lesson), AND
    3. the turn graded A/B (the recovery produced a correct, honest answer).
    Text: model-extracted ("what method worked vs what didn't, and why").
    Stored: outcome=SUCCESS, linked to source_chunk.

  SAVE a DON'T-DO strategy when:
    1. the turn graded D/F, AND
    2. it was not an infra failure (a timeout / grind has no introspectable
       lesson).
    Text: model-extracted ("the one rule that would have prevented this").
    Stored: outcome=FAILURE, linked to source_chunk.

Extraction mechanics: ONE model call per save, fed the turn's tool trail + final
answer, prompted to extract the lesson. No "was this novel?" gate — the trigger
above already qualifies the turn. This replaces both the 3-call grade-A sequence
and the single D/F call in `_strategy_lifecycle`.

### Part 4 — Coordination constraint (the point)

The strategy-save streak threshold and the build-a-tool threshold key off the
same signal and must satisfy:

    strategy-save threshold  <  build-a-tool (overcome) threshold

Otherwise overcome interrupts the model before it can try tool B and recover, and
the "N failures -> natural success" strategy pattern never occurs. With 2 (save)
< 3 (overcome), the model has a one-step recovery window: fail twice, get nudged
to switch tools, and if it recovers we save the strategy; fail a third time and we
escalate to build-a-tool. The current `STUCK_CONSECUTIVE_FAILURES = 2` is too low
precisely because it removes that window.

---

## Tradeoff

Linkage ties strategy portability to the DB: a strategy travels with its source
chunk, so it cannot be exported as a standalone "playbook" without also carrying
the chunk (and its embedding). This is acceptable — strategies already live in the
same `mneme.db` + FAISS index as chunks, so the unit of portability is already
"the whole DB." The standalone strategy export/import in `strategy-roadmap.md` §9
is downgraded to a non-goal unless a concrete need appears.

---

## To be added (separate decisions, same doc)

- **Strategy quality** — the junk filter (`_is_junk_directive`) is the real gate.
  Linkage retrieval (and model-extracted text) will faithfully inject a bad
  strategy once saved, so save-time rejection matters more, not less.
- **Provenance grading** — "Au" is a weight-stored fact, not a `[guess]`. Distinguish
  "known from weights" from "fabricated" (the current source-tagging has one axis
  where it needs two).
- **Grinding** — menu-price queries loop ~4 min, ~200s of which is two ~100s
  OpenRouter read timeouts. Separate from strategy retrieval.
- **Threshold sweep** — confirm (a) the strategy floor (~0.55) against same-concept
  vs unrelated queries, and (b) the failure-ladder numbers (2 / 3 / 6) against the
  real consecutive-failure distribution, before freezing them.

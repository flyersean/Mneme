# Mneme Build Plan — strategy + context rework

Derived from `docs/strategy-retrieval-spec.md`. This is the EXECUTION order, not the
rationale order — see the spec for the "why". Each phase is independently testable
and committable; work proceeds lowest-risk → highest-risk, respecting dependencies.

## Implementation status

- [x] Phase 0 — baseline (54 tests green)
- [x] Phase 1 — prefix-cache stability (`f9ebdb5`)
- [x] Phase 2 — save-time rule (`ae99e5f`)
- [x] Phase 3 — retrieval / source-chunk linkage (`02f82c2`)
- [ ] Phase 4 — validation sweep (needs a live deploy + real query battery; not code)

Deferred from the strict two-rule spec (documented in code):
- The "different approach" condition on the SUCCESS trigger is approximated by
  ">= 2 consecutive failures then success" — precise tool/url/query comparison
  needs tool names in the combined trail (not yet carried).
- `source_chunk` is populated by the archive path and `_save_strategy`'s new
  param; the recovery/DON'T-DO/novel saves still write `source_chunk=""` because
  the turn's chunk is archived asynchronously after the save is enqueued (a
  turn->chunk tracker is the follow-up).
- `_strategy_floor_chunks` embeds the query a second time (route_query also
  embeds) — a single-embed refactor is a cheap follow-up.

Order rationale:
  Phase 1 (prefix-cache) is self-contained and touches no semantics — safe first,
  and it establishes the "stable prefix" invariant later phases must respect.
  Phase 2 (save-time rule) rewrites the save paths AND makes `source_chunk`
  authoritative on write — a prerequisite for Phase 3.
  Phase 3 (retrieval) reads `source_chunk` and deletes the taxonomy — depends on
  Phase 2 having populated it, and is the highest-risk change.
  Phase 4 is measurement, not more code.

Safety net: `python tests/test_tool_loop.py` must stay green (54 tests) after every
task. Commit after each phase.

---

## Phase 0 — Baseline

Verify clean state before touching anything.

  cd ~/mneme/repo
  git status                       # expect: only docs/build-plan.md new
  python tests/test_tool_loop.py   # expect: 54 passed, 0 failed

---

## Phase 1 — Prefix-cache stability (context assembly)

Files: `proxy/mneme_proxy.py` only. No schema, no behavior change — only WHERE
variable content is placed and idempotency.

### Task 1a — Move advisory directives off the system message to the tail

Current (`process_chat`, ~L3812–L3854):
  mneme_system = _system_prompt_block() + _tool_directive(db, cur_ptype) + _explore_directive(full_user_msg)
  ... mneme_system += "\n" + _tool_injection ...
  ... mneme_system += "\n\n" + <overcome/build/reuse/nudge> ...
  messages.insert(insert_at, {"role": "system", "content": mneme_system})
  ... then memory context prepended to last user message ...

Change:
  - The system message keeps ONLY `_system_prompt_block()` (stable).
  - `_tool_directive(...)`, `_explore_directive(...)`, `_tool_injection` move to the
    TAIL, prepended to the last user message alongside the memory context.
  - `overcome`/`build`/`reuse`/`nudge` directives STAY in the system message
    (rare, control-flow, need authority — per spec "What NOT to do").

Mechanics: build a `dynamic_tail` string from the three advisory pieces; if
non-empty, fold it into the existing tail-injection block (context + dynamic_tail
prepended to the last user message). Keep the overcome/build/reuse/nudge block as
`mneme_system += ...`.

Test: run suite (green). Optionally add a test asserting `_system_prompt_block()`
is the only fixed system content and `_tool_directive` output never appears in the
system message when a saved tool exists.

### Task 1b — Make tool-result truncation idempotent

Current (`compress_large_tool_results`, ~L2825–L2832): truncates >MAX_TOOL_FORWARD
to head(3/4) + ~170-char note + tail(1/4) ≈ MAX_TOOL_FORWARD + note_len, which is
STILL over the threshold, so it re-truncates next turn (note's length number
changes → bytes shift).

Change: compute head_len = (MAX_TOOL_FORWARD - NOTE_LEN) * 3 // 4 so the truncated
result is ≤ MAX_TOOL_FORWARD, OR skip messages already containing the truncation
marker (`[... content truncated:`). Prefer the marker-skip (simplest, no re-truncation).
Marker constant: `_TRUNC_MARKER = "[... content truncated:"`.

Test: suite green; add a test that a pre-truncated message is returned unchanged.

### Task 1c — Memory-context re-prepend guard

Current (~L3859–L3864): `content = context + "\n\n---\n" + content` with no guard.

Change: skip the prepend if the message already contains the injection separator
(or a dedicated marker). Track the injected turn so the SAME turn never double-injects.

Test: suite green; add a test that a message already carrying the marker is not
prepended again.

Commit: `refactor: prefix-cache-stable context assembly (phase 1)`

---

## Phase 2 — Save-time rule (strategy triggers)

Files: `proxy/mneme_proxy.py`, `proxy/mneme/overcome.py`. Replaces five save paths
with two deterministic rules + one extraction call.

Current save paths (all hit `_save_strategy`):
  (A) `_strategy_lifecycle` (every turn; 3-call grade-A + D/F single-call)
  (B) `_archive_single_chunk` (D/F failure, hardcoded grade B)
  (C) `_run_learning_mode` (<<LEARN>>) — unchanged
  (D) `_learn_from_tool_trail` (any FAILURE + any SUCCESS)
  (E) `_save_novel_strategy` (novel tool procedure)

### Task 2a — Collapse to two rules

  SAVE SUCCESS: streak >= 2 consecutive bad tool calls (from combined trail),
    then success via a DIFFERENT tool/url/query, and turn graded A/B.
  SAVE DON'T-DO: turn graded D/F and not infra-failure.

Remove paths (A) every-turn and (D) over-eager tool-trail; keep (B)/(E) but route
them through the same two-rule gate; keep (C) <<LEARN>> unchanged.

### Task 2b — One extraction call (no novelty gate)

Replace the 3-call grade-A sequence (novelty q1 + id q2 + describe q3) with ONE
call: feed the turn's tool trail + final answer, prompt "what method worked vs
what didn't, and why" (SUCCESS) / "the one rule that would have prevented this"
(DON'T-DO).

### Task 2c — Coordination: STUCK_CONSECUTIVE_FAILURES 2 → 3

`proxy/mneme/overcome.py` `STUCK_CONSECUTIVE_FAILURES` default "2" → "3" (recovery
window: model can try tool B at streak 2 and save a strategy; escalate at 3).
Update the docstring + the instruction meta description that cite "2 failures".

Test: update `test_tool_loop.py` save-trigger tests to assert the two-rule gate;
suite green.

Commit: `feat: deterministic two-rule strategy save trigger (phase 2)`

---

## Phase 3 — Retrieval (source-chunk linkage)

Files: `proxy/mneme_proxy.py`, `proxy/mneme/capability.py`. Highest risk.

### Task 3a — `source_chunk` authoritative on write

Every `_save_strategy` path must populate `source_chunk` with the current turn's
chunk id (needs the archive step to expose "chunks created this turn"). A strategy
with no chunk stays storable but inert for linkage.

### Task 3b — Delete taxonomy from injection

Remove `AND problem_type = ?` from `_strategy_block`'s two SQL paths; strategies
inject by `source_chunk` membership (per-chunk similarity), not category.
`_classify_problem_type` stays ONLY for capability-edge tracking.

### Task 3c — Two-floor retrieval

`route_query` must return per-chunk scores (it already computes them — expose
them). Then two lookups:
  chunks >= memory floor (0.62)        -> inject memory + linked strategies
  chunks in [strategy floor 0.55, 0.62) -> inject linked strategies only

Keep `keyword_fallback: 0` = no keyword searching.

Test: update injection tests to the linkage model; suite green.

Commit: `feat: source-chunk linkage retrieval (phase 3)`

---

## Phase 4 — Validation sweep (measure, don't assert)

Not more code. Confirm the starting values against real data before freezing:

  - strategy floor 0.55 vs memory floor 0.62 (same-concept vs unrelated battery)
  - failure-ladder 2 / 3 / 6 (real consecutive-failure streak distribution)
  - prefix-cache: confirm the system message is now byte-stable across turns
    (diff two consecutive requests' leading system content)

Record results in the spec's "Threshold sweep" section. No code unless a number
demonstrably needs to move.

---

## Open items NOT in this build (still "to be added" in the spec)

  - Strategy quality (junk filter as the save-time gate)
  - Provenance grading ("known from weights" vs "fabricated")
  - Grinding / OpenRouter 100s read-timeout
  - `[guess]` labeling conflation

# Capability-Edge Tracking

Turns grades into a map of where the model's competence ends, so that a task the
model can't do stops being "grind/fabricate" and becomes "build a tool."

## The insight (why this is the right shape)

A model cannot know its own limits *before* it tries — by default it assumes it
can do anything (it will happily fabricate a fact, or grind forever on a
computation). But it *can* grade a result *after* producing it. The provenance
grader (Phase 1/1b/2) gives an honest grade of the epistemic process.

So the loop is:

```
try → grade honestly → poor grade records a capability edge
    → next encounter with that type → route to tool-building
```

The grades are the mechanism that maps the model's competence boundary. The
anti-grind guardrail (CHAT_TIMEOUT) is a *safety net* for the case where the
model doesn't recognize the limit and grinds forever — it is not the trigger.

## Mechanism

### 1. Classify the task (`_classify_problem_type`)

Deterministic keyword heuristic, capability-oriented:

| type | keywords | failure mode |
|------|----------|--------------|
| `compute` | hash, sha, compute, calculate, prime, fibonacci, checksum, encrypt, sum of | grinds (manual computation) |
| `live_data` | price, weather, stock, exchange rate, current, today, latest, temperature | fabricates (can't know live state) |
| `code` | code, function, def, patch, fix, debug, script, python, write a | (model is competent here) |
| `web_retrieval` | search, fetch, browser, http, page, url | (has tools here) |
| `memory_operation` | save, archive, memory, store, remember | (has tools here) |
| `error` | error, failed, crash, 500, exception | (handled separately) |

`code` is checked before `compute` so "write a function to compute X" is a code
task, not a compute task.

### 2. Record the grade (`_record_capability`)

After every graded chat turn, the grade is recorded against the task's type in
the `capability_edges` table (`attempts`, `failures`, `last_grade`, `flagged`).

### 3. Flag the edge

A type is flagged when it accumulates enough poor grades:
`failures >= MNEME_EDGE_FAILURES (2)` AND `failures/attempts >= MNEME_EDGE_RATIO (0.5)`.

### 4. Next-encounter directive (`_capability_directive`)

When a *new* task classifies as a flagged type, a directive is injected into the
system prompt: "you have previously failed on this type; do not attempt from
memory — propose the exact tool/command/script, or state clearly you can't
answer." This routes the model away from grind/fabricate toward tool-building.

## Storage

`capability_edges` table: `problem_type` (PK), `attempts`, `failures`,
`last_grade`, `flagged`, `updated_at`.

Inspection/override endpoint: `GET /capabilities` (list), `POST /capabilities`
with `{"flag": "<type>"}` or `{"clear": "<type>"}`.

## Env knobs

- `MNEME_EDGE_FAILURES` (default 2) — min D/F to flag.
- `MNEME_EDGE_RATIO` (default 0.5) — D/F ratio to flag.
- `MNEME_CHAT_TIMEOUT` (default 240) — anti-grind budget per generation.

## What this is NOT (yet)

- It does not *execute* the tool — it only makes the model *propose* one. Actual
  execution belongs to a coding agent with a shell (Pi), or the throwaway
  sandbox pod (Phase 5).
- It keys on a coarse keyword classifier, not embeddings. Fine for v1; an
  embedding-similarity key is a possible refinement.
- Grades still need calibration (a correct-but-unflagged specific sub-claim
  currently grades D). Fix calibration before trusting edges fully.

## Status

Built and deployed (commit `b441b66` + `1f9ef3c`). Smoke-tested; not yet
stress-tested.

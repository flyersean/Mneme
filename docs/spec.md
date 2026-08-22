# Mneme — Overcome Mode + Externalized Instructions + Modularization

Branch: `modularize-overcome` (cut from `unified_mneme`)
Status: SPEC (pre-implementation) — do not merge until Parts 1–3 land and tests pass.

---

## Why this exists

Mneme today **detects** failure but never **acts** on it:

- Tool failures → a `[TOOL-TRAIL]` plus a soft nudge ("diagnose before retrying"). The model keeps grinding (the Glassdoor run: 13+ tool calls, no convergence).
- Bad grades (D/F) → `_record_capability` flags the problem type, and `_capability_directive` injects "propose a tool OR admit you can't" on the *next* encounter. But that is a one-shot text prompt — nothing forces a stop, nothing drives build→test→iterate, and nothing persists whatever the model proposes.

And every injected prompt is a hardcoded string inside a 4,919-line / 226 KB monolith, so tuning wording for a quirky model or an edge case requires a code edit.

This spec covers three changes that ship together because they build on each other:

1. **Overcome mode** — turn a noted edge into an acted-on edge.
2. **Externalized instructions** — every injected prompt becomes an editable file.
3. **Modularization** — break the monolith so (1) and (2) — and every future feature — have a clean home and are testable in isolation.

---

## Part 1 — Overcome mode

### The loop

`DETECT → STOP → DELIBERATE → BUILD → OUTCOME`

A bounded, self-terminating escalation that runs whenever the model is "stuck."

#### 1. DETECT — `_detect_stuck(messages) -> (bool, reason)`

One helper merging two signals that today are handled separately (or not at all):

- **(a) Consecutive failures** — N tool `FAILURE`s in the combined trail with no `SUCCESS` between them. Default **N = 2**. Catches "same blocked scrape, retried, blocked again."
- **(b) Loop bound** — M total tool-call rounds since the last user turn without a final answer. Default **M = 6**. Catches the Glassdoor case where failures were interleaved with successes but the model still never converged. (This also closes a pre-existing gap: `search_memory` has a 4-round bound, but the *general* passthrough tool loop is unbounded.)

**Explicit non-trigger:** a single failure followed by a success ("fail → recover") must NOT trip it. We never interrupt a healthy recovery.

#### 2. STOP — `_overcome_directive(problem_type, failures)`

Replaces the soft-nudge branch with a hard system-message directive:

> === OVERCOME MODE ===
> You have failed N times / exceeded M tool rounds on this task. STOP. Do not retry the same approach.
> The fixed harness tools are: read, bash, edit, write, search_memory, web_search, web_scrape. You cannot add or modify harness tools; "build a tool" means write a script or command you can run via bash, save it, and invoke it. Do not attempt to extend the harness itself.
> Think, then decide:
>   - If you can build a tool that would solve this, output `DECISION: build_tool` and a `PLAN:` (what it does, how it's built, how it's tested).
>   - Otherwise output `DECISION: declare_edge` and a `MISSING:` note naming the capability that is absent.
> Prefer an existing harness tool first; build only when none of them can do it.

#### 3. DELIBERATE — `_parse_deliberation(reply) -> dict`

Parses the model's next reply for structured markers (more robust than raw JSON for the current backends):

- `DECISION: build_tool | declare_edge`
- `PLAN: <…>`
- `MISSING: <…>`

Natural-language-with-markers, not forced JSON, so a slightly-off model still yields a parseable decision.

#### 4. BUILD — `_tool_build_loop(problem_type, task, plan)`

If `build_tool`, run a bounded loop (default **3 iterations**) letting the model:

1. `write` the tool (a script under `~/mneme/chunks/tools/`),
2. `bash` it against the failing task,
3. observe the result (the deterministic classifier already labels each run result),
4. iterate.

Each iteration is a normal `process_chat` turn carrying a `=== BUILD iteration K/3 ===` marker. Iteration state is derived from markers in the message history (not a new session store) — consistent with how the proxy already derives nudge/edge/trail from messages, and keeps `process_chat` stateless.

Grading each iteration: a cheap yes/no judge ("did this tool produce a correct result for the task?") or reuse the existing provenance grade on a final answer produced with the tool.

#### 5. OUTCOME

- **SUCCESS** → `_save_tool(problem_type, name, description, script_path)` into a new `tools` table, and `_record_overcome(problem_type, "overcame")`. Every future task of that type injects a *positive* directive: "you have a saved tool for this — use it."
- **FAILURE** → `_record_overcome(problem_type, "confirmed")` and the model answers honestly ("I can't do this; here's the missing capability"). The edge stays flagged, now as *attempted and unovercome*, not just *noticed*.

### Triggers — two paths

- **Mid-loop tool failures** → immediate (same session): the stop/deliberate/build runs inside the current task.
- **Bad grades (D/F)** → next-encounter: a D/F turn marks the type; on its next occurrence the overcome path fires (matches the existing edge-directive rhythm; cheaper than an immediate pivot).

### Functions (exact)

| name | responsibility |
|---|---|
| `_detect_stuck(messages)` | (bool, reason) from signals (a)+(b) |
| `_overcome_directive(ptype, failures)` | the STOP directive |
| `_parse_deliberation(reply)` | DECISION/PLAN/MISSING |
| `_tool_build_loop(ptype, task, plan)` | bounded build/test/iterate |
| `_save_tool(...)` | persist a working tool |
| `_record_overcome(ptype, outcome)` | update edge as overcame/confirmed |
| `_tool_directive(ptype)` | positive "use your saved tool" injection |

### Data model (additive, migration-guarded)

- New `tools` table: `tool_id, problem_type, name, description, script_path, tested_at, success_count, retired`.
- `capability_edges` gains `overcome_attempts`, `overcome_success`, `tool_id`. `ALTER TABLE … ADD COLUMN` guarded by `IF NOT EXISTS`, preserving all existing rows.

### Config (all tunable)

```
stuck_consecutive_failures   (default 2)
stuck_max_tool_rounds        (default 6)
build_max_iterations         (default 3)
build_timeout                (reuse CHAT_TIMEOUT)
```

### Bounding & cost

Worst case per stuck task: 1 deliberation + 3 build iterations ≈ 4–8 model calls, then it terminates (success or declared edge). This replaces the current *unbounded* grind — a cost **reduction** in the failure case.

---

## Part 2 — Externalized instructions

Every injected prompt becomes an editable file. The safety of this rests on one rule, already used by `system_prompt.md`:

> **code-default + disk-override + graceful fallback.**

Every instruction ships with a hardcoded default (a fresh clone always works). At startup the proxy looks for overrides under `instructions/`. A missing/malformed file falls back to the default **and logs it loudly**. A bad instruction file degrades to the shipped default, never to a broken injection.

### Directory layout

```
~/mneme/chunks/instructions/
  README.md                     # the map: what each file does + when it's injected
  default/
    capability_edge.txt
    explore.txt
    user_preferences.txt
    meta_principles.txt
    system_directives.txt
    tool_failure_nudge.txt
    overcome.txt                # includes the anti-harness boundary
  <model-name>/                 # per-model overrides (wins over default/)
```

### Templating

Use a reserved `{{var}}` placeholder syntax (prompts contain literal braces — JSON examples, code). Substitute only declared vars; **fail loud on an unknown var**.

### Frontmatter (self-documenting per file)

```
# when: injected when a task's problem type is a flagged capability edge
# vars: {problem_type}
# used_by: _capability_directive
<body with {{problem_type}} placeholder>
```

### README index + sync test

`README.md` is the human map. One test keeps it honest: every instruction file has a `used_by` that maps to a real code site, and every injection site references a file — no orphan files, no undocumented injections.

### Phasing

- **Tier 1 (now):** pure static text — capability edge, explore, user-preferences header, meta-principles header, system-directives header, tool-failure nudge, overcome.
- **Tier 2 (follow-up):** text mixed with logic — strategy-lifecycle, learn-from-tool-trail, provenance judge, abstract-strategy. Extract the text; keep the assembly in code.

---

## Part 3 — Modularization

Strangler-fig, NOT big-bang. New features land in new modules; existing code is extracted only when it is already being touched for a feature.

### Target layout

```
mneme/
  instructions.py   # NEW — instructions loader + {{var}} templating
  overcome.py       # NEW — detect/stop/deliberate/build/outcome
  capability.py     # extract — edges, problem-type classify, _capability_directive
  tool_trail.py     # extract — classifier, combined trail, nudge
  grading.py        # extract — provenance/inline grading, judge
  # later extractions (have a home, not yet moved):
  # retrieval.py, memory.py, strategies.py, config.py, api.py
mneme_proxy.py      # orchestrator (process_chat) + facade re-exports
```

### Facade pattern

`mneme_proxy.py` re-exports moved symbols, so `import mneme_proxy as mp` (what `test_tool_loop.py` does) and the Flask entry point keep working unchanged while modules absorb the code. `_real_*` test hooks re-export through the facade.

### Shared state

Pragmatic module-level globals, initialized by the orchestrator at startup (matches current style). A `mneme/state.py` context object is deferred until the wiring genuinely needs it — do not over-design up front.

### Extraction order

1. `instructions.py` (new)
2. `overcome.py` (new)
3. `capability.py` (extract — touched by overcome)
4. `tool_trail.py` (extract — already self-contained)
5. `grading.py` (extract)

---

## Test plan (deterministic, red → green)

- `_detect_stuck` true on 2 consecutive failures; false on fail→success.
- `_detect_stuck` true on >M tool rounds without answer (scripted history).
- `_parse_deliberation` extracts DECISION/PLAN/MISSING from well-formed and slightly-malformed replies.
- Build loop terminates at max iterations; records `confirmed` on failure.
- `_save_tool` + positive-tool injection round-trips.
- Capability-edges migration preserves existing rows.
- Instructions loader: override wins; missing/malformed falls back + logs; unknown `{{var}}` fails loud; sync test passes.

## Rollout

1. Work on `modularize-overcome`; do NOT touch `unified_mneme` until merged.
2. Preserve the live DB (`~/mneme/chunks/mneme.db`) — all migrations additive.
3. After each module lands: run the offline suite, then a live smoke test through Pi.
4. Merge to `unified_mneme` only when all tests pass and a live session still works.

## Execution & delegation plan

- **Scaffolding — not delegated.** The orchestrator creates the `mneme/` package, the facade re-export, and the module-state init. This is the foundation and the highest-risk wiring; it needs full conversation context.
- **Delegation — sequential, not parallel.** Subagents run as `moonshotai/kimi-k3` via OpenRouter (already pinned in `delegation.model/provider`). Each chunk is a tight brief: point at `docs/spec.md` + the symbols to move + "preserve the `_real_*` test hooks, re-export everything through the facade."
- **Order:** scaffolding (facade + `util.py`) → `tool_trail.py` (first extraction, establishes the pattern) → `instructions.py` (new) → `overcome.py` (new) → `capability.py` → `grading.py`.
- **Verification — mine, not the subagent's.** After each chunk: `py_compile`, then the offline suite (must still `import mneme_proxy as mp` unchanged), then a live Pi smoke test. Never trust a subagent self-report.
- **Merge** to `unified_mneme` only after every chunk is verified.


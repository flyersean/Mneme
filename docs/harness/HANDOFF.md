# Harness work — handoff / resume point

Branch: `agent-harness`. Last updated 2026-09-28.

## Status

| Phase | Status |
|---|---|
| 0 — Architecture audit | **Done**: `00-architecture-audit.md` |
| 1 — Persistent run engine | **Done**: ADR `adr/0001-run-ledger.md` |
| 2 — Planning / execution loop | **Done**: ADR `adr/0002-planning-verification.md` |
| Swarm-creation skill | **Done**: `skills/swarm-creation/SKILL.md` |
| 3 — Strategy → Skill → Tool architecture | **Next** |
| 4–10 | Not started |

## Phase 2 summary

- `harness/planning.py`: parses `PLAN:` and `VERIFY:` lines, and detects `REPLAN:`.
- `harness/verify.py`: deterministic checks (`command`, `file_exists`,
  `file_contains`, `output_contains`, `output_matches`).
- `engine.py`:
  - a pending-plan phase (the `planning` state, a `plan` step kind, fallback to the goal);
  - `_verify` after a step claims it is done;
  - `_plan(mode="replan")` on dead ends or a `REPLAN:` request, bounded by
    `max_replans` (default 2);
  - interrupted steps, which consume no failure budget.
- `chat_executor.py`: `make_chat_planner` and the `harness_plan` instruction. Task
  prompts now include the workspace path and the verify note.
- Proxy:
  - `_turn_cancel_event()` / `_scoped_process_chat` give each harness step its own
    cancel event;
  - the engine is built with both a planner and an executor, sharing one lock;
  - `POST /runs` accepts `plan`.
- Tests: `test_harness_planning` (21) and `test_process_chat_characterization` (5,
  covering the injection split, the cancel flag, and cancel scopes). The proxy suite
  gained a planned run through HTTP, with the plan's `VERIFY:` check enforced.

## Tests

`cd tests; for f in test_*.py; do timeout 300 python3 $f >/dev/null 2>&1 || echo "FAIL $f"; done`

Everything passes except `test_mcp_client` and `test_mcp_endpoints`. Those fail
because the installed `mcp` package has no `mcp.server.mcpserver`, which predates
this work.

`test_harness_proxy` failed once in a single run early on (the plan fell back and
the real model was reached) and could not be reproduced in 10 later runs. If it
reappears, look for a race between setting `mp.HARNESS.planner` in `setUpClass`
and a background run.

## Phase 3 plan (next)

1. **Strategy versioning.** Add a `strategy_versions` history table in `mneme.db`
   (additive migration, following `curation.ensure_schema`). Make `_save_strategy`
   append a version row instead of `INSERT OR REPLACE` overwriting. Add provenance
   columns: `created_by` (user / model / `run:<id>`), `derived_from`, `validated_by`.
2. **Run → strategy feedback.** When a run completes or fails, attach the strategy
   ids that were injected during its steps. Then add per-run success/failure
   counts to strategies. Today `_consume_injected_strategies` counts per turn.
3. **Skills.** Add a `skills` registry (a DB table plus `skills/<name>/SKILL.md`
   files on disk, which the swarm skill already follows). Record description,
   tools, strategies, verification, version and stats. Retrieve skills by embedding
   similarity to the goal or task, and inject only the relevant ones into the
   harness task context.
4. **Tool metadata.** Add risk, permission level and verify hints for the built-in
   tools (`mntools` definitions) and the `tools` table. This is the groundwork for
   capability selection (Phase 4) and permissions (Phase 5/7).
5. Before touching `_save_strategy` and `_consume_injected_strategies`, add
   characterization tests for their current behaviour. `test_tool_loop` covers
   parts of it.

## Known limitations

- `_last_injected_ids` and `_INJECTED_STRATEGY_IDS` are still globals shared with
  chat turns (ADR 0002).
- A crash during a replan loses that replan.
- Steps run at least once, and auto-resume is off by default.
- `model_calls` counts harness turns, not the model re-queries inside a turn.
- `/runs` has no auth.

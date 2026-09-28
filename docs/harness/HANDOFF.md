# Harness work — handoff / resume point

Branch: `agent-harness`. Last updated 2026-09-27.

## Status

| Phase | Status |
|---|---|
| 0 — Architecture audit | **Done**: `00-architecture-audit.md` |
| 1 — Persistent run engine | **Done**: `proxy/mneme/harness/`, wired into the proxy, `/runs` API, ADR `adr/0001-run-ledger.md` |
| Swarm-creation skill (user request) | **Done**: `skills/swarm-creation/SKILL.md` |
| 2 — Planning / execution loop | **Next** |
| 3–10 | Not started |

Also fixed during Phase 1: `/api/chat/completions` (non-stream) never returned its
response. There is a regression test in `tests/test_harness_proxy.py`.

## Tests

Run all suites:
`cd tests; for f in test_*.py; do timeout 300 python3 $f >/dev/null 2>&1 || echo "FAIL $f"; done`

- All suites pass except `test_mcp_client` and `test_mcp_endpoints`. Those two fail
  because the installed `mcp` package has no `mcp.server.mcpserver`. That is an
  environment problem that predates this work.
- New suites: `test_harness_ledger` (20), `test_harness_engine` (22, including a
  real crash-and-resume subprocess test), `test_harness_proxy` (7).
- `test_tool_loop`'s instruction-sync check now also scans `mneme/**` subpackages.

## Where things are

- `proxy/mneme/harness/ledger.py`: the store, the state machine, append-only
  events, and the cross-process lease.
- `proxy/mneme/harness/engine.py`: `RunEngine` (execute, budgets, checkpoints,
  pause/resume/cancel/retry, `restore_checkpoint`, recover).
- `proxy/mneme/harness/chat_executor.py`: one step = one `process_chat` turn.
  Success is judged by the harness, not the model. It uses the
  `harness_task_context` instruction.
- `proxy/mneme/harness/http.py`: the `/runs` routes.
- Proxy wiring in `proxy/mneme_proxy.py`:
  - `_init_harness()`, defined just above `if FLASK_OK:` and called after `_dump_config()`;
  - route registration next to `/admin/reload`;
  - the `harness.*` keys in `_CONFIG_ENV_MAP`.

## Phase 2 plan (next session)

1. Add characterization tests first (audit §8): the static/dynamic injection split
   in `process_chat` (exactly one fixed Mneme system block, one tail block, no
   double injection), and the cancel-flag semantics.
2. Add a planning step. When a run is created with no tasks, the first step is a
   *planner* turn. The model writes in natural language plus `PLAN:` lines (the tag
   style already parsed in `mneme/overcome.py`, `_PLAN_RE`). The harness extracts
   the lines into tasks with `ledger.add_task` and emits `plan_created` (plan
   `version` goes up each time).
   - Keep the parser tolerant.
   - If no `PLAN:` lines are found, fall back to one task equal to the goal.
3. Add replanning. When a task exhausts its attempts, or a step reports new
   information, go back into the planner instead of failing. Increment
   `usage.replans` and enforce `max_replans`, which `engine._budget_exceeded`
   currently skips via `_BUDGET_TO_USAGE`. Record the `replan_requested` and
   `plan_created` events.
4. Add a verify hook: an optional per-task `verify` spec (command exit code, file
   exists), run by the harness after a step reports done, with
   `verification_started` / `verification_passed` / `verification_failed` events.
   This is also the base for Phase 5.
5. Move `process_chat`'s turn globals into a per-call context, so harness steps and
   chat requests stop sharing `_cancel_event`, `_last_injected_ids` and
   `_INJECTED_STRATEGY_IDS`.

## Known limitations (see the ADR)

- Steps run at least once, and auto-resume is off by default.
- `model_calls` counts harness turns.
- The chat UI's Stop button also cancels an in-flight harness step.
- `/runs` has no auth. It inherits the 127.0.0.1 bind.

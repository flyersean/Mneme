# ADR 0002 — Planning, replanning, verification, cancel scopes (Phase 2)

Status: accepted (2026-09-28). Builds on ADR 0001.

## Decisions

1. **Planning uses natural language plus line tags, not JSON.** The planner turn is
   an ordinary `process_chat` call, so it gets memory, tools and grading. It is
   prompted by the editable `harness_plan` instruction. The harness extracts only:
   - `PLAN: <task>` lines, which become tasks;
   - `VERIFY: <command>` lines, which attach a command check to the task above.

   The parser (`harness/planning.py`) tolerates list markers, numbering and bold,
   and caps a plan at 8 tasks. This is the same tag style as `PLAN:` in overcome
   mode and `[TOOL:…]`.

2. **A run plans only when it has no tasks.** `create(plan=None)` plans when no
   tasks are given and a planner is configured. `plan=false` keeps the Phase 1
   behaviour (one task = the goal), and supplying tasks skips the planner. If the
   *initial* plan fails (no `PLAN:` lines, invalid spec, planner error), the run
   falls back to one task equal to the goal (`plan_fallback`). Small models must not
   be able to dead-end a run at step zero.

3. **Replanning replaces failing.** It triggers in two cases:
   - a task exhausts its attempts;
   - a task step reports `REPLAN: <why>`.

   In either case the engine re-enters planning. The replan prompt shows completed
   (do not repeat), failed (with the error) and remaining tasks. Old pending tasks
   are marked `skipped` (`task_superseded`), not deleted. `plan.version` increments.
   Replanning is bounded by `max_replans` (default 2); a replan that yields no tasks
   fails the run. Each replan resets `usage.failures`, so the new plan gets a fresh
   failure budget, while `max_steps` still bounds the whole run.

4. **Verification is deterministic and harness-run.** A task's `verify` checks run
   after the step *claims* the task is done:
   - `command` (exit code)
   - `file_exists`
   - `file_contains`
   - `output_contains`
   - `output_matches`

   Paths and commands run in the run's `workspace/` directory. A failed check turns
   the step into an ordinary failure (retry, then replan). The run passes through
   the `verifying` state, with `verification_started` / `verification_passed` /
   `verification_failed` events that include per-check detail. The model is told
   the workspace path and which checks will run.

   Planner-written `VERIFY:` commands have the same power as the model's `bash`
   tool, so they add no new capability.

5. **Per-thread cancel scopes.** `process_chat` polls `_turn_cancel_event()`: a
   thread-local event if one is set, otherwise the global chat-UI event. Harness
   steps run through `_scoped_process_chat` with a private event. As a result:
   - the chat Stop button no longer stops harness steps, and a run's pause/cancel
     no longer stops chat turns;
   - a watcher thread sets a step's event when the run gets a pause or cancel
     request, so these now interrupt an in-flight step within about 0.5s;
   - an interrupted step is recorded as `interrupted`, consumes no failure budget,
     and its task re-runs on resume.

## Still open

- `_last_injected_ids` and `_INJECTED_STRATEGY_IDS` are still module globals. Harness
  steps are serialized among themselves, but they can interleave with a concurrent
  chat turn. The fix is a per-call turn context — about 15 call sites.
- A crash *during a replan* resumes with the old pending tasks (the replan is lost,
  not corrupted). A crash during the *initial* plan re-plans correctly on resume.
- Verification is deterministic only. LLM-judged verification is Phase 5.

# ADR 0001 — Durable run ledger and run engine (Phase 1)

Status: accepted (2026-09-27). Context: `docs/harness/00-architecture-audit.md`.

## Decisions

1. **The harness is core, not an extension.** It lives in `proxy/mneme/harness/`, is
   stdlib-only, and never imports `mneme_proxy`. The proxy binds it at startup
   (`_init_harness`), the same pattern as `mneme/capability.py` and `mneme/tools.py`.
   If the harness fails to start, only the harness is disabled; chat and memory keep working.

2. **The ledger is a separate SQLite file**: `<db_dir>/harness.db`, which can be
   overridden with `harness.db_path`.
   - `mneme.db` is untouched, so the change is purely additive.
   - It sits beside the *shared* memory DB, so every proxy sharing a DB directory
     sees every run.
   - It has its own connection and lock, so it never contends with the proxy's
     `_LockedConnection`.

3. **Events are append-only, enforced by the database.** Triggers reject `UPDATE`
   and `DELETE` on `events`. Summaries and status are derived from them, and
   model-written text is never the authoritative history.

4. **One executor per run, across processes.** Running a run requires an atomic
   `claim` (owner = `host:pid:tag`, plus a heartbeat every lease/4 seconds).
   Recovery only reclaims a run when:
   - its owner is on this host and its pid is dead, or
   - its heartbeat is older than `harness.lease_seconds` (default 120).

   An owner on a different host is never presumed dead.

5. **Steps run at least once, and auto-resume is off.** A step cut off by a crash is
   marked `interrupted` and re-executes on resume. Tool side effects (`bash`,
   `write`) are not idempotent, so interrupted runs come back as `paused` with a
   `run_interrupted` event. `harness.auto_resume: true` opts in to resuming them
   automatically. Completed tasks never re-run.

6. **Checkpoint after every step.** A crash loses at most the in-flight step. The
   authoritative checkpoint is in the ledger; a JSON copy is written to
   `runs/<id>/checkpoints/` for people to read. `restore_checkpoint` rolls task
   state back, and records the restore itself as an event.

7. **Pause and cancel are cooperative.** A request sets `runs.control`, and the
   worker acts on it at the next step boundary. An in-flight model call finishes first.

8. **One harness step = one `process_chat` turn** (reuse, not replacement). The
   harness decides success from what *it* observed:
   - grade `F` → failure;
   - `done_reason` of `timeout`, `error` or `cancelled` → failure;
   - empty output → failure;
   - tool calls it cannot execute → failure.

   It never relies on the model's own "done" claim. Steps are serialized behind one
   lock because `process_chat` keeps turn-scoped globals.

9. **Budgets are enforced by the engine before each step.** The budget keys are
   `max_steps` (default 100), `max_failures` (default 3), `max_model_calls`,
   `max_tool_calls`, `max_runtime` (seconds summed across resumes) and `max_cost`.
   `max_replans` is stored but not enforced until Phase 2.

## Known limitations (accepted for Phase 1)

- `model_calls` counts harness turns, not the re-queries inside `process_chat`'s tool loop.
- The chat UI's Stop button (the global `_cancel_event`) also stops an in-flight
  harness step. That step fails as `done_reason=cancelled` and is retried within budget.
- A chat request running at the same time can interleave with a harness step through
  `process_chat`'s globals (the same as two concurrent chat requests today).
- There is no planner yet: tasks are given by the caller, or default to one task equal
  to the goal.
- `/runs` has no auth. It inherits the proxy's binding (127.0.0.1 by default), and a
  run has exactly the tool power of a chat turn.

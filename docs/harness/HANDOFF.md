# Harness work — handoff / resume point

Last updated: 2026-09-27. Branch: `agent-harness` (base commit `c4574fa`).
**Nothing is committed yet.** The repo still runs: `proxy/mneme_proxy.py` has not
been touched, so the proxy behaves exactly as it did before this work.

---

## 1. Where we are in the plan

| Phase | Status |
|---|---|
| 0 — Architecture audit | **Done** → `docs/harness/00-architecture-audit.md` |
| 1 — Persistent run engine | **About 70% done.** The core package and its tests are finished. It is not yet wired into the proxy (see §4). |
| 2–10 | Not started |
| Side request: swarm-creation skill | Not started (see §4, step 8) |

## 2. Uncommitted changes (from `git status`)

```
 M proxy/mneme/instructions.py      added the harness_task_context prompt + its INSTRUCTION_META entry
?? docs/harness/                    00-architecture-audit.md, HANDOFF.md (this file)
?? proxy/mneme/harness/             the new package (below)
?? tests/test_harness_ledger.py     20 tests, passing
?? tests/test_harness_engine.py     22 tests, passing
?? opencode.json                    pre-existing, not ours — leave it alone / don't commit it
```

Suggested first commit when you pick this up:
`feat(harness): Phase 0 audit + Phase 1 run ledger/engine (not yet wired into proxy)`.

## 3. What was built (Phase 1 core)

Everything is in `proxy/mneme/harness/`. It uses only the standard library and
**never imports `mneme_proxy`**. The proxy binds it at startup, following the
same pattern as `mneme/capability.py` and `mneme/tools.py`, so it can be tested
against a temp directory.

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `Ledger`, `RunEngine`, `StepResult`, `StepContext`, `RunWorkspace`, and the errors. |
| `ledger.py` | SQLite store in its own file (`harness.db`, separate from `mneme.db`). Tables: `runs`, `tasks`, `steps`, `tool_calls`, `events`, `artifacts`, `checkpoints`, `harness_meta` (schema_version=1). Triggers reject UPDATE/DELETE on `events`, so the event log can only grow. Also holds the run state machine (`RUN_TRANSITIONS`; a terminal state can only be left via retry, `failed/cancelled → created`) and a cross-process lease (`claim` / `heartbeat` / `release` / `orphaned_runs`). An owner is `host:pid:tag`. It counts as dead only if it is on the same host and its pid is gone, or its heartbeat is older than the lease. |
| `engine.py` | `RunEngine(ledger, executor, runs_root, lease_seconds)`. The executor is `executor(StepContext) -> StepResult`. Covers: `create`, `start` (background thread), `execute` (blocking), `pause`, `resume(checkpoint_id=None)`, `cancel`, `retry`, `checkpoint`, `restore_checkpoint`, `recover(auto_resume=False)`, `wait`. Budgets: `max_steps` (default 100), `max_failures` (default 3), `max_model_calls`, `max_tool_calls`, `max_runtime`, `max_cost`, and `max_replans` (stored but not enforced until Phase 2). A checkpoint is taken after every step. |
| `workspace.py` | `RunWorkspace(root, run_id)` creates `input/ workspace/ artifacts/ logs/ checkpoints/`. `resolve()` refuses paths that escape the workspace. Checkpoints are mirrored as JSON files there, but the ledger is the source of truth. |
| `chat_executor.py` | `make_chat_executor(process_chat)`. One step = one `process_chat` call with `session_id=run:<id>`. The run-context system message comes from the editable `harness_task_context` instruction. The harness decides whether the step succeeded from the grade, `done_reason`, empty output, and any tool calls it could not execute — not from the model saying it is done. The tool trace is mapped into ledger tool calls. Steps are serialized behind one lock. |
| `http.py` | `register(app, get_engine, respond)` adds these routes: `POST/GET /runs`, `GET /runs/<id>[?events=1]`, `GET /runs/<id>/events?after=&types=`, `GET /runs/<id>/checkpoints[/<cp>]`, `POST /runs/<id>/{pause,resume,cancel,retry,checkpoint}`, `POST /runs/<id>/artifacts`. Status codes: 404 unknown run, 409 invalid transition, 400 bad input, 503 harness disabled. |

### Key semantics (so the next session doesn't re-derive them)

- **Tasks:** Phase 1 has no planner. Tasks are passed in at creation; if none are given, there is one task equal to the goal.
- **Step results:** `StepResult.done=False` means "keep working on the same task". `ok=False` counts toward `max_failures`, and the task is retried until that limit is reached. With `retryable=False`, the run fails immediately.
- **Pause and cancel** are cooperative. If a worker is running the run, they set `runs.control` and take effect at the next step boundary. If nothing is running it, they apply immediately.
- **Steps run at least once.** A step cut off by a crash is marked `interrupted` and runs again on resume. For that reason `recover()` puts orphaned runs into `paused` (with a `run_interrupted` event) and auto-resume is opt-in. Completed tasks are never re-run.
- **Workers:** `execute()` marks any leftover `running` steps as `interrupted` once it holds the claim. It releases the claim before returning and re-reads the run afterwards.
- **Retry:** failed, cancelled and running tasks go back to `pending`. `usage.failures` resets and `attempt` goes up by one. Completed tasks are kept.

## 4. Next steps, in order

1. **Config keys.** In `proxy/mneme_proxy.py`, add these to `_CONFIG_ENV_MAP` (around line 110). Unknown yaml keys abort startup, and `tests/test_generated_config.py` checks the map.
   `harness.enabled→MNEME_HARNESS` (default "1"), `harness.db_path→MNEME_HARNESS_DB`
   (default `<DB_DIR>/harness.db`), `harness.runs_dir→MNEME_RUNS_DIR` (default
   `<DB_DIR>/runs`), `harness.auto_resume→MNEME_HARNESS_AUTO_RESUME` (default "0"),
   `harness.lease_seconds→MNEME_HARNESS_LEASE` (default 120).
2. **Wiring.** Place this just before `if FLASK_OK:\n    app = Flask(...)` (around line 6712), after `process_chat` is defined:
   ```python
   HARNESS = None
   def _init_harness():
       global HARNESS
       if os.environ.get("MNEME_HARNESS", "1") != "1": return
       try:
           from mneme.harness import Ledger, RunEngine
           from mneme.harness.chat_executor import make_chat_executor
           led = Ledger(os.environ.get("MNEME_HARNESS_DB") or os.path.join(DB_DIR, "harness.db"))
           HARNESS = RunEngine(led, make_chat_executor(process_chat),
                               runs_root=os.environ.get("MNEME_RUNS_DIR") or os.path.join(DB_DIR, "runs"),
                               lease_seconds=float(os.environ.get("MNEME_HARNESS_LEASE", "120")))
       except Exception as e:
           _log_error("harness:init", e); HARNESS = None
   ```
   - Inside the Flask block: `from mneme.harness import http as _harness_http; _harness_http.register(app, lambda: HARNESS, _cors_response)`.
   - In the startup block, after `_dump_config()` (around line 8181): call `_init_harness()`, then `HARNESS.recover(auto_resume=os.environ.get("MNEME_HARNESS_AUTO_RESUME")=="1")` if `HARNESS` exists. Log `[HARNESS] enabled db=…`.
   - Do **not** make `_reset_memory` wipe runs. Runs are history.
3. **Existing bug found in the audit.** In `chat_completions`, the `else:` branch (non-`/v1`, non-stream; around lines 7391–7411) builds `resp` but never returns it, so Flask returns a 500. Add `return resp`, plus a regression test that POSTs to `/api/chat/completions` with `process_chat` stubbed.
4. **`tests/test_harness_proxy.py`.** Set up the environment before importing, copying the first ~50 lines of `tests/test_tool_loop.py` (temp `MNEME_CHUNK_DIR`, empty config, dead Ollama URL). Import `mneme_proxy as mp`, then set `mp.HARNESS.executor = make_chat_executor(fake_process_chat)`. Use `mp.app.test_client()` to cover: create → wait → detail; events; pause/resume; 404 and 409 responses. Also test that the chat executor treats grade `F`, empty output, and unexecuted tool calls as failures.
5. **Full regression run:**
   `cd tests; for f in test_*.py; do timeout 300 python3 $f >/dev/null 2>&1; echo "$f $?"; done`
   The baseline is 19/21 green. `test_mcp_client` and `test_mcp_endpoints` fail because the installed `mcp` package has no `mcp.server.mcpserver`; that is environmental and not caused by this work. The `instructions.py` change has **not** been run against the full suite yet. Check `test_templates.py` and `test_tool_loop.py` especially, in case either asserts on the instruction count or names.
6. **ADR.** Write `docs/harness/adr/0001-run-ledger.md`. Decisions to record:
   - a separate `harness.db` next to the shared DB, so all proxies see every run;
   - append-only events enforced by triggers;
   - the lease and heartbeat;
   - at-least-once steps, with auto-resume off by default;
   - the proxy's globals, and why harness steps are serialized;
   - `model_calls` counts harness turns, not the re-queries inside `process_chat`'s tool loop;
   - the chat UI's `/cancel` (the global `_cancel_event`) also stops an in-flight harness step.
7. **Docs.**
   - README: add an "Agent harness (runs)" section with curl examples.
   - AGENTS.md: add the `/runs` endpoints to §4.2 and `harness.*` to the §3.2 key table.
   - `mneme.yaml.example`: add a commented `harness:` block.
8. **Swarm skill (explicitly requested by the user).** Create `skills/swarm-creation/SKILL.md` with OpenCode-style frontmatter (`name`, `description`). It must cover every option in `extensions/swarm/SWARM_REFERENCE.md`:
   - top-level: `ollama_url`, `timeout`, `max_steps`;
   - step fields: `name`, `backend`, `port`/`model`, `options`, `system_prompt`, `retry`, `timeout`, `delay`, `every`, `read_dir` (string or list, `NO_INPUT`), `skip_if_empty`, `write_dir`/`append_dir` (the extension rule), `edit_dir` with `min_similarity` and SEARCH/REPLACE, `copy_dir`/`copy_to`, `move_dir`/`move_to`, `swap_dir`, `clear_dir`, `goto`, both forms of `if`, `END`, `exec`, and `parallel:`;
   - the step execution order, the startup validation rules, gotchas, hot reload, and ready-to-copy templates (the freeze/consume loop, the review loop).

   Also document two behaviours found in the code that the reference doesn't mention:
   - `parallel:` sub-steps are **not** checked by `_validate_flow`, so validate them by hand.
   - A throttled `every` step still follows its `goto`/`if`.

   The skill must say that swarm stays an extension and runs over HTTP only.
9. Then **Phase 2 (planning loop).** First add the missing characterization tests listed in audit §8 (the static/dynamic injection split, cancel-flag semantics). Then have a planner step produce tasks, using natural language plus existing-style tags (e.g. `PLAN:` lines as parsed in `mneme/overcome.py`), and wire in `max_replans`.

## 5. How to run the new tests

```
cd tests
python3 test_harness_ledger.py     # 20 tests, ~5s
python3 test_harness_engine.py     # 22 tests, ~4s, includes a real subprocess crash + resume
```

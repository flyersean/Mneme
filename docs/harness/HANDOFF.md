# Harness work — handoff / resume point

Branch: `agent-harness`. Last updated 2026-09-28.
The overall spec and per-requirement status is in **`docs/harness/SPEC.md`**.

## Status

| Phase | Status |
|---|---|
| 0 Audit · 1 Run engine · 2 Planning/verification | ✅ committed (`7f9564e` and earlier) |
| 3 Skills / strategy versions / tool metadata | ✅ code + tests — **UNCOMMITTED** |
| 4 Focused context + permission grants | ✅ code + tests — **UNCOMMITTED** |
| 5–10 | ⬜ not started |

## ⚠ First thing to do when resuming

The last session was interrupted **during the full regression run, before the commit**.
The Phase 3–4 work is sitting uncommitted in the working tree:

```
 M proxy/mneme/harness/{chat_executor,engine,http}.py  proxy/mneme/instructions.py
 M proxy/mneme_proxy.py  tests/test_harness_proxy.py
?? proxy/mneme/harness/{capabilities,context,skills}.py  proxy/mneme/strategy_history.py
?? tests/test_harness_capabilities.py   docs/harness/SPEC.md
?? opencode.json   <- pre-existing, not ours; never commit it
```

These four suites passed individually right before the interruption:
`test_harness_capabilities` (10), `test_harness_proxy` (12), `test_harness_planning`
(21), `test_harness_engine` (22).

1. Run the full suite:
   `cd tests; for f in test_*.py; do timeout 300 python3 $f >/tmp/opencode/t.log 2>&1 || echo "FAIL $f"; done`.
   The expected failures are only `test_mcp_client` and `test_mcp_endpoints`: the
   installed `mcp` package lacks `mcp.server.mcpserver`, which predates this work.
   Watch `test_tool_loop` in particular. The Phase 3 edits touched `_save_strategy`
   (it gained `created_by` / `reason` parameters and strategy-history snapshots) and
   `process_chat`'s tool sets (grant filtering). Its instruction-sync test also
   checks the two new `{{capabilities}}` placeholders.
2. Commit, excluding `opencode.json`:
   `git add -A proxy tests docs && git commit -m "feat(harness): Phase 3-4 — skills registry, strategy versions, tool capabilities, focused context"`
3. Write `docs/harness/adr/0003-skills-capabilities.md` (decisions below).

## What Phase 3–4 added

- **`proxy/mneme/strategy_history.py`**:
  - adds an append-only `strategy_versions` table and the `created_by`,
    `derived_from` and `validated_by` columns;
  - `_save_strategy` and the inline `STRATEGY:` save in `chat_completions` now
    snapshot the replaced row and the new row;
  - new endpoint: `GET /strategies/<id>/history`.
- **`harness/skills.py`** (`SkillRegistry`, tables in `harness.db`):
  - loads `skills/*/SKILL.md` and `<db dir>/skills/*/SKILL.md`;
  - `upsert` bumps the version only when content changed; `history` and
    `restore_version` (a restore is recorded as a new version);
  - `select`: lexical selection (embedding if bound), with `requires` expansion;
  - `record_outcome` keeps per-skill stats;
  - endpoints: `/skills`, `/skills/<name>`, `/skills/<name>/restore`.
- **`harness/capabilities.py`**:
  - built-in tool metadata: permission level, risk, cost, what the tool does, and a
    verify hint;
  - `PERMISSION_LEVELS`; unknown and MCP tools count as `system`;
  - `normalize_grant`, `allowed`, `select_tools`.
- **`harness/context.py`** (`CapabilityContext`): per step, the top 2 skills with
  their procedure and failure modes, plus up to 4 relevant tools with permission and
  verify hints, restricted to the run's grant. Planning gets the brief form. This
  fills the new `{{capabilities}}` placeholder in `harness_task_context` and
  `harness_plan`.
- **Permission grants in the proxy.** `_scoped_process_chat(..., tool_grant=set)`
  sets a thread-local grant that `_turn_tool_ok` applies. Ungranted tools are
  removed from `msg_tools` **and** from the server-exec name sets. If the model calls
  one anyway, the call passes through to the client, so the harness step fails. A
  run's grant comes from `run.permissions["grant"]`; `None` means unrestricted, the
  same power as chat.
- **Engine.** The constructor takes `capabilities=` and `skills=`. A new `_end()`
  choke point for completed/failed runs calls the `on_finish` hooks. Skill outcomes
  are recorded from the `meta.skills` field of each step.

**Decisions for ADR 0003:**

- Skills live in `harness.db`. Files are a source; the DB holds the versions.
- A restore is recorded as a new version.
- Selection is lexical by default, which is cheap and needs no embedder.
- The default grant for runs is unrestricted, matching chat; profiles narrow it.
- Tools not in the metadata table count as `system` (the conservative choice).

## Next: Phase 5 → 10 (condensed plan; details in SPEC §7)

5. **Verification & recovery.**
   - `harness/failures.py`: `classify(error, meta)` returns `verification`, `tool`,
     `empty`, `provider`, `fabricated`, `budget`, `interrupted`, `unexecutable` or
     `other`. Store the category in step meta and in the `task_failed` event.
   - Add an `llm_judge` check type to `verify.py`. It takes a `judge` callable; in
     the proxy, back it with a short `query_model` PASS/FAIL prompt.
   - Approvals: a task with `requires_approval` (or a profile with
     `approve_each_task`) puts the run into `awaiting_approval`. Add
     `POST /runs/<id>/approve` and `/reject`; a rejected task fails and then replans.
6. **Self-improvement** (`harness/evolution.py`, in `harness.db`).
   - Tables: `proposals` (kind, target, level, status, content, previous, reason,
     evidence, tests, result) and an append-only `evolution_log`.
   - Appliers: `instruction` (via `instructions.save_instruction`, with the previous
     text kept), `skill` (upsert), `profile`, `knowledge` (L1, auto-applied), and
     `code`. Code is L4: `git worktree` on branch `evolve/<id>`, then `git apply`,
     then the test command, then record. It never merges automatically.
   - Levels: L1 and L2 apply automatically; L3 and L4 need tests to pass and an
     approval. If verification fails after applying, roll back and keep the failed
     attempt.
   - An `on_finish` hook records failure observations (L1). An optional reflection
     turn (`harness.reflect`, default off to save spend) proposes skill updates.
7. **Profiles.**
   - A `profiles` table plus built-ins: default, researcher, coder. Each has skills,
     a grant, budget defaults, `plan`, and `approve_each_task`.
   - `engine.create(profile=)` merges the profile in.
   - Pass `permissions` and `profile` through `POST /runs`.
   - An artifact auto-capture hook scans `workspace/artifacts` at the end of a run.
8. **Control plane.**
   - `harness/commands.py`: `/help /status /runs /plan /tasks /pause /resume /cancel
     /retry /replan /approve /reject /skills /tools /strategies /log /jobs /config /models`.
   - Hook them at the top of `process_chat`, only for known command words. Add
     `POST /harness/command`.
   - Add a `static/runs.html` page (runs, run detail and events, skills,
     evolution) and a "Runs" link in every page's nav (the nav is duplicated per
     static HTML file).
9. **Jobs and gateways.**
   - A `jobs` table and a scheduler thread (interval-based, lease-claimed), with a
     `/jobs` API.
   - `extensions/gateways/{base.py, cli.py, telegram.py, README.md}` as HTTP clients
     of `/harness/command` and `/runs`.
10. **Swarm on runs.**
    - Add `POST /runs/<id>/events` and an external-run status endpoint (only allowed
      when `meta.external`).
    - The engine must refuse to execute external runs, and `recover()` must skip them.
    - Swarm config gains a `harness: {port}` block. The orchestrator records the run,
      each step, and each artifact over HTTP; `parallel` sub-steps become child runs;
      `--resume-run <id>` re-anchors on the last completed step.
    - Update `SWARM_REFERENCE.md` and the swarm skill.

## Known limitations

See `SPEC.md` §10.

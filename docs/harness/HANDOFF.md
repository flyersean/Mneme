# Harness work — handoff / resume point

Branch: `agent-harness`. Last updated 2026-09-28.
The overall spec and per-requirement status is in **`SPEC.md`**. Decisions are in
`adr/0001`–`0004`, and the starting audit is `00-architecture-audit.md`.

## Status: phases 0–10 are built and committed

| Phase | Commit |
|---|---|
| 0–1 audit + run engine | `33f32f7`, `9b23a3a` |
| 2 planning / verification / cancel scopes | `7f9564e` |
| 3–4 skills, strategy versions, capabilities, focused context | `b513c7a` |
| 5 failure classes, llm_judge, approvals | `5440575` |
| 6 self-improvement | `8ffa444` |
| 7 profiles + artifact capture | `98e8074` |
| 8 commands, metrics, dashboard | `457e766` |
| 9 jobs + gateways | `59f0822` |
| 10 swarm on runs | `558b309` |
| docs (ADR 0004, SPEC update, README/AGENTS/yaml) | the commit after `558b309` |

`opencode.json` in the working tree is pre-existing and not ours. Never commit it.

## Tests

Run the whole suite:

`cd tests; for f in test_*.py; do timeout 300 python3 $f >/dev/null 2>&1 || echo "FAIL $f"; done`

Everything passes except `test_mcp_client` and `test_mcp_endpoints`. They fail
because the installed `mcp` package has no `mcp.server.mcpserver`, which predates
this work.

The harness suites:

| Suite | Covers |
|---|---|
| `test_harness_ledger` | ledger |
| `test_harness_engine` | engine, including a real crash and resume |
| `test_harness_planning` | planning and replanning |
| `test_harness_capabilities` | skills, tools, context |
| `test_harness_recovery` | failure classes, judge, approvals |
| `test_harness_evolution` | self-improvement, including a real git repo for L4 |
| `test_harness_profiles` | profiles, artifacts |
| `test_harness_commands` | commands, metrics |
| `test_harness_jobs` | jobs |
| `test_gateways` | CLI and Telegram gateways |
| `test_harness_proxy` | the harness through the real proxy app |
| `test_process_chat_characterization` | pinned `process_chat` behaviour |
| `test_swarm_harness` | real HTTP swarm, fail and then resume |

Watch `test_harness_proxy`: it failed once early in Phase 2 and could not be
reproduced afterwards.

## Recommended next work (hardening + evaluation, in priority order)

1. **Live-model evaluation (SPEC R29).** Every test so far uses scripted models.
   - Build `tests/agent_tasks/`: repeatable tasks in the brief's tiers (simple,
     medium, hard, self-improvement).
   - Run each task via `POST /runs` against real 3B, 30B and frontier proxies.
   - Record `/harness/metrics` for each.
   - Tune `harness_plan`, `harness_task_context`, `harness_judge` and
     `harness_reflect` through L3 evolution proposals, so every change is versioned.
   - The key experiment: a task the system fails → reflection → a skill proposal →
     retry → success.
2. **A short system prompt for runs (R13).** Harness steps currently get the full
   `system_prompt.md`. Add a compact instruction (`system_prompt_harness`) chosen
   when `_cancel_local.event` is set, and measure the difference on a small model.
3. **Per-call turn context.** Move `_last_injected_ids` and `_INJECTED_STRATEGY_IDS`
   into thread-local or per-call state, as was done for the cancel event and the
   tool grant. That removes the last way harness steps and chat turns interfere.
4. **Per-run strategy feedback (R9).** Record the strategy ids injected during a
   run's steps (return them from `process_chat`), and credit them with the run's
   outcome in `on_finish`.
5. **Auth.** Nothing on `/runs`, `/evolution` or `/jobs` is authenticated. At a
   minimum, document putting the reverse proxy with `MNEME_GATEWAY_TOKEN` in front
   of it. Better: require a token for evolution approvals and L4 proposals.
6. **Smaller items:**
   - bind the embedder into `SkillRegistry.select`;
   - add versioning for tool definitions (R16);
   - persist an in-progress replan so a crash during replanning doesn't lose it;
   - show job controls in `/runs/ui`.

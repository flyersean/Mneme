# Mneme Agent Harness — Specification

The overall spec for turning Mneme into a persistent, extensible agent harness. It
condenses the original mission brief into requirements, and maps each requirement
to what exists in the code today and what is still to build. Decisions are recorded
in `adr/`, the starting audit is in `00-architecture-audit.md`, and the current
resume point is `HANDOFF.md`.

---

## 1. Mission

**Mneme is the agent system; the model is one component of it.** A relatively small
model should be able to do increasingly complex work, because the harness around
it supplies:

- persistent memory, strategies and skills
- tools
- planning, execution and verification
- retrieval
- task state and checkpoints
- provenance
- controlled self-modification

The long-term goal is recursive self-improvement (RSI) **without training**. The
system improves by accumulating and modifying external state: strategies, skills,
tools, prompts, workflows, knowledge, retrieval structures, configuration, code,
evaluation procedures, and experience.

Two principles:

- Nothing useful is casually discarded. A failed attempt becomes information, and a
  success becomes reusable knowledge.
- Do not rewrite Mneme. Evolve its existing systems additively, and keep the
  repository runnable at every step.

## 2. Architectural rules (binding)

1. Do not rewrite working components just to make them look cleaner.
2. **The harness owns state.** Never depend on the model remembering it.
3. **The model proposes; the harness executes and verifies.**
4. Natural language stays acceptable, with lightweight line tags, not forced JSON.
5. Everything important is persistent.
6. Everything that changes the system has provenance.
7. Prefer additive evolution over destructive replacement. Version instead of
   overwriting.
8. The provider layer stays replaceable (OpenAI, Anthropic, OpenRouter, DeepSeek,
   Ollama, any OpenAI-compatible endpoint), with no provider assumptions in the core.
9. The core stays model-agnostic: the same harness serves a 3B, a 30B, and a
   frontier model.
10. Optimize for **capability per unit of context**.

Repository conventions (from `AGENTS.md`):

- Extensions talk to proxies over HTTP only.
- Config keys are registered in `_CONFIG_ENV_MAP`, and unknown keys abort startup.
- Tests are standalone `python3 tests/test_*.py` scripts.

## 3. Core abstractions

```
Session   conversation / interface state
Run       one execution of a user goal (survives interruption and restart)
Task      a meaningful piece of work within a run
Step      one executed action (plan | model | verify)
ToolCall  interaction with an external capability
Artifact  file/result produced by a run
Event     immutable record of something that happened (append-only)
Memory    persistent knowledge extracted from experience (mneme.db chunks)
Skill     reusable, versioned, composable capability (procedure + tools + verification)
Strategy  reusable operational knowledge, versioned, with success/failure history
Job       scheduled/recurring source of runs
Profile   reusable behaviour/capability selection for a run (skills, permissions, budgets)
Proposal  a candidate self-modification with level, evidence, tests, and outcome
```

**Run fields:**

- id, parent run, session, goal, status;
- plan (versioned), current task and step;
- profile, model, workspace;
- budget and usage, permissions, approval state;
- control request (pause or cancel);
- result or error, attempt number;
- owner and heartbeat lease;
- meta, provenance (`created_by`), timestamps.

**Run states:**

```
created planning running waiting paused awaiting_approval verifying completed failed cancelled
```

The state machine is deliberately permissive between the non-terminal states. A
terminal state can only be left through `retry` (failed or cancelled → created).

## 4. Control loop

```
GOAL → PLAN → SELECT skills/strategies/tools → EXECUTE → OBSERVE → VERIFY
  ├─ pass → next task … → COMPLETE
  └─ fail / new information → retry (budget) → REPLAN (max_replans) → EXECUTE
```

Tag protocol for model → harness messages. These are line-oriented and tolerant of
markdown:

| Tag | Meaning |
|---|---|
| `PLAN: <task>` | a task in the plan (planner turn) |
| `VERIFY: <shell command>` | an exit-0 check for the preceding task |
| `REPLAN: <why>` | the remaining plan is wrong, so replan |
| `[TOOL:SUCCESS]` / `[TOOL:FAILURE: why]` | tool-outcome tags (existing) |
| `[source: X]` / `[guess]` | provenance tags, graded deterministically (existing) |

User → harness controls follow the existing `<<…>>` chat commands, plus
`/`-commands (Phase 8).

## 5. Responsibility split

| Model | Harness |
|---|---|
| understand goals, reason, propose plans | persistence, state, event log, checkpoints |
| choose capabilities, decide what info is needed | execution, tool invocation, permissions |
| use tools, interpret observations | budgets, approvals, run control, recovery |
| propose modifications, flag uncertainty | verification infrastructure |
| suggest verification, learn from outcomes | memory / strategy / skill / profile storage, versioning, provenance |

## 6. Requirements by area, with implementation status

Status: ✅ done · 🟡 partial · ⬜ not started.

| # | Area | Requirement (summary) | Status | Where |
|---|---|---|---|---|
| R1 | Run ledger | runs, tasks, steps, tool_calls, events (append-only), artifacts, checkpoints; answers "what happened?" without the model | ✅ | `harness/ledger.py` (triggers enforce append-only) |
| R2 | Checkpoint/resume | checkpoint/resume/pause/cancel/retry; survives model, tool, network and process failures, context exhaustion, and user interrupts | ✅ | `harness/engine.py`; checkpoint after every step; `recover()`; real kill-9 test |
| R3 | Multi-process safety | several proxies share one ledger; one executor per run | ✅ | claim + heartbeat lease |
| R4 | Planning | goal → plan in natural language + tags; fallback so small models never dead-end | ✅ | `harness/planning.py`, `make_chat_planner`, `harness_plan` instruction |
| R5 | Replanning | on a dead end or `REPLAN:`; superseded tasks kept; bounded | ✅ | `engine._plan(mode="replan")`, `max_replans` |
| R6 | Separation | model proposes → harness validates / executes / observes / verifies → state update | ✅ | the step's success is judged by the harness (grade, `done_reason`, empty output, unexecutable calls, verification) |
| R7 | Deterministic verification | command exit, file exists/contains, output contains/matches | ✅ | `harness/verify.py`, `verifying` state + events |
| R8 | LLM verification | supplements deterministic checks, never replaces them | ⬜ | Phase 5: `llm_judge` check type |
| R9 | Strategies | problem type, procedure, failure modes, verification, success/failure history, confidence, provenance, version, parent | 🟡 | existing table + telemetry; **✅ version history + provenance** (`strategy_history.py`); per-run feedback ⬜ |
| R10 | Skills | reusable, composable, versioned; description, tools, strategies, verification, failure modes, stats | ✅ | `harness/skills.py`, `SKILL.md` loading, `/skills` API |
| R11 | Tools as capabilities | metadata: permission, risk, cost, verification, examples | ✅ | `harness/capabilities.py` (built-ins; MCP/unknown → `system`) |
| R12 | Capability selection | retrieve the relevant memories, strategies, skills and tools → small focused context | 🟡 | `harness/context.py` (skills + tools per step); memory/strategies via `process_chat`; per-run tool narrowing through permission grants |
| R13 | Prompt architecture | short static system prompt; dynamic harness context separate; conversation separate | 🟡 | static block + dynamic tail pinned by characterization tests; harness context messages. A dedicated short system prompt for runs ⬜ |
| R14 | Self-improvement loop | observe → identify → propose → evaluate → approve/test → apply → verify → record | ⬜ | Phase 6 |
| R15 | Modification levels | L1 knowledge (auto), L2 strategies/skills (auto, versioned), L3 prompts/config/profiles (tested + previous kept), L4 tools/code (branch → test → verify → activate) | ⬜ | Phase 6 |
| R16 | Versioning | strategies, skills, prompts, tool definitions, profiles | 🟡 | strategies ✅ skills ✅; prompts, profiles, tools ⬜ |
| R17 | Provenance | created_by / derived_from / validated_by for every learned or modified object; known vs inferred vs verified | 🟡 | chunks (curation) ✅, strategies ✅, skills (source/actor) ✅, events actor ✅; proposals ⬜ |
| R18 | Budgets | max model calls, tool calls, replans, runtime, failures, cost (+ steps) | ✅ | `engine.DEFAULT_BUDGET` (usage in the ledger) |
| R19 | Agent profiles | skills, tools, permissions, verifier, budget defaults | ⬜ | Phase 7 (the grant mechanism already exists) |
| R20 | Workspaces | `runs/<id>/{input,workspace,artifacts,logs,checkpoints}` | ✅ | `harness/workspace.py`; the model is told the path |
| R21 | Artifacts | run/task, path, type, checksum, provenance, description | 🟡 | ledger + `POST /runs/<id>/artifacts` ✅; auto-capture on completion ⬜ |
| R22 | Control commands | /help /status /plan /tasks /runs /pause /resume /cancel /retry /replan /approve /reject /tools /skills /strategies /memory /search /files /jobs /log /config /models | ⬜ | Phase 8 (the HTTP equivalents exist for runs and skills) |
| R23 | Dashboard | runs list/detail, event viewer, strategy/skill browser, system-evolution view | ⬜ | Phase 8 (evolve the existing static pages) |
| R24 | Background jobs | job → run; scheduling; recurring work | ⬜ | Phase 9 |
| R25 | Gateways | generic Gateway (receive, send, authenticate, identify_user, authorize); CLI, Web, Telegram | ⬜ | Phase 9, as HTTP clients. Not `proxy/gateway.py`, which is a reverse proxy. |
| R26 | Approvals/permissions | permission levels; approval for dangerous actions | 🟡 | levels + grants enforced in `process_chat` ✅; `awaiting_approval` flow ⬜ (Phase 5/7) |
| R27 | Transactional self-mod | snapshot → change → test → verify → activate → record; roll back on failure and keep the attempt | ⬜ | Phase 6 |
| R28 | Swarm on the harness | swarm = parent run + child runs + events/artifacts; not the core | 🟡 | swarm-creation skill ✅; recording swarm runs in the ledger ⬜ (Phase 10) |
| R29 | Test strategy | repeatable agent tasks (simple → hard → self-improvement), not prose quality | 🟡 | unit + integration suites ✅; a benchmark task set ⬜ |
| R30 | Metrics | success rates, calls per task, replans, time, failure categories, strategy/skill/tool reuse and success, recovery, self-improvement success | 🟡 | raw data in the ledger (usage, events, skill stats); `/metrics` aggregation + failure classification ⬜ |

## 7. Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture audit | ✅ `00-architecture-audit.md` |
| 1 | Persistent run engine (ledger, engine, workspace, `/runs`) | ✅ ADR 0001 |
| 2 | Planning, replanning, deterministic verification, cancel scopes | ✅ ADR 0002 |
| 3 | Strategy versions, skills, tool metadata | ✅ code + tests; **not yet committed** (see HANDOFF) |
| 4 | Focused context + permission-grant tool filtering | ✅ code + tests; **not yet committed** |
| 5 | Verification & recovery: failure classification, `llm_judge`, approvals (`awaiting_approval`, /approve /reject) | ⬜ |
| 6 | Self-improvement: proposals, levels L1–L4, versioned appliers (instruction, skill, profile, knowledge, code via git branch), reflection hook, rollback | ⬜ |
| 7 | Profiles (skills, grant, budget defaults, approve-each-task), artifact auto-capture | ⬜ |
| 8 | Control plane: `/`-commands, `POST /harness/command`, runs/skills/evolution dashboard page + nav link | ⬜ |
| 9 | Jobs + scheduler; gateway base + CLI + Telegram (HTTP clients under `extensions/gateways/`) | ⬜ |
| 10 | Swarm records parent/child runs over HTTP (`harness:` config block, external runs, resume by step name) | ⬜ |

## 8. Module map (current)

```
proxy/mneme/harness/
  ledger.py         durable store, state machine, lease            (P1)
  engine.py         run loop, budgets, checkpoints, control,
                    recovery, planning/replanning, verification,
                    interrupts, _end() + on_finish hooks            (P1-P4)
  workspace.py      per-run directories                             (P1)
  chat_executor.py  task step + planner over process_chat;
                    grants, capability text, REPLAN detection       (P1-P4)
  planning.py       PLAN:/VERIFY:/REPLAN: extraction                (P2)
  verify.py         deterministic checks                            (P2)
  skills.py         versioned skill registry                        (P3)
  capabilities.py   tool metadata + permission levels               (P3)
  context.py        per-step capability context                     (P4)
  http.py           /runs, /skills routes                           (P1-P3)
proxy/mneme/strategy_history.py  strategy_versions + provenance     (P3)
proxy/mneme_proxy.py  wiring: _init_harness, _scoped_process_chat
                    (cancel scope + tool grant), _turn_tool_ok,
                    /strategies/<id>/history, harness.* config keys
skills/swarm-creation/SKILL.md   first shipped skill
```

Storage:

- `<db dir>/harness.db` holds the ledger plus the skills tables.
- `<db dir>/runs/<id>/` holds the per-run workspaces.
- `mneme.db` gains an additive `strategy_versions` table and three provenance
  columns on `strategies`.

## 9. HTTP surface (current)

```
POST /runs {goal, tasks?, plan?, budget?, profile?, permissions?*, session_id?, parent_run_id?, meta?, start?}
GET  /runs[?status=&parent=]   GET /runs/<id>[?events=1]   GET /runs/<id>/events[?after=&types=]
GET  /runs/<id>/checkpoints[/<cp>]
POST /runs/<id>/{pause,resume,cancel,retry,checkpoint}     POST /runs/<id>/artifacts
GET  /skills[?q=&k=&all=1]   POST /skills   GET /skills/<name>   POST /skills/<name>/restore
GET  /strategies/<id>/history
```

\* `permissions` is supported by `engine.create`, but is **not yet passed through by
`POST /runs`**. It is a one-line fix and belongs to Phase 7 (profiles).

## 10. Known limitations / open risks

- `_last_injected_ids` and `_INJECTED_STRATEGY_IDS` are still process globals shared
  between harness steps and concurrent chat turns.
- Steps run at least once. Auto-resume is off by default because tool side effects
  can repeat.
- A crash during a replan loses that replan; the old pending tasks resume.
- `model_calls` counts harness turns, not the model re-queries inside a turn.
- `/runs` and `/skills` have no auth; they inherit the proxy's 127.0.0.1 bind.
- Skill selection is lexical unless an embedder is bound (not bound yet in the proxy).

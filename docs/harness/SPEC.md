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

| # | Area | Status | Where / notes |
|---|---|---|---|
| R1 | Run ledger (runs, tasks, steps, tool calls, append-only events, artifacts, checkpoints) | ✅ | `harness/ledger.py` |
| R2 | Checkpoint / resume / pause / cancel / retry; survives crashes and restarts | ✅ | `harness/engine.py`; real kill-9 test; mid-step interrupts |
| R3 | Multi-process safety (one executor per run) | ✅ | claim + heartbeat lease; job compare-and-set |
| R4 | Planning (natural language + `PLAN:` / `VERIFY:`; fallback) | ✅ | `harness/planning.py`, `make_chat_planner` |
| R5 | Replanning (dead end, `REPLAN:`, user `/replan`, approval reject) | ✅ | `engine._plan`, `max_replans` |
| R6 | Model proposes → harness validates / executes / verifies | ✅ | success is judged by the harness |
| R7 | Deterministic verification | ✅ | `harness/verify.py` |
| R8 | LLM verification (supplements deterministic checks, never replaces them) | ✅ | `llm_judge`, run only after deterministic checks pass |
| R9 | Strategies: versions, provenance, history | 🟡 | `strategy_history.py` ✅; per-*run* strategy success stats ⬜ (per-turn telemetry exists) |
| R10 | Skills (versioned, composable, stats) | ✅ | `harness/skills.py` |
| R11 | Tools as capabilities (permission, risk, cost, verify hint) | ✅ | `harness/capabilities.py` |
| R12 | Capability selection → small focused context | ✅ | `harness/context.py` + grant filtering in `process_chat` |
| R13 | Static prompt / dynamic harness context / conversation separation | 🟡 | split pinned by tests; a dedicated *short* system prompt for runs ⬜ (runs still get the full `system_prompt.md`) |
| R14 | Self-improvement loop | ✅ | `harness/evolution.py`, `observe_run`, optional reflector |
| R15 | Modification levels L1–L4 | ✅ | L4 applies to a git branch only |
| R16 | Versioning (strategies, skills, prompts, profiles, tools) | 🟡 | strategies, skills, profiles ✅; prompts via proposals (previous kept) ✅; tool *definitions* ⬜ |
| R17 | Provenance on every learned or modified object | ✅ | chunks, strategies, skills, profiles, proposals, event actors |
| R18 | Budgets | ✅ | steps, failures, model calls, tool calls, replans, runtime, cost |
| R19 | Agent profiles | ✅ | `harness/profiles.py` (default, researcher, coder, reviewer, cautious) |
| R20 | Workspace per run | ✅ | `harness/workspace.py` |
| R21 | Artifacts | ✅ | ledger + automatic capture at run end |
| R22 | Control commands | ✅ | `harness/commands.py` (chat, `/harness/command`, gateways) |
| R23 | Dashboard (runs, events, skills, evolution, profiles, metrics) | ✅ | `static/runs.html` at `/runs/ui` |
| R24 | Background jobs | ✅ | `harness/jobs.py` + scheduler |
| R25 | Gateways (CLI, Telegram, future) | ✅ | `extensions/gateways/` |
| R26 | Permissions & approvals | ✅ | per-run grants; task/profile approvals |
| R27 | Transactional self-modification | ✅ | capture previous → apply → test → roll back; code on a branch |
| R28 | Swarm on the harness | ✅ | external runs, child runs, `--resume-run` |
| R29 | Test strategy with repeatable agent tasks | 🟡 | ~520 unit/integration tests (scripted models); a **live-model benchmark task set** ⬜ |
| R30 | Metrics | ✅ | `harness/metrics.py`, `/harness/metrics` |

## 7. Phase plan and status

| Phase | Scope | Status |
|---|---|---|
| 0 | Architecture audit | ✅ |
| 1 | Persistent run engine | ✅ ADR 0001 |
| 2 | Planning, replanning, verification, cancel scopes | ✅ ADR 0002 |
| 3–4 | Skills, strategy versions, tool metadata, focused context, grants | ✅ ADR 0003 |
| 5 | Failure classification, `llm_judge`, approvals | ✅ ADR 0004 |
| 6 | Self-improvement (proposals, L1–L4, rollback, observation, reflection) | ✅ ADR 0004 |
| 7 | Profiles, artifact capture | ✅ ADR 0004 |
| 8 | Commands, metrics, runs dashboard | ✅ ADR 0004 |
| 9 | Jobs + scheduler; CLI and Telegram gateways | ✅ ADR 0004 |
| 10 | Swarm as external runs, child runs, resume | ✅ ADR 0004 |

What remains is hardening and evaluation work, not a new phase. See `HANDOFF.md`.

## 8. Module map

```
proxy/mneme/harness/
  ledger.py        durable store, state machine, lease, external-run exclusion
  engine.py        run loop, budgets, checkpoints, control, recovery, planning/replanning,
                   verification, approvals, interrupts, external runs, _end() + on_finish hooks
  workspace.py     per-run directories
  chat_executor.py task step / planner / reflector over process_chat
  planning.py      PLAN:/VERIFY:/REPLAN:/LESSON:/SKILL: extraction
  verify.py        deterministic checks + llm_judge
  failures.py      failure categories
  skills.py        versioned skill registry
  capabilities.py  tool metadata + permission levels
  context.py       per-step capability context
  evolution.py     proposals, levels, appliers (knowledge/skill/instruction/profile/code), hooks
  profiles.py      versioned agent profiles
  metrics.py       ledger-derived metrics
  commands.py      /-commands
  jobs.py          jobs + scheduler
  http.py          HTTP routes
proxy/mneme/strategy_history.py   strategy_versions + provenance
proxy/static/runs.html            control-plane UI (/runs/ui)
extensions/gateways/              base.py, cli.py, telegram.py (HTTP only)
extensions/swarm/                 RunRecorder (harness: block, --resume-run)
skills/swarm-creation/SKILL.md    first shipped skill
```

Storage:

- `<db dir>/harness.db` holds the ledger plus the skills, profiles, proposals,
  evolution log, jobs and job log tables.
- `<chunk dir>/runs/<id>/` holds the run workspaces (the instance's chunk dir, so
  they're writable by the model).
- `<db dir>/evolve/` holds the L4 git worktrees.
- `<db dir>/skills/` holds user skills.
- `mneme.db` gains the additive `strategy_versions` table and provenance columns.

## 9. HTTP surface

```
Runs        POST /runs {goal, tasks?, plan?, budget?, profile?, permissions?, meta?, start?}
            GET /runs[?status=&parent=]  GET /runs/<id>[?events=1]  GET /runs/<id>/events
            GET /runs/<id>/checkpoints[/<cp>]
            POST /runs/<id>/{pause,resume,cancel,retry,checkpoint,approve,reject}
            POST /runs/<id>/artifacts
External    POST /runs/<id>/events {type,data}   POST /runs/<id>/status {status,...}   (meta.external only)
Skills      GET/POST /skills   GET /skills/<name>   POST /skills/<name>/restore
Strategies  GET /strategies/<id>/history
Profiles    GET/POST /profiles   GET /profiles/<name>
Evolution   GET/POST /evolution[?status=&kind=&target=]   GET /evolution/<id>
            POST /evolution/<id>/{test,approve,reject,rollback}
Jobs        GET/POST /jobs   GET /jobs/<id>   POST /jobs/<id>/{enable,disable,trigger}
Control     POST /harness/command {text}   GET /harness/metrics   GET /runs/ui
```

Config (`harness:`): `enabled`, `db_path`, `runs_dir`, `auto_resume`,
`lease_seconds`, `reflect`, `auto_apply_level`, `scheduler`, `scheduler_tick`.

## 10. Known limitations / open risks

- `_last_injected_ids` and `_INJECTED_STRATEGY_IDS` are still process globals shared
  between harness steps and concurrent chat turns.
- Steps run at least once; auto-resume is off by default.
- A crash during a replan loses that replan.
- `model_calls` counts harness turns, not the model re-queries inside a turn.
- **No auth on any endpoint.** Everything inherits the 127.0.0.1 bind; put the
  reverse proxy (`MNEME_GATEWAY_TOKEN`) in front of it for remote use.
- Skill selection is lexical unless an embedder is bound.
- Runs still receive the full `system_prompt.md`. A short run-specific system prompt
  would suit small models better (R13).
- Nothing has been validated against a **live model** yet. Every test uses scripted
  models, so the planner, judge and reflector prompts need tuning on real 3B, 30B
  and frontier models.

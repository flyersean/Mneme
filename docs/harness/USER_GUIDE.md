# Mneme Agent Harness — User Guide

This guide covers the `agent-harness` branch: how to install it, turn it on, run
durable agent work, and control and extend it. For the design, see
[`SPEC.md`](SPEC.md) and the ADRs in [`adr/`](adr/). For work in progress, see
[`HANDOFF.md`](HANDOFF.md).

**Contents**

1. [What the harness is](#1-what-the-harness-is)
2. [Install and enable](#2-install-and-enable)
3. [Five-minute quickstart](#3-five-minute-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Runs: create, watch, control](#5-runs-create-watch-control)
6. [Planning and the tag protocol](#6-planning-and-the-tag-protocol)
7. [Verification](#7-verification)
8. [Budgets](#8-budgets)
9. [Permissions and profiles](#9-permissions-and-profiles)
10. [Approvals](#10-approvals)
11. [Skills](#11-skills)
12. [Strategies and their history](#12-strategies-and-their-history)
13. [Self-improvement (evolution)](#13-self-improvement-evolution)
14. [Background jobs](#14-background-jobs)
15. [Chat commands and the Runs dashboard](#15-chat-commands-and-the-runs-dashboard)
16. [Gateways: CLI and Telegram](#16-gateways-cli-and-telegram)
17. [Swarms as durable runs](#17-swarms-as-durable-runs)
18. [Workspaces, artifacts, crash recovery](#18-workspaces-artifacts-crash-recovery)
19. [Multiple proxies, one harness](#19-multiple-proxies-one-harness)
20. [Metrics](#20-metrics)
21. [Configuration reference](#21-configuration-reference)
22. [HTTP API reference](#22-http-api-reference)
23. [Security](#23-security)
24. [Troubleshooting](#24-troubleshooting)

---

## 1. What the harness is

Plain Mneme is a memory proxy. It answers one chat turn at a time, with memory,
tools and provenance grading. The **agent harness** adds a durable layer on top:
you give it a **goal**, and the harness turns that goal into a **run**.

A run:

- is **planned** into tasks. The model plans, and the harness keeps the plan.
- **executes** each task as a normal Mneme turn, so memory, tools and grading all
  still apply.
- is **verified** by the harness, which checks the work itself rather than
  trusting "done".
- **recovers** from failures by retrying, then replanning, within a budget.
- **persists** everything: plan, steps, tool calls, artifacts, checkpoints, and an
  append-only event log. It survives crashes and restarts.
- **learns**: failures become stored lessons, and reusable procedures become
  versioned skills.

The model is one component. **The harness owns the state**, so a small model does
not have to remember what happened. It is told the relevant goal, task, prior
results, skills, tools and budget each step.

Nothing about ordinary chat changes. The harness is inert until you create a run,
a job, or type a `/command`.

## 2. Install and enable

Install Mneme as usual (see the main [README](../../README.md)):

```bash
git clone -b agent-harness https://github.com/flyersean/Mneme.git && cd Mneme
./scripts/install.sh            # or: python3 scripts/mneme_setup.py   (interactive wizard)
```

The harness is **enabled by default**. When the proxy starts you should see:

```
  [HARNESS] enabled db=/…/harness.db runs=/…/runs
```

To configure it, add a `harness:` block to your `mneme.yaml`. Every key is
optional, and the defaults are shown:

```yaml
harness:
  enabled: true          # false = no harness (chat unaffected)
  # db_path: <db dir>/harness.db     # run ledger + skills/profiles/proposals/jobs
  # runs_dir: <db dir>/runs          # per-run workspaces
  auto_resume: false     # resume crash-interrupted runs automatically at startup
  lease_seconds: 120     # heartbeat lease before another process may take a run over
  reflect: false         # one extra model call after failed runs to extract lessons/skills
  auto_apply_level: 2    # self-improvement levels applied without approval (1 or 2)
  scheduler: true        # run background jobs
  scheduler_tick: 15     # seconds between scheduler checks
```

`<db dir>` is the directory that contains `storage.db_path`, the shared memory DB.
As with every Mneme key, each setting can also be set through an environment
variable (see §21).

**Requirements:**

- The harness is standard-library Python, and runs inside the proxy's existing
  Flask process.
- Optional pieces have their own dependencies:
  - L4 code proposals need `git`;
  - gateways need `requests`;
  - the swarm needs `pyyaml` and `requests`.

## 3. Five-minute quickstart

Start a proxy (for example on `8080`). Then:

```bash
# 1. Give it a goal. With no tasks, the model plans first.
curl -s localhost:8080/runs -H 'Content-Type: application/json' \
  -d '{"goal": "Find the latest stable Python release and write the version to notes.txt"}'
# -> {"run": {"run_id": "run_1a0e…", "status": "created", ...}}

# 2. Watch it
curl -s localhost:8080/runs/run_1a0e…            # status, plan, tasks, steps, tool calls, artifacts
curl -s localhost:8080/runs/run_1a0e…/events     # everything that happened, in order
```

Or do it all from the chat page, with no curl at all:

```
/run Find the latest stable Python release and write the version to notes.txt
/status
/tasks last
/log last 20
```

Or open **`http://localhost:8080/runs/ui`**. The Runs link is in every page's nav.

What happens:

1. **Planning.** The model writes a short plan using `PLAN:` lines, optionally with
   `VERIFY:` checks.
2. **Execution.** Each task runs as one Mneme turn. The model gets the goal, the
   current task, earlier results, the run's workspace path, the relevant skills and
   tools, and the remaining budget.
3. **Verification.** If a task has checks, the harness runs them. A failed check
   counts as a failure, whatever the model claims.
4. **Recovery.** Failed tasks are retried. When a task dead-ends, the harness asks
   the model for a new plan for the *remaining* work.
5. **Completion.** The run ends `completed` (with the last task's result as its
   result) or `failed` (with a reason). Files left in its artifacts folder are
   registered.

## 4. Core concepts

| Term | Meaning |
|---|---|
| **Run** | One execution of a goal. It has a status, a plan, a budget and usage, a workspace, and a full history. It survives restarts. |
| **Task** | One piece of a run's work (for example "write the version to notes.txt"). A task is pending, running, completed, failed, skipped (superseded by a replan) or cancelled. |
| **Step** | One executed action. `plan` = a planner turn, `model` = a task turn. Verification runs inside a model step, after it claims done. |
| **Tool call** | Each tool the model used during a step, recorded with its arguments, a result preview, and a success/failure status. |
| **Event** | An immutable record (`run_created`, `task_failed`, `verification_passed`, `plan_created` …). The event log is **append-only**: the database rejects edits and deletes. |
| **Checkpoint** | A snapshot of the run's state, taken after every step. |
| **Artifact** | A file a run produced, with a checksum and provenance. |
| **Skill** | A versioned, reusable procedure (`SKILL.md`). The relevant ones are injected per task. |
| **Profile** | A named bundle of defaults: skills, tool permissions, budget, and planning/approval behaviour. |
| **Proposal** | A candidate change to the system (knowledge, skill, prompt, profile, code), with a level, evidence and tests. |
| **Job** | A goal on a schedule, which starts a new run every `interval_s`. |

**Run states:**

```
created → planning → running ⇄ verifying → completed
             ↑          │  ↘ awaiting_approval ─(approve)→ running
             └─replan───┤  ↘ paused ─(resume)→ running
                        └→ failed / cancelled ─(retry)→ created
```

`completed` is final. `failed` and `cancelled` can only be left via **retry**.

## 5. Runs: create, watch, control

### Creating a run

`POST /runs` accepts the following fields. Only `goal` is required.

| Field | Meaning |
|---|---|
| `goal` | What to achieve. |
| `tasks` | Skip planning and give the tasks yourself: a list of strings or objects (see below). |
| `plan` | `true` forces planning; `false` means one task equal to the goal, with no planner. Default: plan when no `tasks` are given. |
| `budget` | Limits (§8). |
| `profile` | A profile name (§9). |
| `permissions` | `{"grant": [permission levels]}` (§9). |
| `meta` | Free-form metadata. `approve_each_task: true` is honoured. |
| `session_id`, `parent_run_id` | Linking. |
| `start` | Default `true`. `false` creates the run without executing it. |

A task object looks like this:

```json
{"title": "Write the version to notes.txt",
 "instructions": "Write only the version number, nothing else.",
 "verify": [{"type": "file_contains", "path": "notes.txt", "text": "3."}],
 "requires_approval": false}
```

Example with explicit tasks and a budget:

```bash
curl -s localhost:8080/runs -H 'Content-Type: application/json' -d '{
  "goal": "Summarize the proxy module",
  "tasks": ["Read proxy/mneme_proxy.py and list its main sections",
            {"title": "Write summary.md", "verify": [{"type": "file_exists", "path": "summary.md"}]}],
  "budget": {"max_steps": 12, "max_failures": 2}}'
```

### Watching a run

| Request | Returns |
|---|---|
| `GET /runs?status=running,paused&limit=20` | List runs (`?parent=<id>` for child runs). |
| `GET /runs/<id>` | Everything: run, tasks, steps, tool_calls, artifacts, checkpoints, children, and `executing` (whether a worker is on it right now). Add `?events=1` to include the event log. |
| `GET /runs/<id>/events?after=<event_id>&types=task_failed,verification_failed` | Incremental polling. |
| `GET /runs/<id>/checkpoints` and `GET /runs/<id>/checkpoints/<cp_id>` | Checkpoint list, and one checkpoint's full saved state. |

### Controlling a run

| Action | Effect |
|---|---|
| `POST /runs/<id>/pause` | Interrupts the in-flight step within about a second. The step re-runs on resume, and doesn't count as a failure. |
| `POST /runs/<id>/resume` | Continues from persisted state. `{"checkpoint_id": "…"}` first rolls task state back to that checkpoint. |
| `POST /runs/<id>/cancel` | Stops the run. Remaining tasks become `cancelled`. |
| `POST /runs/<id>/retry` | Re-runs a failed or cancelled run. Completed tasks are **kept**; only the rest re-run. |
| `POST /runs/<id>/checkpoint` | Takes a manual checkpoint. |
| `POST /runs/<id>/approve` / `reject` | For runs `awaiting_approval` (§10). |

Invalid actions return **409**, for example resuming a completed run. An unknown
run returns **404**.

## 6. Planning and the tag protocol

The harness never asks the model for JSON. The model reasons in plain language and
marks only what the harness needs with **line tags**:

| Tag | Where | Meaning |
|---|---|---|
| `PLAN: <task>` | planner turn | one task, in order |
| `VERIFY: <shell command>` | planner turn, after a `PLAN:` | an exit-0 check for that task (run in the run's workspace) |
| `REPLAN: <why>` | task turn | "the remaining plan is wrong" — the harness replans |
| `LESSON: <rule>` / `SKILL: name :: description :: procedure` | reflection turn | self-improvement (§13) |

The existing Mneme tags still apply inside every turn: `[TOOL:SUCCESS]`,
`[TOOL:FAILURE: why]`, `[source: X]` and `[guess]`.

Example planner reply:

```
First find the official release, then record it.
PLAN: Find the latest stable Python version on python.org
PLAN: Write that version number to notes.txt
VERIFY: grep -Eq '^3\.[0-9]+' notes.txt
```

The parser is tolerant:

- Tags are case-insensitive.
- List markers (`- `, `1. `) and `**bold**` are fine.
- Plans are capped at 8 tasks.

If the planner produces **no usable plan**, the run falls back to one task equal to
the goal (a `plan_fallback` event), so a small model can't dead-end a run at step zero.

**Replanning** happens when a task exhausts its attempts, the model writes
`REPLAN:`, you type `/replan <run>`, or you reject an approval. The planner sees:

- what's **completed** (not to be repeated);
- what **failed**, and why;
- what was still pending.

Old pending tasks are marked `skipped`, not deleted, and the plan version goes up.
Replanning is bounded by `max_replans` (default 2). A replan that yields nothing
fails the run.

The prompts are editable instructions (`/instructions` page): `harness_plan`,
`harness_task_context`, `harness_judge` and `harness_reflect`. Changing them
through a proposal (§13) keeps the previous version.

## 7. Verification

When a step reports a task done and the task has `verify` checks, the harness runs
them. If any check fails, the step becomes a failure (`verification failed: …`),
which leads to a retry and then a replan. The run passes through the `verifying`
state, and the `verification_passed` / `verification_failed` events carry
per-check detail.

| Type | Fields | Passes when |
|---|---|---|
| `command` | `command`, `expect_exit` (0), `timeout` (60 s) | the command exits with `expect_exit` |
| `file_exists` | `path` | the file exists |
| `file_contains` | `path`, `text` | the file contains `text` |
| `output_contains` | `text` | the step's final answer contains `text` |
| `output_matches` | `pattern` | a regex matches the answer |
| `llm_judge` | `criteria` | the configured model answers PASS |

A bare string is shorthand for a `command` check.

- Relative paths and all commands run in the run's **`workspace/`** directory. The
  model is told this path, and which checks will run.
- `llm_judge` only runs **after every deterministic check has passed**. A judge
  never rescues work that failed an objective check.

## 8. Budgets

| Key | Default | Counts |
|---|---|---|
| `max_steps` | 100 | every step (plan + task) |
| `max_failures` | 3 | failed steps before the task fails. It resets after a replan. |
| `max_replans` | 2 | planner re-entries |
| `max_model_calls` | none | harness turns |
| `max_tool_calls` | none | tool calls recorded |
| `max_runtime` | none | seconds of execution, summed across resumes |
| `max_cost` | none | executor-reported cost units |

Budgets are enforced before every step. Exceeding one fails the run with
`budget exceeded: <key>` and a `budget_exceeded` event. The model is shown the
remaining budget each step. Unknown budget keys are rejected (400).

## 9. Permissions and profiles

### Permission levels

Every tool has a permission level:

| Level | Tools |
|---|---|
| `read-only` | search_memory, read_file, list_tools, read_tool, read_image |
| `normal` | flag_bad_memory, clear_bad_memory_flag |
| `network` | web_search, fetch_url |
| `filesystem-write` | write |
| `shell` | bash |
| `memory-write` | remove_memory |
| `system` | anything unknown: MCP and client tools |
| `self-modification` | reserved for evolution |

A run with `permissions.grant` gets **only** the tools whose level is granted.
Ungranted tools are removed from the tool list, **and** refused if the model calls
them anyway (which makes the step fail as "unexecutable"). No grant means
unrestricted: the same power as a chat turn. `"*"` grants every level.

```bash
curl -s localhost:8080/runs -H 'Content-Type: application/json' \
  -d '{"goal": "Review README.md for errors", "permissions": {"grant": ["read-only"]}}'
```

### Profiles

A profile is a reusable bundle of those settings. The built-in profiles:

| Profile | Effect |
|---|---|
| `default` | unrestricted |
| `researcher` | read-only, normal, network and filesystem-write (no shell); `max_steps` 40 |
| `coder` | adds shell; `max_steps` 80, `max_failures` 4 |
| `reviewer` | read-only + normal, no planning |
| `cautious` | every task needs your approval |

```bash
curl -s localhost:8080/runs -H 'Content-Type: application/json' -d '{"goal": "…", "profile": "researcher"}'
```

Create your own. The spec keys are `description`, `skills`, `grant`, `budget`,
`plan`, `approve_each_task` and `model`:

```bash
curl -s localhost:8080/profiles -H 'Content-Type: application/json' -d '{
  "name": "docs-writer",
  "spec": {"description": "Writes docs; no shell",
           "skills": ["swarm-creation"],
           "grant": ["read-only", "filesystem-write"],
           "budget": {"max_steps": 30}}}'
```

- Explicit run arguments always win over the profile.
- Profiles are versioned: `GET /profiles/<name>` shows the history. Changing a
  profile through evolution (§13) needs your approval.
- `model` is informational: a proxy serves one model, so choose the proxy port per
  profile.

## 10. Approvals

A run pauses for you before a task runs when that task has
`"requires_approval": true`, or when the run uses the `cautious` profile (or
`meta.approve_each_task`).

```bash
curl -s localhost:8080/runs -H 'Content-Type: application/json' -d '{
  "goal": "Clean the build directory",
  "tasks": ["List what is in build/", {"title": "Delete build/", "requires_approval": true}]}'
# … status becomes awaiting_approval
curl -s -X POST localhost:8080/runs/<id>/approve -d '{"note": "ok"}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8080/runs/<id>/reject  -d '{"reason": "too risky"}' -H 'Content-Type: application/json'
```

- **Approve:** the task runs.
- **Reject:** the task fails with "rejected by …", and the harness replans the
  remaining work with your reason in the prompt. Without a planner, the run fails.

From chat, use `/approve <run> [note]` and `/reject <run> [reason]`. A waiting run
holds no worker, is not treated as a crashed orphan, and can wait indefinitely.

## 11. Skills

A skill is a reusable procedure, stored as `skills/<name>/SKILL.md`:

```markdown
---
name: web-research
description: Research a factual question on the web and cite the pages read
tools: [web_search, fetch_url]
requires: []            # other skills this one builds on (composition)
tags: [search, cite, current]
failure_modes: ["answering from search snippets"]
---
1. web_search for candidate pages.
2. fetch_url the most authoritative one BEFORE stating any fact.
3. Cite the URL you read: [source: <url>].
```

**Where skills come from:**

- the repository's `skills/`, which ships `swarm-creation`;
- `<db dir>/skills/`, for your own skills;
- `POST /skills`;
- self-improvement proposals.

Every change creates a **new version**, and old versions are kept. Loading an
unchanged file is a no-op.

**How skills are used:** for each task, the harness selects the one or two most
relevant skills (following `requires`) and injects their procedure and failure
modes into that step's context. A profile can pin skills. Every run that used a
skill updates its `uses`, `successes` and `failures` counts.

```bash
curl -s localhost:8080/skills                         # list (?q=… to rank by relevance, ?all=1 incl. inactive)
curl -s localhost:8080/skills/web-research            # detail + version history
curl -s -X POST localhost:8080/skills/web-research/restore -d '{"version": 1}' -H 'Content-Type: application/json'
```

## 12. Strategies and their history

Mneme's existing strategy library (learned rules, injected when relevant) now
**keeps every version**. Each save records the replaced row and the new row in
`strategy_versions`, with who made the change and why. Strategies also carry
`created_by`, `derived_from` and `validated_by`.

```bash
curl -s localhost:8080/strategies/<strategy_id>/history
```

In chat, `/strategies` lists recent strategies.

## 13. Self-improvement (evolution)

Every change to the system is a **proposal**. Levels decide how much ceremony it gets:

| Level | Kinds | Applied |
|---|---|---|
| **L1** | `knowledge` | automatically; also stored into memory so it is retrievable |
| **L2** | `skill`, `strategy` | automatically (up to `auto_apply_level`), versioned |
| **L3** | `instruction`, `profile`, `config` | only after its tests pass **and** you approve |
| **L4** | `code` | patch applied on a **new git branch** `evolve/<id>` in a separate worktree, tested there. **Never merged automatically**: you merge it. |

**Every apply is transactional:**

1. The previous content is captured.
2. The change is applied.
3. The tests run again.
4. If they fail, the change is rolled back automatically and the attempt is kept
   (`rolled_back`).

Any applied proposal can also be rolled back later.

**Where proposals come from:**

- **Automatically:** every failed run, or run that recovered from failures, becomes
  an L1 knowledge note listing what failed and how (failure categories).
- **Reflection** (`harness.reflect: true`): after such a run, one extra model turn
  writes `LESSON:` lines (which become L1 notes) and `SKILL:` lines (which become
  L2 skill proposals).
- **You**, or any client:

```bash
# propose a prompt change (L3) with a test, then approve it
curl -s localhost:8080/evolution -H 'Content-Type: application/json' -d '{
  "kind": "instruction", "target": "harness_judge",
  "content": "…new prompt text…", "reason": "judge was too lenient",
  "evidence": ["run_…"], "tests": [{"type": "output_contains", "text": "PASS or FAIL"}]}'
curl -s -X POST localhost:8080/evolution/<proposal_id>/approve
curl -s -X POST localhost:8080/evolution/<proposal_id>/rollback

# propose a code change (L4): content is a unified diff; tests are REQUIRED
curl -s localhost:8080/evolution -H 'Content-Type: application/json' -d '{
  "kind": "code", "target": "proxy/mneme/tools.py", "content": "--- a/…\n+++ b/…\n@@ …",
  "tests": ["python3 tests/test_native_tools.py"]}'
curl -s -X POST localhost:8080/evolution/<id>/approve    # -> branch_ready: evolve/<id>
git merge evolve/<id>                                     # YOU activate it (or /evolution/<id>/reject to discard)
```

| Request | Returns / does |
|---|---|
| `GET /evolution?status=proposed` | the review queue |
| `GET /evolution?kind=skill&target=web-research` | *why is this object the way it is?* |
| `GET /evolution/<id>` | the proposal, plus its append-only log |
| `POST /evolution/<id>/test` | runs its tests without applying |

The dashboard's **System evolution** tab shows each proposal's content, previous
version, evidence and decision.

## 14. Background jobs

A job starts a run of a goal on a schedule:

```bash
curl -s localhost:8080/jobs -H 'Content-Type: application/json' -d '{
  "name": "daily-digest", "goal": "Summarize new items in ~/inbox into digest.md",
  "interval_s": 86400, "profile": "researcher", "start_in_s": 60}'
curl -s localhost:8080/jobs                          # list
curl -s localhost:8080/jobs/<job_id>                 # detail + log (run_started / skipped / …)
curl -s -X POST localhost:8080/jobs/<job_id>/trigger # run on the next tick
curl -s -X POST localhost:8080/jobs/<job_id>/disable # (enable to turn back on)
```

- The minimum interval is 10 s. Tasks, profile and budget are optional.
- A job **doesn't overlap itself** by default. If its previous run is still active,
  the tick is skipped and logged. Pass `"overlap": true` to allow overlap.
- Several proxies sharing one database never start the same tick twice.
- Runs created by a job carry `meta.job_id` and `created_by: job:<id>`.

## 15. Chat commands and the Runs dashboard

Type these in the chat page (or any client of the proxy), in a gateway, or through
`POST /harness/command {"text": "/status last"}`. **They are handled by the
harness, and the model never sees them.** Unknown `/words` go to the model as
normal messages.

| Command | Does |
|---|---|
| `/help` | list commands |
| `/run <goal>` | start a planned run |
| `/runs [status]` | recent runs |
| `/status [run]` | status, progress and result (the default run is `last`) |
| `/plan <run>` | the current plan |
| `/tasks <run>` | tasks and their states |
| `/log <run> [n]` | the last n events |
| `/files <run>` | artifacts |
| `/pause`, `/resume`, `/cancel`, `/retry <run>` | control a run |
| `/replan <run> [reason]` | replan the remaining work |
| `/approve <run> [note]`, `/reject <run> [reason]` | approvals |
| `/skills [query]`, `/tools`, `/profiles`, `/strategies` | catalogues |
| `/memory <q>`, `/search <q>` | memory search |
| `/evolution [status]`, `/approve-change <id>`, `/reject-change <id>` | proposals |
| `/jobs`, `/metrics`, `/config`, `/models` | status |

Run references can be a full id, a unique suffix or prefix (for example the last 6
characters), or `last`.

**Dashboard:** `http://localhost:<port>/runs/ui` has these tabs:

- **Runs:** create a run; list runs; open one for its plan, tasks, result,
  artifacts, tool calls and live events, with pause, resume, cancel, retry,
  approve and reject buttons. The page refreshes every 5 s.
- **Skills:** each skill, its version history, and one-click restore.
- **System evolution:** each proposal's content and previous version, level,
  evidence, and test / approve / reject / rollback controls.
- **Profiles**, **Metrics**, and a **Command** box.

## 16. Gateways: CLI and Telegram

Gateways live in `extensions/gateways/`. They talk to the proxy **over HTTP only**:

- a line starting with `/` is a harness command;
- other text is a chat turn (the default), or a new run with `--plain run`.

Runs you start from a gateway are watched, and you're notified when one
**completes**, **fails**, or **needs approval**.

```bash
# terminal
python3 extensions/gateways/cli.py --url http://localhost:8080
python3 extensions/gateways/cli.py --once "/runs"

# Telegram (create a bot with @BotFather; find your numeric user id with @userinfobot)
export MNEME_TELEGRAM_TOKEN=123456:ABC...
export MNEME_TELEGRAM_ALLOWED=11111111          # REQUIRED allow-list
python3 extensions/gateways/telegram.py --url http://localhost:8080 --plain run
```

The Telegram gateway refuses to start without an allow-list, answers only private
chats, and ignores users who are not on the list. Everything you do there is
recorded with the actor `telegram:<your id>`.

To write a new gateway, subclass `base.Gateway`, implement `receive()` and
`send()`, and call `serve()`. See [`extensions/gateways/README.md`](../../extensions/gateways/README.md).

## 17. Swarms as durable runs

Add a `harness:` block to a swarm config to record the whole swarm as a run:

```yaml
harness:
  port: 8080            # a proxy with the harness enabled
  goal: "tides story"   # optional
  required: false       # true = abort if the proxy is unreachable (default: warn, continue)
steps:
  - ...
```

The swarm then records:

- each step, as a `swarm_step_completed` event that includes **which step runs next**;
- every output file, as an artifact;
- each `parallel:` sub-step, as a **child run**;
- the overall status: completed at `END`, failed on an error, paused on Ctrl-C.

Resume after a crash, a failure, or Ctrl-C:

```bash
python3 extensions/swarm/swarm_orchestrator.py swarm_config.yaml --resume-run <run_id>
```

The flow restarts at the recorded next step, and earlier model calls are **not**
repeated. A swarm's run is *external*: the harness records it but never executes
or resumes it itself. For every swarm option, see
[`skills/swarm-creation/SKILL.md`](../../skills/swarm-creation/SKILL.md) and
`extensions/swarm/SWARM_REFERENCE.md` §20.

## 18. Workspaces, artifacts, crash recovery

Every run gets a workspace directory:

```
<runs_dir>/<run_id>/
  input/        files you hand the run
  workspace/    where it works; relative VERIFY paths and commands run here
  artifacts/    deliverables; every file here is registered when the run ends
  logs/
  checkpoints/  human-readable JSON copies of every checkpoint (the DB is authoritative)
```

The model is told the `workspace/` and `artifacts/` paths. You can also register
files yourself with `POST /runs/<id>/artifacts {"path", "description"}`. Each
artifact records a sha256 checksum and its provenance.

**Crash recovery:**

- If the proxy dies mid-run, the next proxy start finds the orphaned run, marks the
  interrupted step `interrupted`, and sets the run to **paused** (a
  `run_interrupted` event).
- `POST /runs/<id>/resume` (or `/resume <run>`) continues it. **Completed tasks never
  re-run**, and the interrupted task runs again.
- A step can therefore execute twice, so shell or file side effects may repeat.
  That's why `auto_resume` is off by default. Turn it on only if your tasks are
  safe to re-run.

## 19. Multiple proxies, one harness

The ledger (`harness.db`) sits next to the **shared** memory DB, so every proxy
that shares a DB directory sees every run, skill, proposal and job.

- A run is executed by **one** process at a time. Ownership is a lease with a
  heartbeat; another proxy takes a run over only if its owner is provably dead, or
  its heartbeat has gone stale for longer than `lease_seconds`.
- Jobs are claimed atomically, so a tick never fires twice.
- Use different proxies (different models) for different work, for example a small
  local model for a `reviewer` profile and a frontier model for a `coder` profile.
  Point each run at the proxy you want by POSTing to that proxy's port.

## 20. Metrics

`GET /harness/metrics` (or `/metrics` in chat, or the dashboard tab) reports:

- run counts by status, and the task success rate;
- average steps, model calls, tool calls, replans and runtime per completed run;
- failure categories: verification, empty, fabricated, provider, tool,
  unexecutable, budget, rejected, interrupted and more;
- verification passes and failures, and the number of runs that recovered from
  failures;
- per-tool success rates;
- per-skill uses and success rates;
- self-improvement proposals, by status.

These numbers are the feedback signal for improving strategies, skills, prompts
and profiles.

## 21. Configuration reference

| Key (`mneme.yaml`) | Env var | Default |
|---|---|---|
| `harness.enabled` | `MNEME_HARNESS` | `true` |
| `harness.db_path` | `MNEME_HARNESS_DB` | `<db dir>/harness.db` |
| `harness.runs_dir` | `MNEME_RUNS_DIR` | `<db dir>/runs` |
| `harness.auto_resume` | `MNEME_HARNESS_AUTO_RESUME` | `false` |
| `harness.lease_seconds` | `MNEME_HARNESS_LEASE` | `120` |
| `harness.reflect` | `MNEME_HARNESS_REFLECT` | `false` |
| `harness.auto_apply_level` | `MNEME_HARNESS_AUTO_APPLY_LEVEL` | `2` |
| `harness.scheduler` | `MNEME_HARNESS_SCHEDULER` | `true` |
| `harness.scheduler_tick` | `MNEME_HARNESS_SCHEDULER_TICK` | `15` |

Environment variables beat `mneme.yaml`, which beats the defaults. Unknown keys
abort startup (a typo guard).

## 22. HTTP API reference

All endpoints are on the proxy's port and return JSON. Errors are:

- **400** — bad input;
- **404** — unknown id;
- **409** — invalid state transition;
- **503** — the harness is disabled.

```
Runs        POST /runs
            GET  /runs[?status=a,b&limit=&offset=&parent=]
            GET  /runs/<id>[?events=1]
            GET  /runs/<id>/events[?after=&limit=&types=a,b]
            GET  /runs/<id>/checkpoints            GET /runs/<id>/checkpoints/<cp_id>
            POST /runs/<id>/pause | resume {checkpoint_id?} | cancel | retry | checkpoint
            POST /runs/<id>/approve {note?} | reject {reason?}
            POST /runs/<id>/artifacts {path, kind?, description?}
External    POST /runs/<id>/events {type, data}    POST /runs/<id>/status {status, result?, error?}
            (only for runs created with meta.external — how extensions like the swarm record work)
Skills      GET /skills[?q=&k=&all=1]   POST /skills {name, description, body, tools?, requires?, tags?,
            failure_modes?, verify?}   GET /skills/<name>   POST /skills/<name>/restore {version}
Strategies  GET /strategies/<id>/history
Profiles    GET /profiles   POST /profiles {name, spec}   GET /profiles/<name>
Evolution   GET /evolution[?status=&kind=&target=]   POST /evolution {kind, target, content, reason?,
            evidence?, tests?, level?}   GET /evolution/<id>
            POST /evolution/<id>/test | approve | reject {reason?} | rollback
Jobs        GET /jobs   POST /jobs {name, goal, interval_s, tasks?, profile?, budget?, start_in_s?, overlap?}
            GET /jobs/<id>   POST /jobs/<id>/enable | disable | trigger
Control     POST /harness/command {text}   GET /harness/metrics   GET /runs/ui
```

## 23. Security

- **Runs have real power.** Unless a grant or profile restricts it, a run can use
  `bash` and `write` with the proxy's OS privileges. So can planner-written
  `VERIFY:` commands. Use `researcher`, `reviewer` or a custom grant for untrusted
  goals, and `cautious` (or `requires_approval`) for risky tasks.
- **There is no authentication** on the proxy or its harness endpoints. The proxy
  binds to `127.0.0.1` by default; keep it that way. For remote access, front it
  with the reverse proxy (`scripts/start_gateway.sh`, with `MNEME_GATEWAY_TOKEN`
  set) or a VPN or SSH tunnel.
- **Self-modification is gated.**
  - Prompt, profile and config changes need your approval.
  - Code changes only land on a git branch, and require tests.
  - `harness.auto_apply_level` never goes above 2.
  - Every change is logged in an append-only log, with its previous version.
- **Telegram** refuses to start without an allow-list, and answers only private
  chats.

## 24. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/runs` returns 503 | The harness is disabled, or failed at startup. Look for `[HARNESS][ERR]` in `proxy.log` or `errors.log`. |
| A run sits in `paused` after a restart | Expected: runs interrupted by a crash come back paused. Resume them, or set `auto_resume: true`. |
| The plan source is `fallback` | The model didn't write `PLAN:` lines. Check the plan step's output in `GET /runs/<id>`. Small models may need the `harness_plan` prompt tuned (through a proposal). |
| A task fails with `verification failed` | The work didn't pass the checks. The step's `meta.verification` has the per-check detail. Files must be in the run's `workspace/`, or use absolute paths. |
| A step fails with `…cannot execute: <tool>` | The model called a tool the run's grant doesn't allow (or a client-only tool). Widen the grant or change the profile. |
| `budget exceeded: max_steps` | Raise the budget, or give explicit tasks. `max_steps` counts plan steps too. |
| A command like `/foo` went to the model | Only known command words are intercepted. See `/help`. |
| Two proxies — "owned by another live process" | That run is executing elsewhere. Wait, or let its lease expire if that process is really gone. |
| The swarm prints `harness unreachable` | The swarm config's `harness.port` isn't a running proxy. The swarm continues unrecorded unless `required: true`. |
| An L4 proposal is `test_failed: patch does not apply` | The diff doesn't apply to the current `HEAD`. Regenerate it against the current code. |

---

*Design and rationale: [`SPEC.md`](SPEC.md), [`adr/`](adr/),
[`00-architecture-audit.md`](00-architecture-audit.md).*

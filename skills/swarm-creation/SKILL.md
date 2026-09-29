---
name: swarm-creation
description: Design, write, validate and run a Mneme swarm — a config-driven multi-step / multi-model loop (swarm_config.yaml) executed by extensions/swarm/swarm_orchestrator.py (serial) or swarm_p_orchestrator.py (parallel). Use when asked to build an agent pipeline, critic/review loop, fan-out, recurring inbox processor, or any multi-proxy workflow on Mneme.
---

# Skill: creating a Mneme swarm

A swarm is a YAML file of **steps** that the orchestrator runs in order, with jumps.
The steps talk to each other **only through files on disk**. Each model step reads
directories, calls one backend, and writes a file. Control flow (`goto` / `if` /
`END`) lives in the config, not in code.

Ground rules. Never break these:

- The swarm is an **extension**. It talks to Mneme proxies **over HTTP only**:
  `POST http://localhost:<port>/v1/chat/completions`. Never import proxy code or
  touch its database.
- All paths are **relative to the directory you run the orchestrator from**, not
  to the config file's location.
- Ship **placeholder ports** and tell the user to point them at their real
  proxies. On RunPod, never use 8081 (nginx reserves it).
- Dependencies: `requests` and `pyyaml`.

Source of truth: `extensions/swarm/SWARM_REFERENCE.md` and `swarm_orchestrator.py`.
If this skill and the code ever disagree, the code wins.

---

## 1. Procedure

1. **Clarify the loop.**
   - Which roles are there (critic, writer, reviewer, …)?
   - Which model or proxy port does each role use?
   - What is the input (an inbox folder)?
   - What is the final output?
   - What stops the loop (a verdict string, a file count, `max_steps`)?
2. **Draw the board.** Name one directory per hand-off, for example
   `input → input.active → pass1/ → pass2/ → output/ → published/`.
   One writer per file.
3. **Write the steps.** Use the templates in §6. Give every step a `name`.
4. **Validate before running.** Use the checklist in §5. The orchestrator also fails
   loudly at load, but it does **not** validate `parallel:` sub-steps (see §4).
5. **Run it:**
   ```bash
   mkdir -p "$WORK/input" && cd "$WORK"   # put the brief/input files in input/
   python3 /path/to/mneme/extensions/swarm/swarm_orchestrator.py /path/to/swarm_config.yaml
   # or swarm_p_orchestrator.py when the config uses parallel:
   ```
   The proxies must already be running (use the setup wizard). Check them with
   `curl localhost:<port>/health`.
6. **Iterate live.** The config hot-reloads at each step boundary (see §4). Set
   `max_steps` while you are developing so a loop can't run forever.

---

## 2. Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `ollama_url` | `http://localhost:11434` | Base URL for `backend: ollama` steps |
| `timeout` | `600` | Default per-call request timeout, in seconds |
| `max_steps` | `0` (no limit) | Cap on **total step executions**. Counts every step, including action-only steps and loop passes. Stops a runaway loop. |
| `steps` | required | Ordered list of steps |
| `harness` | off | `{port, goal?, required?}`: record the swarm as a durable harness run (events, artifacts, child runs per `parallel` sub-step). Resume with `--resume-run <run_id>`. See SWARM_REFERENCE §20. |

## 3. Step fields (every option)

| Field | Type | Meaning / rules |
|---|---|---|
| `name` | str | Label: a jump target and a log tag. Must be unique. `END` is reserved. |
| `backend` | `mneme` (default) \| `ollama` | Which backend a model step calls. |
| `port` | int | **Required** for `mneme` when the step calls a model. |
| `model` | str | **Required** for `ollama` when the step calls a model. |
| `options` | map | Per-call generation override. For `mneme`: `temperature`, `top_p`, `top_k`, `max_tokens`. For `ollama`: `temperature`, `top_p`, `top_k`, `num_predict`, plus passthrough keys such as `repeat_penalty`, `num_ctx`, `presence_penalty`. The orchestrator nests these under `"options"` for you; the proxy ignores bare top-level fields. |
| `system_prompt` | str | For `ollama`: this **is** the system prompt. For `mneme`: an **extra** instruction added on top of the proxy's own prompt. Use `\|` block scalars for multi-line text. |
| `retry` | number | Extra attempts on **transient** failures only (connection error, timeout, HTTP 5xx), with backoff of `2**attempt` seconds. HTTP 4xx or a malformed response stops the run. |
| `timeout` | number | Per-step timeout; overrides the top-level `timeout`. |
| `delay` | number | Sleep this many seconds **before** the step runs (for pacing or rate limits). |
| `every` | int | Run only on every Nth visit. `0` (default) = every visit. A skipped visit does no work at all, but **still follows the step's `goto`/`if`** (see §4). |
| `read_dir` | str \| list | Directory or directories read into the `user` message. Headers are `--- relpath ---`, or `--- <dirbase>/relpath ---` when several directories are listed. Files are read in sorted order; dot-files are skipped; missing dirs are skipped. If nothing is read, the input is the literal `NO_INPUT`. A **file path yields `NO_INPUT`**: it reads directories only. |
| `skip_if_empty` | bool | When `read_dir` gives `NO_INPUT`, skip the model call and its write/append. Folder actions and flow still run. |
| `write_dir` | str | Overwrites the output. A path **with an extension** is written as that exact file; a path **without one** is a directory, and `output.txt` is written inside it. |
| `append_dir` | str | Like `write_dir`, but appends. **No newline is added** between appends. Can be combined with `write_dir` (the same output goes to both). |
| `edit_dir` | str (one file) | Edits a file in place. The model returns either the **complete corrected file** or `<<<<<<< SEARCH` / `=======` / `>>>>>>>` patch blocks. For a patch, each SEARCH text must match exactly once. For a full rewrite, a diff gate compares it to the original. A failed edit **aborts the run and leaves the file untouched**. Cannot be combined with `write_dir`/`append_dir`. Pair it with a `read_dir` that contains the file. |
| `min_similarity` | 0–1, default 0.5 | Diff-gate threshold for `edit_dir`. Raise it (e.g. 0.8) for typo fixes. |
| `copy_dir` + `copy_to` | str + str | Snapshot. A source directory has its **contents** merged into `copy_to`; a source file is copied in under its own name. Both keys are required together. |
| `move_dir` + `move_to` | str + str | Promote. Renames the source **into** `move_to` (atomic on one filesystem). Both keys are required together. |
| `swap_dir` | str | Freeze an inbox. Removes the stale `<dir>.active`, renames `<dir>` → `<dir>.active`, and recreates an empty `<dir>`. Put it on an action-only step. |
| `clear_dir` | str \| list | Deletes everything **under** the directory (the directory itself stays). Missing dirs are skipped. **Cannot be combined with `goto`/`if`.** |
| `goto` | label \| `END` | Unconditional jump. Cannot be combined with `clear_dir`/`if`. |
| `if` | map | Branch (see below). Needs `then` and/or `else`. If the branch taken has no label, execution falls through to the next step. |
| `exec` | str \| list | Runs a command from the working directory. A string goes through the shell; a list runs argv with no shell. stdout is logged. A **non-zero exit aborts the run.** Makes the step action-only. |
| `parallel` | list of leaf steps | **Parallel driver only.** Runs the sub-steps at the same time. |

**`if` form A: branch on the model's output.** This makes the step call a model.
```yaml
if: { condition: contains|equals|startswith|endswith|matches, value: "...", then: label|END, else: label|END }
```
- `contains` is a case-sensitive substring match.
- `equals`, `startswith` and `endswith` compare against the stripped output.
- `matches` is `re.search(value, output, DOTALL)`. Use `(?i)` for case-insensitive matching.

**`if` form B: branch on the filesystem.** This makes no model call.
```yaml
if: { condition: count_ge|count_lt|empty|exists, dir: path, value: N, then: ..., else: ... }
```
- `count_*` and `empty` count non-hidden files. A missing directory counts as 0.
- `value` is required for `count_ge` and `count_lt`.

**When is a model called?** Only when the step has `write_dir`, `append_dir`,
`edit_dir`, or a **string** `if`. Every other step is action-only and costs nothing.

**Fixed order inside a step:** `delay` → `exec` → `read_dir` → model call →
`write_dir` → `append_dir` → `edit_dir` → `copy` → `move` → `swap` → `clear` →
`goto`/`if`/fall-through.

Consequence: `write_dir` followed by `copy_dir` in the **same** step copies the
freshly written file.

---

## 4. Behaviours that are easy to get wrong

- **`parallel:` sub-steps are not checked by startup validation.** `_validate_flow`
  only checks top-level steps. Check each sub-step yourself: mneme sub-steps have a
  `port`, ollama sub-steps have a `model`, and each sub-step writes a **distinct**
  output file. Sub-steps are leaves: a `goto` inside a block is not followed, and a
  string `if` still calls the model but is ignored. After a block, execution always
  moves to the next step.
- **A throttled `every` step still follows its flow.** On a skipped visit, a `goto`
  still jumps. A string `if` sees `None` as the output, so it evaluates false and
  takes the `else` branch (or falls through). A folder `if` is evaluated normally.
- **Duplicate YAML keys are silently collapsed**: the last one wins. For two
  destinations, use `write_dir` + `append_dir`, or `write_dir` + `copy_dir`.
- **Tabs break YAML.** Indent with spaces only.
- **`swap_dir` deletes the previous `.active`.** A reader must re-read after the swap.
- **Hot reload:** the config is re-read whenever its modification time changes. The
  flow re-anchors on the current step's **name**; if that name was removed, the flow
  restarts from the top. A broken edit keeps the last good flow and logs a warning.
  `MNEME_HOT_RELOAD=0` locks the config.
- **Hosted fan-out:** a large `parallel:` block can hit HTTP 429. 4xx is permanent
  (the run stops), so size the block to your API key's rate limit. A local
  single-GPU Ollama only speeds up **same-model** fan-out.
- **`read_dir` content is also the Mneme memory-retrieval query.** Keep the inputs
  focused.

## 5. Validation checklist (run through it before launching)

1. No step is named `END`, and all step names are unique.
2. Every `goto`, `if.then` and `if.else` target exists, or is `END`.
3. No step combines `clear_dir` with `goto` or `if`. No step combines `edit_dir`
   with `write_dir` or `append_dir`.
4. `copy_dir` has a `copy_to`; `move_dir` has a `move_to`.
5. Every model-calling step (top-level **and** parallel sub-steps) has a `port`
   (mneme) or a `model` (ollama). `backend` is `mneme` or `ollama`.
6. `read_dir` values are directories, not files.
7. `if` rules: a folder condition has a `dir`; `count_*` has an integer `value`; a
   string condition other than `matches` has a `value`; there is at least one of
   `then`/`else`.
8. `retry`, `delay` and `min_similarity` are numbers (`min_similarity` between 0
   and 1).
9. The loop has a real exit (`END` through `if`) **and** `max_steps` is set while
   developing.
10. Ports are placeholders the user must set (no 8081 on RunPod), and paths are
    relative.

To check without calling any model, load the config and let validation run:
`python3 -c "import sys; sys.path.insert(0,'extensions/swarm'); from swarm_orchestrator import Orchestrator; Orchestrator('swarm_config.yaml'); print('valid')"`.
Constructing the orchestrator only loads and validates the config. Then check the
parallel sub-steps by hand (see §4).

## 6. Templates

**Inbox freeze → process → consume (a recurring tick):**
```yaml
max_steps: 200
steps:
  - { name: freeze, swap_dir: inbox }
  - name: work
    backend: mneme
    port: 8080                       # placeholder — set to your proxy
    read_dir: inbox.active
    skip_if_empty: true
    append_dir: log/results.txt
  - { name: consume, clear_dir: inbox.active }
  - { name: loop, delay: 30, goto: freeze }
```

**Draft → review → revise, until approved:**
```yaml
steps:
  - { name: draft, port: 8080, read_dir: input, write_dir: board/draft.txt }
  - name: review
    port: 8084
    read_dir: board
    write_dir: review/verdict.txt
    system_prompt: "Critique the draft. End with exactly one line: VERDICT: APPROVE or VERDICT: REVISE"
    if: { condition: matches, value: '(?i)VERDICT\s*:\s*APPROVE', then: publish, else: revise }
  - { name: revise, port: 8083, read_dir: [board, review], write_dir: board/draft.txt, goto: review }
  - { name: publish, move_dir: board/draft.txt, move_to: published, goto: END }
```

**Parallel critics + gate (use `swarm_p_orchestrator.py`):**
```yaml
steps:
  - { name: freeze, swap_dir: input }
  - { name: snapshot, copy_dir: input.active, copy_to: buffer }
  - parallel:
      - { name: c_structure, port: 8080, read_dir: input.active, write_dir: pass1/structure.txt, retry: 2 }
      - { name: c_prose, backend: ollama, model: qwen2.5:14b, options: { temperature: 0.7, num_predict: 400 },
          system_prompt: "You are a prose critic.", read_dir: input.active, write_dir: pass1/prose.txt }
  - { name: consume, clear_dir: input.active }
  - name: gate
    if: { condition: count_ge, dir: pass1, value: 2, then: synthesize, else: END }
  - { name: synthesize, port: 8080, read_dir: [buffer, pass1], write_dir: output/synthesis.txt }
  - { name: cleanup, clear_dir: [buffer, pass1] }
```

**Timestamp every tick** (models' sense of "now" drifts):
`- { name: stamp, exec: "python3 scripts/now.py board" }` writes
`board/timestamp.txt`. Add `-u` for UTC. A later step must `read_dir: board` to see it.

**Throttled summary every 3rd cycle:**
`- { name: summarize, every: 3, read_dir: log, write_dir: summary.txt }`

## 7. Relation to the agent harness

Add `harness: {port: <proxy port>}` to make the swarm durable:

- The whole flow is recorded as an external harness run.
- Each step becomes a `swarm_step_completed` event that says which step comes next.
- Every output file is registered as an artifact.
- Each `parallel:` sub-step becomes a child run.

After a crash, a failure, or Ctrl-C, run the same command with
`--resume-run <run_id>` and the flow continues at the recorded next step, without
redoing earlier model calls.

Recommend this for any long-running or recurring swarm. If the proxy can't be
reached, the swarm warns and continues; set `required: true` to make it abort
instead. Watch progress in the proxy's `/runs/ui`.

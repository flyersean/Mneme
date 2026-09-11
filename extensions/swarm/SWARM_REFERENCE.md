# Swarm Orchestrator — Complete Config Reference

This is the full, authoritative reference for the Mneme swarm orchestrator's
config format. It covers every field, every primitive, every flow, both drivers
(serial and parallel), and both backends. Written for a coding agent that needs
to author or validate `swarm_config.yaml` files or build its own consumer.

Two drivers ship in `extensions/swarm/`:

- `swarm_orchestrator.py` — the serial driver (one step at a time).
- `swarm_p_orchestrator.py` — the parallel driver (adds one `parallel:` block).
  Everything below applies to both unless a section is marked parallel-only.

---

## 1. Architecture & integration contract

The orchestrator is a **config-driven loop**. It consumes the Mneme proxy stack
over HTTP only — it has **no import/path/code dependency** on the Mneme repo. The
whole contract is:

- Mneme proxy: `POST http://localhost:<port>/v1/chat/completions`
  (OpenAI-compatible). Body `{"model": "default", "messages": [...], "options": {...}}`.
  Response `{"choices": [{"message": {"content": "..."}}]}`.
- Raw Ollama: `POST <ollama_url>/api/chat` (native). Body
  `{"model": "...", "stream": false, "messages": [...], "options": {...}}`.
  Response `{"message": {"content": "..."}}`.

A step that calls a Mneme proxy sends a `system` message (only if `system_prompt`
is set) plus a `user` message containing the `read_dir` context. The proxy does
memory retrieval/injection, the tool loop, and grading on its own; the
orchestrator only sends messages and reads the reply.

**Important Mneme detail:** the proxy injects its *own* system prompt on every
turn. A per-step `system_prompt` on a `backend: mneme` step is therefore an
*extra* instruction prepended on top — not a replacement.

---

## 2. Running it

```
python3 swarm_orchestrator.py [config.yaml]       # serial
python3 swarm_p_orchestrator.py [config.yaml]     # parallel
```

Defaults to `swarm_config.yaml` when no path is given. **All folder paths in the
config are relative to the directory you run the orchestrator from.** So the
inbox, boards, buffer, log, and final output all live in that directory. Run from
a directory on persistent storage if you want to keep `output/` and `published/`.

Proxies must already be running before you launch the orchestrator (see the Mneme
setup wizard). On RunPod, avoid port `8081` — nginx reserves it.

---

## 3. Top-level config keys

| Key         | Type   | Default                   | Meaning |
|-------------|--------|---------------------------|---------|
| `ollama_url`| string | `http://localhost:11434`  | Base URL for `backend: ollama` steps. |
| `timeout`   | number | `600`                     | Default per-call request timeout (seconds). |
| `max_steps` | number | `0` (no limit)            | Safety cap on the TOTAL number of step executions before the orchestrator stops. Catches an infinite `goto`/`if` loop that never reaches `END`. Counts every execution, including action-only steps and loop iterations. |
| `steps`     | list   | (required)                | The ordered list of steps. |

---

## 4. Step execution model

Each step executes in a **fixed order**:

1. `delay` — sleep this many seconds (if set).
2. `exec` — run the command (if set); a non-zero exit **aborts the whole run**.
3. `read_dir` — read context into one text blob.
4. model call — only if the step *needs* a model (see below).
5. `write_dir` — write output (overwrite), if set and output present.
6. `append_dir` — append output, if set and output present.
7. `copy_dir` → `copy_to` — copy, if set.
8. `move_dir` → `move_to` — move, if set.
9. `swap_dir` — freeze, if set.
10. `clear_dir` — wipe, if set.
11. advance index via `goto` / `if` / fall-through.

### When a model is called

A step calls a model **only when**:

- it has `write_dir`, OR
- it has `append_dir`, OR
- it has an `if` with a **string** condition (`contains`/`equals`/`startswith`/
  `endswith`/`matches`) — because that branches on the step's own output.

A step with **only** folder actions (`swap_dir`/`copy_dir`/`move_dir`/`clear_dir`),
`goto`, a **folder-state** `if` (`count_ge`/`count_lt`/`empty`/`exists`), `exec`,
or `delay` **never calls a model**. Use those to sequence the loop without
burning a generation.

Consequence: a string-`if` step calls the model to *produce* the output it
branches on, even without `write_dir`/`append_dir` — the output is used only for
the branch and is not written anywhere.

---

## 5. Step field reference

| Field          | Type            | Applies to            | Meaning |
|----------------|-----------------|-----------------------|---------|
| `name`         | string          | all                   | Optional label; used as a jump target and in logs. Must be unique. `END` is reserved. |
| `backend`      | string          | model steps           | `"mneme"` (default) or `"ollama"`. |
| `port`         | number          | `backend: mneme`      | Required. The Mneme proxy port. |
| `model`        | string          | `backend: ollama`     | Required. The Ollama model name. |
| `options`      | map             | model steps           | Generation overrides for THIS call only (see §6). |
| `system_prompt`| string          | model steps           | Optional system message (see §7). |
| `retry`        | number          | model steps           | Extra re-issues on transient failure (see §8). |
| `timeout`      | number          | model steps           | Per-step request timeout override. |
| `delay`        | number          | all                   | Pause this many seconds BEFORE the step runs. |
| `read_dir`     | string or list  | model steps           | Directory(ies) to read context from (see §9). |
| `write_dir`    | string          | model steps           | Write output (OVERWRITE) (see §10). |
| `append_dir`   | string          | model steps           | Append output (see §10). |
| `copy_dir`     | string          | action-only           | Source to copy (see §11). Pair with `copy_to`. |
| `copy_to`      | string          | action-only           | Destination folder for `copy_dir`. |
| `move_dir`     | string          | action-only           | Source to move (see §11). Pair with `move_to`. |
| `move_to`      | string          | action-only           | Destination folder for `move_dir`. |
| `swap_dir`     | string          | action-only           | Freeze a directory for the tick (see §11). |
| `clear_dir`    | string or list  | action-only           | Wipe a directory(ies) (see §11). |
| `goto`         | string          | action-only           | Jump to a label (see §12). |
| `if`           | map             | see §12              | Branch (see §12). |
| `exec`         | string or list  | action-only           | Run a shell command / argv (see §13). |

---

## 6. Per-step `options`

Overrides the backend's global generation settings for that one call. The shape
depends on the backend:

```yaml
# backend: mneme  (OpenAI-style names — the proxy maps these)
options: { temperature: 0.4, top_p: 0.9, top_k: 40, max_tokens: 400 }

# backend: ollama (native Ollama names)
options: { temperature: 0.7, top_p: 0.9, top_k: 40, num_predict: 400 }
```

Flow-style (`{ k: v }`) and block style are equivalent. For Mneme, `options` MUST
be nested under the top-level `"options"` key in the request payload — bare
`temperature`/`top_p` fields are ignored by the proxy.

---

## 7. `system_prompt` semantics by backend

- `backend: ollama` — the step's `system_prompt` IS the role/system prompt for
  that call.
- `backend: mneme` — the role prompt normally lives in that proxy's own
  `system_prompt.md`. A per-step `system_prompt` here is an *extra* instruction
  prepended on top of it; leave it unset unless you want that.

Use `|` block scalars for multi-line prompts (literal, newlines preserved) rather
than `>` (folded) when you want line breaks kept. Remember: YAML indentation
defines the block — every prompt line must be indented deeper than the
`system_prompt:` key.

---

## 8. `retry` (transient-failure retry)

`retry: N` re-issues the model call up to N **extra** times (so `retry: 2` = up
to 3 total attempts) on a *transient* failure only:

- connection/timeout error, or
- HTTP 5xx.

Backoff is exponential: `2 ** attempt` seconds between tries. **Permanent** errors
(HTTP 4xx, any non-200 that isn't 5xx, or a malformed response) stop the run
immediately — retry does not mask them. Default `0` (no retry). Ignored on
action-only steps (no model call to retry).

---

## 9. `read_dir` — reading context

`read_dir` reads one directory, or a **list** of directories, into a single text
blob. Each file is headed by a path marker:

- Single directory → `--- relpath ---` (e.g. `--- story.txt ---`).
- Multiple directories (2+) → `--- <dirbasename>/<relpath> ---` (e.g.
  `--- buffer/story.txt ---`) so the model can tell which source each file came
  from.

Details:

- Hidden files/dirs (dot-prefixed: `.DS_Store`, `.ipynb_checkpoints`, …) are
  skipped.
- Files are read in sorted order by relative path.
- Missing directories are skipped silently (not an error).
- A directory with no readable files yields the literal string `NO_INPUT`.
- A single-element list behaves exactly like a single string (plain header).
- `read_dir` reads **directories**, not single files — pointing it at a file path
  yields `NO_INPUT` (the file is not walked).

`read_dir` contents become the `user` message to the backend. For Mneme, this is
also the text the proxy embeds as its memory-retrieval query.

---

## 10. `write_dir` / `append_dir` — the extension rule

Both use the same path-resolution rule:

- Path **with** a file extension (e.g. `pass1/a2_synthesis.txt`) → a full file
  path; output is written/ appended to that exact file.
- Path **without** an extension (e.g. `pass1`) → a directory; `output.txt` is
  written/ appended inside it.

`write_dir` overwrites; `append_dir` appends (for a running log, or a story that
grows across ticks). **They can coexist in one step** — the same model output goes
to both (one overwrite, one append). `append_dir` adds no automatic newline
separator between appends.

To write the *same* output to two overwrite-style locations, use `write_dir` plus
`copy_dir`/`copy_to` in the same step (copy runs after write, picking up the fresh
file). To produce two *different* outputs, use two steps — one step makes exactly
one model call.

---

## 11. Folder primitives (action-only)

These move/transform state on disk and never call a model.

### `copy_dir` + `copy_to` — snapshot

Copies `copy_dir` into the `copy_to` folder. `copy_dir` and `copy_to` MUST appear
together (validation fails otherwise).

- Source is a **directory** → its **contents** are copied into `copy_to`
  (recursively, merging with what is already there; same-name files overwritten).
- Source is a **file** → copied into `copy_to` under its own name.

This is the "snapshot" primitive: it moves a *copy* of content OUT of the
freeze/consume loop so it survives the tick. E.g. copy `input.active` into
`buffer/` before the consume step, so later steps can read the frozen story after
`input.active` is cleared. Clear `copy_to` first (or on a prior step) to start
from a clean slate.

### `move_dir` + `move_to` — promote

Moves (renames) `move_dir` into the `move_to` folder. `move_dir`/`move_to` MUST
appear together. Atomic (`os.rename`) when source and destination share a
filesystem. The source is moved INTO `move_to` under its own basename (a file
keeps its name; a directory moves as a whole). The "promote draft → final"
primitive.

### `swap_dir` — freeze

Freezes `swap_dir` for the current tick (atomic inbox swap):

1. Removes any stale `<dir>.active` from a previous tick.
2. Renames `<dir>` → `<dir>.active` (the frozen snapshot readers use).
3. Recreates a fresh, empty `<dir>` for the NEXT tick's writes.

Readers point `read_dir` at `<dir>.active`; a later `clear_dir` on `<dir>.active`
consumes the snapshot. Use on an action-only step (no `write_dir`/`if`) so you
control *when* the freeze happens. If `<dir>` doesn't exist, it is created first
(so you get an empty `.active` and an empty `<dir>`).

### `clear_dir` — wipe

Deletes everything **under** `clear_dir`; the directory itself is kept. Accepts a
single directory or a **list** (so one step can reset several boards at once).
Missing dirs are skipped. **Cannot be combined with `goto`/`if`** (clearing on a
jump erases context the next step needs to read).

---

## 12. Control flow — `goto`, `if`, `END`

### `goto`

Jumps to a named step after the current one. **Not allowed with `clear_dir`/`if`.**

### `if` — two forms

Form A — **branch on the model's output** (requires a model call):

```yaml
if:
  condition: contains | equals | startswith | endswith | matches
  value:     "string to match"   # or regex pattern for `matches`
  then:      label_or_END         # when true
  else:      label_or_END         # when false (optional → fall through)
```

Matching semantics (against the step's raw output text):

- `contains`   — `value in output` (substring, case-sensitive).
- `equals`     — `output.strip() == value.strip()`.
- `startswith` — `output.strip().startswith(value)`.
- `endswith`   — `output.strip().endswith(value)`.
- `matches`    — `re.search(value, output, re.DOTALL)` (regex).

Form B — **branch on filesystem state** (no model call — action-only):

```yaml
if:
  condition: count_ge | count_lt | empty | exists
  dir:       directory_or_path
  value:     integer          # required for count_ge / count_lt
  then:      label            # when true
  else:      label            # when false (optional)
```

- `count_ge` / `count_lt` — number of non-hidden files under `dir` vs `value`.
- `empty` — `dir` has no files (or does not exist).
- `exists` — the path `dir` exists.

`count_*` and `empty` count **non-hidden files** only; a missing dir counts as 0
files (so `empty` is true, `count_lt: 1` is true, `count_ge: 1` is false).

`else` is optional: when a branch is taken but the corresponding label is absent,
the step falls through to the next index.

### `END` (reserved label)

Jumping to `END` (via `goto` or `if.then`/`if.else`) stops the run. The loop
otherwise runs until it hits `END`, the step list ends, `max_steps` trips, or you
Ctrl-C. You may not name a step `END`.

---

## 13. `exec` — run a command (action-only)

Runs a shell command or an argv list with no model call:

```yaml
- name: stamp
  exec: "python3 scripts/now.py board"        # string → run via shell
  # or, argv form (no shell — immune to quoting/spacing issues):
  exec: ["python3", "scripts/now.py", "board"]
```

- stdout is logged.
- A non-zero exit **aborts the run** with a clear message (stdout + stderr).
- Runs from the orchestrator's **working directory**.

### `scripts/now.py` — timestamp utility

`extensions/swarm/scripts/now.py` writes a timestamp for models to read. It
follows the same extension rule as `write_dir`:

- Path with extension → exact file (e.g. `now.py board/now.txt`).
- Path without extension → directory, `timestamp.txt` inside (e.g. `now.py board`).
- An **existing** directory is always treated as a directory (even a dotted name
  like `raw.active`).
- `-u` flag → UTC instead of local time.

Output format: `YYYY-MM-DD HH:MM:SS TZ (+OFFSET)`. Because `exec` is action-only,
the timestamp only reaches a model if a later step `read_dir`s that folder.

---

## 14. The canonical loop (worked example)

`swarm_config.yaml` ships a complete example. Its flow:

```
freeze → snapshot → critics → consume → gate
gate:  pass1 has ≥2 files → synthesize → decide
        otherwise         → finalize
decide: APPROVE → finalize | otherwise → revise → log → cleanup → loop
```

Steps in order:

1. `freeze` — `swap_dir: input` (freeze inbox → `input.active`).
2. `snapshot` — `copy_dir: input.active`, `copy_to: buffer`.
3. `critic_structure` — `backend: mneme`, `port: 8080`, `read_dir: input.active`,
   `write_dir: pass1/structure.txt`, `retry: 2`.
4. `critic_prose` — `backend: ollama`, `model: qwen2.5:14b`,
   `options: { temperature: 0.7, num_predict: 400 }`, `write_dir: pass1/prose.txt`.
5. `consume` — `clear_dir: input.active`.
6. `gate` — folder-state `if count_ge pass1 >= 2 → synthesize / finalize`.
7. `synthesize` — `read_dir: [buffer, pass1]`, `write_dir: pass2/synthesis.txt`.
8. `decide` — writes a verdict, `if matches '(?i)APPROVE' → finalize / revise`.
9. `revise` — `read_dir: [buffer, pass2]`, `write_dir: input/summary.txt`,
   `goto: log`.
10. `log` — `append_dir: log/verdicts.txt`.
11. `cleanup` — `clear_dir: [buffer, pass1, pass2]`.
12. `loop` — `delay: 2`, `goto: freeze`.
13. `finalize` — `read_dir: [buffer, pass2]`, `write_dir: output/story.txt`.
14. `promote` — `move_dir: output/story.txt`, `move_to: published`.

Read the in-file comments for a line-by-line explanation of why each primitive is
used where it is.

---

## 15. Parallel driver (`swarm_p_orchestrator.py`)

Adds exactly one new step form on top of everything above:

```yaml
steps:
  - name: freeze
    swap_dir: input
  - parallel:
      - name: critic_structure
        backend: mneme
        port: 8080
        read_dir: input.active
        write_dir: pass1/structure.txt
      - name: critic_prose
        backend: mneme
        port: 8080
        read_dir: input.active
        write_dir: pass1/prose.txt
  - name: consume
    clear_dir: input.active
```

- A `parallel:` step is a **list of leaf sub-steps** run concurrently via a thread
  pool (one worker per sub-step).
- Sub-steps are **leaf** steps: no `goto`/`if`/branching inside a block. Each
  reads shared input and writes a distinct output file.
- A sub-step may use any leaf field: `name`, `backend`, `port`/`model`, `options`,
  `system_prompt`, `retry`, `timeout`, `delay`, `exec`, `read_dir`, `write_dir`,
  `append_dir`, `copy_dir`/`copy_to`, `move_dir`/`move_to`, `swap_dir`,
  `clear_dir`.
- A sub-step with a string `if` would still call the model (the `if` is ignored),
  so don't put `if` in a block.

**When parallel helps:** sub-steps hitting the **same model** batch/parallelize on
Ollama (up to `OLLAMA_NUM_PARALLEL`). Sub-steps on **different models** that don't
both fit in VRAM get serialized by Ollama (model swap) — no speedup, just swap
latency. Fan out steps that share a model. Everything else is inherited unchanged,
so a config written for the serial driver also runs here.

---

## 16. Hot-reload (live edits)

The config file is re-read whenever its mtime changes on disk. On the next step
boundary the orchestrator rebuilds the flow and re-anchors the current position by
step name, so an edit takes effect **without a restart**. A broken/partial edit
(parse error, invalid flow) keeps the last good flow and logs a warning. When the
current step's name no longer exists after a reload, the flow restarts from the
top.

---

## 17. Startup validation (fail loud, before any model call)

The orchestrator validates the flow at load and aborts (`SystemExit`) on any of
these:

- A step is named `END` (reserved).
- Duplicate step `name`.
- `clear_dir` combined with `goto` or `if`.
- `copy_dir` present without `copy_to` (or vice versa).
- `move_dir` present without `move_to` (or vice versa).
- Unknown `backend` (not `mneme`/`ollama`).
- `backend: mneme` without `port` — only when the step calls a model.
- `backend: ollama` without `model` — only when the step calls a model.
- `read_dir` is not a string or list of strings.
- `retry` / `delay` is not numeric.
- `goto` target not found.
- `if.condition` unknown; a folder-state condition without `dir`; `count_ge`/
  `count_lt` without an integer `value`; a string condition (other than `matches`)
  without a `value`; an `if` with neither `then` nor `else`; a `then`/`else`
  target not found.

---

## 18. Gotchas & edge cases

- **Duplicate YAML keys are silently resolved** (PyYAML keeps the last one, no
  error). Do not write two `write_dir:` keys in one step — one silently wins.
  Use `write_dir`+`append_dir` or `write_dir`+`copy_dir` for two destinations.
- **Tabs break YAML.** Indentation must be spaces. The "mapping values are not
  allowed here" error at a nonsense column is usually a stray tab.
- **Paths are relative to the run directory.** The inbox/boards/output live where
  you launch the orchestrator, not where the config file sits.
- **`read_dir` reads directories, not single files.** Pointing it at a file yields
  `NO_INPUT`.
- **`append_dir` has no automatic newline** between appends.
- **`clear_dir` + `goto`/`if` is invalid** — put the jump on the next step.
- **A string-`if` step calls the model** even with no `write_dir` (to produce the
  branchable output). A folder-state `if` does not.
- **Mneme `options` must be nested** under the `"options"` key; bare generation
  fields are ignored.
- **`swap_dir` removes the previous `.active`** before re-freezing, so a reader
  holding a reference to the old snapshot must re-read after the swap.
- **`max_steps` counts every execution** (including action-only and loop steps),
  not just model calls.
- On RunPod, avoid port `8081`.

---

## 19. Writing a consumer (coding-agent notes)

To build your own driver against the same stack, you only need the HTTP contract
in §1. A minimal step executor is: read `read_dir` → POST to the backend → parse
`choices[0].message.content` (Mneme) or `message.content` (Ollama) → write/
append/copy/move/swap/clear in the fixed order of §4 → resolve `goto`/`if`.
The `Orchestrator` class in `swarm_orchestrator.py` is the reference
implementation; `ParallelOrchestrator` shows how to fan out independent leaf
steps concurrently.

# Swarm — example Mneme proxy consumer

A small, config-driven orchestrator that drives several Mneme proxies (and/or raw
Ollama models) through a loop defined in `swarm_config.yaml`.

This is **not** part of the Mneme stack. Mneme is the proxy (provider + DB + UI); the
orchestrator is an independent consumer that talks to proxies only over HTTP
(`http://localhost:<port>/v1/chat/completions`). You can swap it for a serial
orchestrator, a parallel fan-out, or any other driver without touching proxy code.

## As a guide for writing your own extension

This is the reference example for **extensions** — programs that add capability on top
of Mneme without sharing any code with it. If you want to build one, the pattern is:

1. **Decide what the proxy should do for you.** Anything sent to
   `POST /v1/chat/completions` comes back with memory retrieval, injection, the tool
   loop, and provenance grading already applied. You do not implement any of that.
2. **Talk to it over HTTP.** Any language, any machine. Nothing is imported.
3. **Keep your own state outside Mneme.** The swarm uses the filesystem as its shared
   board; that is one choice, not the required one.

The whole integration lives in one method — `call_mneme()` in `swarm_orchestrator.py`
(~20 lines). Read that first. The README's [Extensions](../../README.md#extensions)
section lists the other endpoints an extension can use (`/search` for retrieval without
a generation, the memory curation endpoints, `/health` for readiness).

Beyond that, the orchestration ideas worth stealing are in this document: control flow
in config rather than code, artifacts on disk as the communication channel between
steps, and per-step generation overrides.

## What it does

The example config runs a creative-writing loop that exercises **every** orchestrator
function — inbox freeze, snapshot, single and multi-source reads, named-file writes,
append, copy, move, single and list clears, string and folder-state branching, pacing,
retry, and both backends (`mneme` + `ollama`):

```
freeze -> snapshot -> critics -> consume -> gate -> synthesize -> decide
  gate:   pass1 has >=2 files -> synthesize | otherwise -> finalize
  decide: APPROVE -> finalize -> promote | otherwise -> revise -> log -> cleanup -> loop
```

## Files

- `swarm_orchestrator.py` — the serial driver (control flow `goto`/`if`, folder IO, mneme + ollama backends)
- `swarm_p_orchestrator.py` — the parallel driver: adds a `parallel:` block that fans out independent steps concurrently (see "Parallel")
- `swarm_config.yaml` — the loop definition (steps, ports, prompts, directories)
- `scripts/now.py` — timestamp utility for the `exec` step
- `SWARM_REFERENCE.md` — complete config reference (every field, primitive, and flow)

## Run it

1. Have your Mneme proxies running (see the setup wizard). On RunPod, avoid port 8081 —
   nginx reserves it.
2. Make a working directory for the run's state and put your brief in it:

   ```
   mkdir -p /workspace/swarm/input
   printf 'A generation ship on a thousand-year voyage...\n' > /workspace/swarm/input/story.txt
   ```

3. Run the orchestrator **from that working directory**:

   ```
   cd /workspace/swarm
   python3 /path/to/mneme/extensions/swarm/swarm_orchestrator.py \
       /path/to/mneme/extensions/swarm/swarm_config.yaml
   ```

All folder paths in the config are **relative to the directory you run from**, so the
inbox, boards, buffer, log and final output live in that directory (e.g. `/workspace/swarm`).
Pick a directory on persistent storage if you want to keep `output/` and `published/`;
the `buffer/` and `pass*/` boards are scratch.

**Live edits.** The config file is re-read whenever it changes on disk — edit a step's
`system_prompt`, `options`, `delay`, `retry`, or add/remove steps and the change applies on
the next step, no restart. A broken/partial edit keeps the last good flow. Input folders
(`read_dir`) are also re-read fresh every step.

## Config

Top level:

| key | meaning |
| --- | --- |
| `ollama_url` | base URL for `backend: ollama` steps (default `http://localhost:11434`) |
| `timeout` | default per-call timeout in seconds (default 600) |
| `max_steps` | safety cap on total step executions before stopping (default 0 = no limit) |

Step fields:

| field | meaning |
| --- | --- |
| `name` | optional label — a jump target and a log tag |
| `backend` | `mneme` (default) or `ollama` |
| `port` | Mneme proxy port (required for `backend: mneme`) |
| `model` | Ollama model name (required for `backend: ollama`) |
| `options` | per-step generation override (mneme: `temperature/top_p/top_k/max_tokens`; ollama: `temperature/top_p/top_k/num_predict`) |
| `system_prompt` | optional extra system message (for mneme, prepended to the proxy's own prompt) |
| `retry` | re-issue the call up to N extra times on a transient failure (timeout / HTTP 5xx) |
| `delay` | pause N seconds before the step runs (rate-limiting) |
| `read_dir` | directory, OR a **list** of directories, to read context from |
| `write_dir` | write output (OVERWRITE) — a path with an extension is a file, otherwise a directory (`output.txt` inside) |
| `append_dir` | like `write_dir`, but APPEND to the target (a running log / growing story) |
| `edit_dir` | edit a file IN PLACE from a prompt — the model outputs the full corrected file, gated by a similarity diff (see `SWARM_REFERENCE.md` §10.1) |
| `copy_dir` | source file/dir to copy; pair with `copy_to` |
| `copy_to` | destination folder for `copy_dir` |
| `move_dir` | source file/dir to move (rename); pair with `move_to` |
| `move_to` | destination folder for `move_dir` |
| `clear_dir` | directory, OR a **list** of directories, to wipe |
| `swap_dir` | directory to freeze (atomic rename to `<dir>.active` + recreate) |
| `goto` | label to jump to after this step |
| `if` | branch (see below) |
| `timeout` | per-step request timeout override |
| `exec` | run a shell command (string) or argv (list) as an action-only step — stdout is logged, non-zero exit aborts |

Key semantics:

- A model is called only when the step has `write_dir`, `append_dir`, `edit_dir`, or a STRING `if`.
  Action-only steps (`swap_dir` / `copy_dir` / `move_dir` / `clear_dir` / `exec` /
  `goto` / a folder-state `if`) never call a model.
- `read_dir: [a, b]` concatenates both directories into one context blob; each file is
  headed `--- <dir>/<relpath> ---` so the model can tell which source it came from.
  A single `read_dir: a` keeps the plain `--- path ---` header.
- `copy_dir: X` + `copy_to: Y` copies a snapshot of `X` outside the freeze/consume loop
  (e.g. copy `input.active` into `buffer/` before consuming it). A directory source copies
  its contents into `Y` (merging); a file source copies into `Y` under its own name.
- `move_dir: X` + `move_to: Y` atomically renames `X` into `Y` (promote draft -> final).
- `clear_dir` cannot be combined with `goto`/`if` — split the clear and the jump into
  separate steps (see `cleanup` + `loop` in the example).

### `if` — two forms

A) Branch on the model's output (needs a model call):

```yaml
if:
  condition: contains | equals | startswith | endswith | matches   # matches = regex
  value: "the string or pattern to match"
  then: label-or-END
  else: label-or-END        # optional
```

B) Branch on filesystem state (no model call):

```yaml
if:
  condition: count_ge | count_lt | empty | exists
  dir: directory-or-path
  value: 2                 # required for count_ge / count_lt
  then: label-or-END
  else: label-or-END       # optional
```

- `count_ge` / `count_lt`: number of files under `dir` vs `value`.
- `empty`: `dir` has no files (or does not exist).
- `exists`: the path `dir` exists.

### Timestamp utility (`scripts/now.py`)

Models have different training cutoffs, and small models often assume current
events are made up. Stamp a real timestamp each tick so every step shares one
consistent "now":

```yaml
  - name: stamp
    exec: "python3 scripts/now.py board"   # writes board/timestamp.txt (or -u for UTC)
  - name: critic
    read_dir: [board, input.active]
    # ...the critic now sees the timestamp in its context
```

`exec` runs the command as an action-only step (no model call); `now.py` writes
`YYYY-MM-DD HH:MM:SS TZ (+offset)`. Its target follows the same rule as
`write_dir`: a path WITH an extension is written to exactly, a path WITHOUT one
is a directory and gets `timestamp.txt` inside it. Use the string form for shell
commands (`python3 scripts/now.py board`) or a list to skip the shell
(`[python3, scripts/now.py, board]`).

## Parallel (`swarm_p_orchestrator.py`)

The serial driver runs one step at a time. The parallel driver adds one new step
form — a `parallel:` block that runs a list of independent sub-steps concurrently:

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

Run it exactly like the serial one: `python3 swarm_p_orchestrator.py swarm_config.yaml`.
Sub-steps are leaf steps (no `goto`/`if` inside a block); each reads the shared
input and writes a distinct output file.

Parallel is backend-agnostic — the fan-out is just a thread pool of independent HTTP calls,
so it works with both `mneme` and `ollama` sub-steps. What it buys you depends on the backend:

- **Hosted (OpenRouter / any OpenAI-compatible):** genuine speedup for both same-model and
  different-model sub-steps — there's no local GPU to thrash, each request runs on the
  provider's side, and wall-clock is the slowest sub-step, not the sum. This is where
  parallel shines.
- **Ollama (local, single GPU):** only sub-steps hitting the **same model**
  batch/parallelize (up to `OLLAMA_NUM_PARALLEL`); different models that don't both fit in
  VRAM get serialized by model-swap — no speedup, just swap latency.

One hosted caveat: rate limits. A large fan-out can hit HTTP 429 on a hosted key, and 4xx is
treated as permanent (stops the run immediately, no retry) — size the fan-out against your
key's rate limit.

## Adapt it

- Change each step's `port` to point at your proxies (one model per proxy; two roles may
  share a model via two ports).
- Add raw-Ollama steps with `backend: ollama` + `model:` + optional `options:`.
  Backend `mneme` needs only `port`.
- Control flow: `goto:` jumps to a named step; `if:` branches on the step's output
  (string conditions) or on filesystem state (folder conditions), with `then:` / `else:`
  targets (or `END`).
- Inbox swap: `swap_dir: raw` renames `raw` → `raw.active` and recreates `raw`, so readers
  read the frozen `raw.active` snapshot while new writes land in `raw`. Consume it with
  `clear_dir: raw.active`. Use these on action-only steps so you control when the freeze
  and consume happen.

Dependencies: `requests`, `pyyaml` (`pip install requests pyyaml`).

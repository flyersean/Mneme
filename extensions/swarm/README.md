# Swarm — example Mneme proxy consumer

A small, config-driven orchestrator that drives several Mneme proxies (and/or raw
Ollama models) through a loop defined in `swarm_config.yaml`.

This is **not** part of the Mneme stack. Mneme is the proxy (provider + DB + UI); the
orchestrator is an independent consumer that talks to proxies only over HTTP
(`http://localhost:<port>/v1/chat/completions`). You can swap it for a serial
orchestrator, a parallel fan-out, or any other driver without touching proxy code.

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

- `swarm_orchestrator.py` — the driver (control flow `goto`/`if`, folder IO, mneme + ollama backends)
- `swarm_config.yaml` — the loop definition (steps, ports, prompts, directories)

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
| `copy_dir` | source file/dir to copy; pair with `copy_to` |
| `copy_to` | destination folder for `copy_dir` |
| `move_dir` | source file/dir to move (rename); pair with `move_to` |
| `move_to` | destination folder for `move_dir` |
| `clear_dir` | directory, OR a **list** of directories, to wipe |
| `swap_dir` | directory to freeze (atomic rename to `<dir>.active` + recreate) |
| `goto` | label to jump to after this step |
| `if` | branch (see below) |
| `timeout` | per-step request timeout override |

Key semantics:

- A model is called only when the step has `write_dir`, `append_dir`, or a STRING `if`.
  Action-only steps (`swap_dir` / `copy_dir` / `move_dir` / `clear_dir` / `goto` / a
  folder-state `if`) never call a model.
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

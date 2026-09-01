# Swarm — example Mneme proxy consumer

A small, config-driven orchestrator that drives several Mneme proxies (and/or raw
Ollama models) through a loop defined in `swarm_config.yaml`.

This is **not** part of the Mneme stack. Mneme is the proxy (provider + DB + UI); the
orchestrator is an independent consumer that talks to proxies only over HTTP
(`http://localhost:<port>/v1/chat/completions`). You can swap it for a serial
orchestrator, a parallel fan-out, or any other driver without touching proxy code.

## What it does

The example config runs a creative-writing loop through 5 roles:

```
outline -> draft -> review -[REVISE]-> revise -[goto]-> review ...
   ... until review outputs "VERDICT: APPROVE", then finalize -> Speak/output.txt
```

## Files

- `swarm_orchestrator.py` — the driver (control flow `goto`/`if`, folder IO, mneme + ollama backends)
- `swarm_config.yaml` — the loop definition (steps, ports, prompts, directories)

## Run it

1. Have your Mneme proxies running (see the setup wizard). On RunPod, avoid port 8081 —
   nginx reserves it.
2. Make a working directory for the run's state and put your brief in it:

   ```
   mkdir -p /workspace/swarm/raw
   printf 'A generation ship on a thousand-year voyage...\n' > /workspace/swarm/raw/brief.txt
   ```

3. Run the orchestrator **from that working directory**:

   ```
   cd /workspace/swarm
   python3 /path/to/mneme/extensions/swarm/swarm_orchestrator.py \
       /path/to/mneme/extensions/swarm/swarm_config.yaml
   ```

All `read_dir` / `write_dir` / `clear_dir` / `swap_dir` paths in the config are
**relative to the directory you run from**, so the brief, intermediate boards, and final
output live in that directory (e.g. `/workspace/swarm` on a pod). Pick a directory on
persistent storage if you want to keep `Speak/output.txt`; the `brain/` boards are scratch.

## Adapt it

- Change each step's `port` to point at your proxies (one model per proxy; two roles may
  share a model via two ports).
- Add raw-Ollama steps with `backend: ollama` + `model:` + optional `options:`
  (temperature / num_predict / top_p / top_k). Backend `mneme` needs only `port`.
- Control flow: `goto:` jumps to a named step; `if:` branches on the step's output
  (`contains` / `equals` / `startswith` / `endswith` / `matches`), with `then:` / `else:`
  targets (or `END`). `clear_dir` cannot be combined with `goto`/`if`.
- Inbox swap (for a loop fed by a stream of files — tool results, a feed): `swap_dir: raw`
  atomically renames `raw` → `raw.active` and recreates a fresh `raw`, so readers read the
  frozen `raw.active` snapshot (all see identical bytes) while new writes land in `raw`.
  Consume the snapshot with `clear_dir: raw.active`. Use these on action-only steps (no
  `read_dir`/`write_dir`/`if`) so you control exactly when the freeze and consume happen.
- Per-step `timeout:` overrides the top-level `timeout:`.

Dependencies: `requests`, `pyyaml` (`pip install requests pyyaml`).

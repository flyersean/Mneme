# Setup wizard: add-proxy / reconfigure leaves proxies on a stale model

Status: FIXED in code (commit 3e11d53, pushed) — live verification pending on a pod
Date: 2026-09-25 (fixed 2026-09-26)

## Fix (shipped)
- Config now carries TOP-LEVEL `model:` / `embed_model:` / `label_model:` keys
  (authoritative for BOTH backends; Ollama no longer env-only).
- Generated start scripts + start_proxy()/start_instance() CLEAR inherited
  model env vars instead of exporting them, so the config wins.
- _resolve_provider() falls back to providers.openrouter.model for Ollama when
  the top-level key is absent (pre-upgrade config back-compat).
- 6 regression tests (test_generated_config +14, test_config_load +9).
- Remaining live-verify: add-proxy / reconfigure on a real pod and confirm the
  model actually changes (config -> env -> MODEL, and old process is dead).

## Symptom (user report)
After several "Add a proxy" runs and "Reconfigure this install" runs, some
proxies locked onto the PREVIOUS model. The instance's mneme.yaml looked
correct, but the proxy log showed Mneme trying to connect to a model that had
been `ollama rm`'d, and failing.

## Root cause (confirmed in code)
Model identity is ENV-driven and read once at import — NOT from the config's
`model` key.

  proxy/mneme_proxy.py:486
    MODEL = os.environ.get("MNEME_MODEL", "<hardcoded default>")

  proxy/mneme_proxy.py:189   "model": "MNEME_MODEL",          # config key -> env
  proxy/mneme_proxy.py:292   _set("MNEME_MODEL", "model")      # applied at config load

So `MODEL` is frozen at import time from the MNEME_MODEL env var (which the
generated start_proxy.sh exports). The config's top-level `model:` key is only
applied if MNEME_MODEL is NOT already set — and the start script sets it, so
env wins and the config key is ignored. Result: "config looks right" while the
running proxy stays on the model baked into its env/start script.

Two ways this bites after add-proxy/reconfigure:
1. Old proxy PROCESS still alive (stop didn't take / port held) → it keeps its
   old MNEME_MODEL and keeps serving the old (now removed) model.
2. Stale start_proxy.sh still exporting the old MNEME_MODEL → a fresh start
   still boots onto the removed model.

## Diagnostic (on the pod, for every instance)
  ss -ltnp | grep <port>                       # which PID actually owns the port
  tr '\0' '\n' < /proc/<pid>/environ | grep MNEME_MODEL   # the REAL model in use
  grep MNEME_MODEL instances/<port>/start_proxy*.sh       # what the script exports
  grep -i '^model' instances/<port>/mneme.yaml            # what config claims
  curl -s localhost:<port>/health                         # what's actually served
Smoking gun = /proc/<pid>/environ MNEME_MODEL != config model (and/or a stale PID).

## Test plan (next session)
1. Add proxy -> model A: verify /health + environ MNEME_MODEL == A, start script == A.
2. Add proxy -> model B (new port): verify B, and that A's proxy is untouched.
3. Reconfigure same port: A -> C. Verify environ + /health == C, OLD PID gone.
4. Reconfigure to a DIFFERENT port: verify the old port's proxy is LEFT RUNNING
   (not killed), new port serves the new model.
5. After each, assert start-script / config / running process all agree on model.

## Already fixed this session (do NOT re-diagnose)
- generated start scripts free their port before starting (port-freeing block) —
  but EXISTING on-disk scripts predate this and must be regenerated.
- reconfigure now stops the old instance only when the port is UNCHANGED
  (port == reconf_port); it previously stopped the old port even on a new port.
- max_server_rounds is configurable (caps.max_server_rounds).
- bash/write share the tools dir as their relative-path base.

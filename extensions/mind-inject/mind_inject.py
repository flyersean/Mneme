#!/usr/bin/env python3
"""mind_inject — a generic "swarm tick" for Mneme.

WHAT IT DOES
  Reads recent context from a Mneme proxy's memory DB, runs ONE mind module
  (creative, guardian, curiosity, ...) through a cheap one-off model (a DIRECT
  API call — it never touches the proxy, so nothing is written to the DB), then
  injects the output into the target model's thought flow via the force-flow
  steer file. The injection is consumed ONCE, at the target model's NEXT tool
  boundary (the proxy wraps it as a `[CHUNK #LIVE-USER-STEERING | PRIORITY:
  CRITICAL]` note and prepends it to that tool's result).

ONE MODULE = ONE YAML FILE
  modules/<name>.yaml holds everything: prompt, model, target proxy, memory
  window, and schedule. To scaffold a NEW module, copy modules/_TEMPLATE.yaml,
  change `module` + `prompt` (and the model if you want), and you're done — no
  code changes.

USAGE
  python3 mind_inject.py modules/creative.yaml              # run ONE tick (test)
  python3 mind_inject.py modules/creative.yaml --daemon     # loop every interval_minutes

  The runner is a standalone program: it talks to proxies only over HTTP
  (localhost:<port>), imports nothing from the proxy, and cannot break it.
"""

import argparse
import os
import sys
import time

import requests
import yaml


# ── Helpers ────────────────────────────────────────────────────────────────

def _load_env_file(path: str) -> dict:
    """Parse a KEY=VALUE env file (the proxy's shared provider-env file) into a
    dict. Non-secret values only; keys are consumed by name in _run_model."""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def _read_memory(proxy: str, recent: int) -> str:
    """Pull the N most recent chunks from the proxy's memory DB (read-only).

    GET /list  -> recent chunk ids + topic labels (newest first)
    GET /detail/<id> -> a chunk's messages

    Returns the chunks joined into one context string, each headed by its topic
    label. This is what the module "looks at" for this tick.
    """
    r = requests.get(f"{proxy}/list", timeout=15)
    r.raise_for_status()
    chunks = r.json().get("chunks", [])[:recent]
    parts = []
    for c in chunks:
        topic = (c.get("topic_label") or "").strip()
        body = ""
        try:
            d = requests.get(f"{proxy}/detail/{c['chunk_id']}", timeout=15).json()
            # Flatten the chunk's messages to plain text (first 160 chars each
            # so the context stays compact).
            body = " ".join(
                m["content"][:160] for m in d.get("messages", []) if m.get("content")
            )
        except Exception:
            pass  # a chunk we can't read is skipped, not fatal
        line = f"[{topic}] {body}".strip() if topic else body.strip()
        if line:
            parts.append(line)
    return "\n\n".join(parts)


def _run_model(cfg: dict, context: str, env: dict) -> str:
    """One-off call to the module's model. Direct API — no proxy, no DB save.

    The reasoning pass is DISABLED by default (reasoning_enabled: false): a
    cheap one-off model that reasons will burn its whole token budget on
    thinking and return an empty body (finish_reason=length). Disabling it makes
    the model emit content directly — faster, cheaper, and it actually answers.
    Set reasoning_enabled: true if you'd rather it think first (costs more).
    """
    key = env.get(cfg["key_env"], "")
    if not key:
        raise SystemExit(f"[{cfg['module']}] {cfg['key_env']} not found in env file")

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg["prompt"]},
            {"role": "user", "content": "Recent context from memory:\n\n" + context},
        ],
        "max_tokens": cfg.get("max_tokens", 1000),
        "stream": False,
    }
    if cfg.get("temperature") is not None:
        # Sampling temperature for THIS module (higher = more varied/creative).
        payload["temperature"] = float(cfg["temperature"])
    if not cfg.get("reasoning_enabled", False):
        payload["reasoning"] = {"enabled": False}

    r = requests.post(
        cfg["base_url"],
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=cfg.get("timeout_seconds", 120),
    )
    r.raise_for_status()
    content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return (content or "").strip()


def _inject(proxy: str, content: str) -> None:
    """Write the generated text to the target proxy's force-flow steer file.

    POST /steer writes steer.txt; the proxy consumes it ONCE at the next tool
    boundary. Last-write-wins: a fresh tick overwrites any un-consumed note.
    """
    r = requests.post(f"{proxy}/steer", json={"message": content}, timeout=10)
    r.raise_for_status()


# ── One tick ───────────────────────────────────────────────────────────────

def run_tick(cfg: dict, env: dict) -> bool:
    """Run one full tick: read memory -> run module -> inject. Returns True if a
    note was actually injected (False = nothing to say / empty output)."""
    name = cfg["module"]
    context = _read_memory(cfg["proxy"], cfg.get("recent_chunks", 6))
    if not context.strip():
        print(f"[{name}] no memory context — skipping", flush=True)
        return False
    out = _run_model(cfg, context, env)
    if not out:
        print(f"[{name}] model returned empty — skipping", flush=True)
        return False
    _inject(cfg["proxy"], out)
    print(f"[{name}] injected {len(out)} chars", flush=True)
    return True


# ── Entry point ────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mind-module injector (swarm tick)")
    ap.add_argument("config", help="path to a module YAML, e.g. modules/creative.yaml")
    ap.add_argument("--daemon", action="store_true",
                    help="loop forever, ticking every interval_minutes")
    ap.add_argument("--once", action="store_true",
                    help="run a single tick and exit (default)")
    args = ap.parse_args(argv)

    # /extensions page config-form overrides (the manifest injects these as env
    # vars when the extension is run from the UI). Purely optional.
    _mod = os.environ.get("MIND_INJECT_MODULE", "").strip()
    if _mod:
        args.config = os.path.join(os.path.dirname(args.config) or ".", f"{_mod}.yaml")

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    env = _load_env_file(cfg.get("env_file", "/home/ubuntu/mneme/env"))

    if args.daemon:
        interval = int(cfg.get("interval_minutes", 10)) * 60
        print(f"[{cfg['module']}] daemon — every {interval // 60} min (Ctrl-C to stop)", flush=True)
        while True:
            try:
                run_tick(cfg, env)
            except Exception as e:
                print(f"[{cfg['module']}] tick error: {e}", flush=True)
            time.sleep(interval)
    else:
        try:
            run_tick(cfg, env)
        except Exception as e:
            print(f"[{cfg['module']}] error: {e}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

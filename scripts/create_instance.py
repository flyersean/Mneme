#!/usr/bin/env python3
"""Create a new Mneme proxy instance by cloning an existing one.

Usage:
    python3 scripts/create_instance.py --port 8082 --model deepseek/deepseek-chat \
        [--from-port 8080] [--start]

Clones chunks/instances/<from_port>/mneme.yaml + start_proxy.sh into
chunks/instances/<port>/, rewriting only what must differ per instance:
port number and main chat model. The memory DB is isolated per-instance by
default; pass --db-path to connect to an existing (shared) memory DB instead.

This deliberately does NOT shell out to mneme_setup.py — that wizard is
interactive-only (no CLI flags), so non-interactive reuse would mean
importing its internals with a pile of stubs. Cloning a known-good
config is simpler and less likely to drift from what actually runs.
"""
import argparse
import os
import re
import subprocess
import sys

INSTANCES = os.environ.get(
    "MNEME_INSTANCES", "/home/ubuntu/mneme/chunks/instances")


def find_main_model(cfg_text):
    """First 'model:' line at top level (the main chat model id)."""
    m = re.search(r'^model:\s*"?([^"\n]+?)"?\s*$', cfg_text, re.M)
    if not m:
        sys.exit("could not locate 'model:' key in source yaml")
    return m.group(1).strip()


def find_db_path(cfg_text):
    """Value of storage.db_path (the shared-memory DB path), or None."""
    m = re.search(r'^\s*db_path:\s*"?([^"\n]+?)"?\s*$', cfg_text, re.M)
    return m.group(1).strip() if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", required=True,
                    help="main chat model id, e.g. deepseek/deepseek-chat")
    ap.add_argument("--from-port", type=int, default=8080)
    ap.add_argument("--db-path", default=None,
                    help="storage.db_path for the new instance. Omit to auto-create "
                         "an isolated per-instance DB (chunks/instances/<port>/mneme.db); "
                         "pass an existing path to SHARE memory with that proxy.")
    ap.add_argument("--start", action="store_true",
                    help="start the new instance after creating it")
    args = ap.parse_args()

    src_dir = os.path.join(INSTANCES, str(args.from_port))
    dst_dir = os.path.join(INSTANCES, str(args.port))
    if not os.path.isdir(src_dir):
        sys.exit(f"source instance {src_dir} does not exist")
    if os.path.isdir(dst_dir):
        sys.exit(f"destination {dst_dir} already exists — refusing to overwrite")

    src_yaml = os.path.join(src_dir, "mneme.yaml")
    src_sh = os.path.join(src_dir, "start_proxy.sh")
    for p in (src_yaml, src_sh):
        if not os.path.isfile(p):
            sys.exit(f"source missing {p}")

    with open(src_yaml) as f:
        cfg = f.read()
    old_main = find_main_model(cfg)

    # Port appears as 'port: 8080', URLs, and paths — replace all occurrences.
    cfg = re.sub(rf"\b{args.from_port}\b", str(args.port), cfg)

    # Memory DB: the creator chooses shared or isolated.
    #   --db-path <existing>  -> connect to that DB (shared memory with another proxy)
    #   (omit)                -> auto-create an isolated per-instance DB
    src_db = find_db_path(cfg)
    if src_db:
        new_db = args.db_path or os.path.join(
            INSTANCES, str(args.port), "mneme.db")
        cfg = cfg.replace(src_db, new_db, 1)

    # Replace ONLY lines naming the main chat model — embedder untouched.
    lines = []
    for ln in cfg.splitlines():
        m = re.match(r'^(\s*(?:model|id):\s*)"?\S+"?\s*$', ln)
        if m:
            body = ln.split(':', 1)[1].strip().strip('"')
            if body == old_main:
                indent = ln[:len(ln) - len(ln.lstrip())]
                key = ln.strip().split(':', 1)[0]
                ln = f'{indent}{key}: "{args.model}"'
        lines.append(ln)
    cfg = "\n".join(lines) + "\n"

    os.makedirs(dst_dir, exist_ok=True)
    dst_yaml = os.path.join(dst_dir, "mneme.yaml")
    with open(dst_yaml, "w") as f:
        f.write(cfg)
    print(f"wrote {dst_yaml}")

    with open(src_sh) as f:
        sh = f.read()
    sh = sh.replace(str(args.from_port), str(args.port))
    dst_sh = os.path.join(dst_dir, "start_proxy.sh")
    with open(dst_sh, "w") as f:
        f.write(sh)
    os.chmod(dst_sh, 0o755)
    print(f"wrote {dst_sh}")
    print(f"instance {args.port} created (model {args.model}, "
          f"cloned from {args.from_port})")

    if args.start:
        print("starting...")
        r = subprocess.run(["setsid", "./start_proxy.sh"], cwd=dst_dir,
                           capture_output=True, text=True, timeout=60)
        print(r.stdout[-2000:] if r.stdout else "", end="")
        print(r.stderr[-2000:] if r.stderr else "", end="")
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()

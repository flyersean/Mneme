#!/usr/bin/env python3
"""Mneme OpenRouter setup wizard — LOCAL machine, fully hosted models.

Sets up the unified_mneme build of Mneme on a laptop/desktop. No Ollama,
no GPU, no model downloads — the main LLM, embedder, and labeler all run on
OpenRouter. This script:

  1. Asks for (and validates) your OpenRouter API key, saves it to
     ~/mneme/env (chmod 600, never written into the repo).
  2. Lets you pick the main / embedder / labeler models (cheap defaults).
  3. Creates a venv (~/mneme/venv) with faiss/numpy/flask/requests if needed.
  4. Writes a config + start script, launches the proxy, and health-checks it.

Self-contained (stdlib only) so it runs via:
  curl -sSL -o /tmp/setup_or.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_setup_openrouter.py && python3 /tmp/setup_or.py
"""

import os
import sys
import json
import time
import shutil
import socket
import getpass
import subprocess
import urllib.request
import urllib.error

# ── Defaults ─────────────────────────────────────────────────────
DEFAULT_MAIN   = "deepseek/deepseek-v4-flash"        # cheap thinking model
DEFAULT_EMBED  = "voyageai/voyage-4-lite"            # 1024-dim (matches FAISS)
DEFAULT_LABEL  = "meta-llama/llama-3.2-3b-instruct"  # small, non-thinking

OR_BASE       = "https://openrouter.ai/api/v1"
KEY_FILE      = os.environ.get("MNEME_KEY_FILE", os.path.expanduser("~/mneme/env"))
VENV_DIR      = os.environ.get("MNEME_VENV_DIR", os.path.expanduser("~/mneme/venv"))
MEMORY_DIR    = os.environ.get("MNEME_CHUNK_DIR", os.path.expanduser("~/mneme/chunks"))
_REPO_DERIVED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = None  # resolved in main() via find_repo()

MAIN_MODELS = [
    ("deepseek/deepseek-v4-flash  (cheapest thinking MoE, $0.064/$0.128)", "deepseek/deepseek-v4-flash"),
    ("deepseek/deepseek-chat      (V3, non-thinking, $0.50/$0.70)",         "deepseek/deepseek-chat"),
    ("qwen/qwen3-32b              (open-weight, $0.08/$0.28)",              "qwen/qwen3-32b"),
    ("Custom (enter any OpenRouter model id)",                              "__custom__"),
]

EMBED_MODELS = [
    ("voyageai/voyage-4-lite  (1024-dim, $0.02/M — recommended)", "voyageai/voyage-4-lite"),
    ("Custom (WARNING: must output 1024-dim or change DIM)",        "__custom__"),
]

LABEL_MODELS = [
    ("meta-llama/llama-3.2-3b-instruct  (small, non-thinking — recommended)", "meta-llama/llama-3.2-3b-instruct"),
    ("Custom (WARNING: must be NON-thinking or labels come back empty)",       "__custom__"),
]


# ── Helpers ──────────────────────────────────────────────────────
def run(cmd, timeout=None):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def ask(prompt, default=None):
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


def choose(prompt, options):
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"Choice (1-{len(options)}): ").strip()
        try:
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return idx, options[idx]
        except Exception:
            pass
        print(f"  Enter 1-{len(options)}")


def banner():
    print("""
  \033[36m███╗   ███╗███╗   ██╗███████╗███╗   ███╗███████╗
  ████╗ ████║████╗  ██║██╔════╝████╗ ████║██╔════╝
  ██╔████╔██║██╔██╗ ██║█████╗  ██╔████╔██║█████╗
  ██║╚██╔╝██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══╝
  ██║ ╚═╝ ██║██║ ╚████║███████╗██║ ╚═╝ ██║███████╗
  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚══════╝\033[0m

  Conversational memory proxy — OpenRouter backend (local, no Ollama)
""")


def find_repo():
    """Locate the Mneme repo (needs proxy/mneme_proxy.py). Tries the script's
    own location, then common checkout paths, then offers to git-clone."""
    candidates = [
        _REPO_DERIVED,
        os.path.expanduser("~/mneme/repo"),
        os.path.expanduser("~/mneme"),
        os.getcwd(),
    ]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "proxy", "mneme_proxy.py")):
            return c
    print("\n  Mneme proxy code not found. Where is the repo checked out?")
    print("  (Leave blank to git-clone the unified_mneme branch now.)")
    path = input("  Repo path [clone]: ").strip()
    if path:
        if os.path.exists(os.path.join(path, "proxy", "mneme_proxy.py")):
            return path
        print(f"  proxy/mneme_proxy.py not found in {path}.")
        sys.exit(1)
    dest = os.path.expanduser("~/mneme/repo")
    print(f"  Cloning into {dest} ...")
    r = run(f"git clone --branch unified_mneme https://github.com/flyersean/Mneme.git {dest}", timeout=300)
    if r.returncode != 0:
        print(f"  ✗ Clone failed: {r.stderr[:300]}")
        sys.exit(1)
    return dest


# ── OpenRouter key ───────────────────────────────────────────────
def load_saved_key():
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def or_get(key, path):
    req = urllib.request.Request(OR_BASE + path, headers={"Authorization": f"Bearer {key}"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None
        try:
            return json.loads(e.read())
        except Exception:
            return None
    except Exception:
        return None


def show_credits(key):
    d = or_get(key, "/credits")
    if d and "data" in d:
        data = d["data"]
        total = data.get("total_credits", 0)
        usage = data.get("total_usage", 0)
        remain = max(total - usage, 0)
        print(f"  Credits: ${remain:.2f} remaining (used ${usage:.2f} of ${total:.2f})")
        return remain
    return None


def ask_and_validate_key():
    """Prompt for the OR key (masked), validate it, save it."""
    saved = load_saved_key()
    if saved:
        print("\n  Saved OpenRouter key found — validating...")
        info = or_get(saved, "/auth/key")
        if info and "data" in info:
            print("  ✓ Valid key. Keeping it.")
            show_credits(saved)
            return saved
        print("  Saved key is invalid — please re-enter.")

    while True:
        print("\n\033[1mOpenRouter API key\033[0m")
        print("  Create one at https://openrouter.ai/keys (sk-or-v1-...).")
        key = getpass.getpass("  API key (input is hidden): ").strip()
        if not key:
            print("  No key entered.")
            continue
        info = or_get(key, "/auth/key")
        if info and "data" in info:
            print("  ✓ Valid key.")
            save_key(key)
            show_credits(key)
            return key
        print("  ✗ Invalid key — check it and try again.")


def save_key(key):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    with open(KEY_FILE, "w") as f:
        f.write(f"OPENROUTER_API_KEY={key}\n")
    os.chmod(KEY_FILE, 0o600)
    print(f"  Saved key to {KEY_FILE} (chmod 600).")


# ── Venv ─────────────────────────────────────────────────────────
def ensure_venv():
    py = os.path.join(VENV_DIR, "bin", "python")
    if os.path.exists(py):
        print(f"\n  venv found: {VENV_DIR}")
        return py
    print(f"\n  Creating venv at {VENV_DIR} (installs faiss/numpy/flask/requests)...")
    print("  (~250MB — this is the only local footprint; models stay on OpenRouter)")
    r = run(f"python3 -m venv {VENV_DIR}", timeout=300)
    if r.returncode != 0:
        print(f"  ✗ Failed to create venv: {r.stderr[:300]}")
        print("    Try: sudo apt install python3-venv  (Debian/Ubuntu) and re-run.")
        sys.exit(1)
    r = run(f"{py} -m pip install --quiet --no-cache-dir faiss-cpu numpy flask flask-cors requests pyyaml", timeout=600)
    if r.returncode != 0:
        print(f"  ✗ pip install failed: {r.stderr[:500]}")
        sys.exit(1)
    print("  ✓ venv ready.")
    return py


# ── Config + start script ────────────────────────────────────────
def write_config(cfg):
    """Write the proxy config file ($MNEME_CHUNK_DIR/mneme.yaml).

    The unified_mneme build reads mneme.yaml (NOT the old setup_config.json),
    so we emit the full config with the picked models + sensible defaults.
    Kept stdlib-only: the yaml is written as a fixed-shape string, no PyYAML.
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)
    inject = "true" if str(cfg.get("inject_system")) == "1" else "false"
    yaml = f"""# Mneme proxy config — generated by mneme_setup_openrouter.py.
# Precedence: environment variable > this file > built-in default.
# Full reference: docs/config-spec.md in the repo.

backend:
  type: openai
  provider: openrouter

providers:
  openrouter:
    base_url: {OR_BASE}
    api_key_env: OPENROUTER_API_KEY   # key is read from this env var, never stored here
    model: "{cfg['model']}"
    embed_model: "{cfg['embed_model']}"
    label_model: "{cfg['label_model']}"

sampling:
  temperature: 0.2
  top_p: 0.9
  ctx_tokens: 256000
  completion_reserve: 8192

timeouts:
  chat_timeout: 60
  ollama_chat_timeout: 120
  first_token_timeout: 30
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30

storage:
  chunk_dir: "{MEMORY_DIR}"
  port: {cfg['port']}
  inject_system: {inject}
  staging_turns: 6
  staging_idle: 120
  belief_evolution: false

retrieval:
  max_injected_tokens: 8000
  inject_min_similarity: 0.62
  keyword_fallback: false
  route_threshold: 0.08
  classify_threshold: 0.78
  baseline_noise: 0.20
  age_decay_days: 7
  max_siblings: 3
  max_chunk_words: 500
  max_chunk_size: 10000

caps:
  max_history_messages: 32
  db_msg_cap: 8000
  compress_threshold: 500
  compress_max_tok: 2048
  max_tool_forward: 12000
  chunk_size: 4000
"""
    with open(os.path.join(MEMORY_DIR, "mneme.yaml"), "w") as f:
        f.write(yaml)


def _read_yaml_values(path):
    """Lightweight scan of a generated mneme.yaml for the keys the wizard shows
    on reconfigure. Keeps this script stdlib-only (no PyYAML import)."""
    out = {}
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return out
    for key in ("model", "embed_model", "label_model", "port", "inject_system"):
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(key + ":"):
                out[key] = s.split(":", 1)[1].split("#", 1)[0].strip().strip('"').strip("'")
                break
    return out


def write_start_script(py, cfg):
    """Write a start script next to the DB that sources the key + launches the
    proxy. Models/port/etc. come from mneme.yaml (written by write_config), so
    the script only needs the chunk dir to locate that file."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, "start_proxy.sh")
    with open(path, "w") as f:
        f.write(f"""#!/bin/bash
# Mneme proxy — OpenRouter backend (generated by mneme_setup_openrouter.py)
# Start/restart with:  {path}

# Source the saved API key unless one is already exported
if [ -z "${{OPENROUTER_API_KEY:-}}" ] && [ -f "{KEY_FILE}" ]; then
  export $(grep -v '^#' "{KEY_FILE}" | xargs)
fi

# The proxy reads its config (models, port, etc.) from $MNEME_CHUNK_DIR/mneme.yaml
export MNEME_CHUNK_DIR="{MEMORY_DIR}"
export PYTHONDONTWRITEBYTECODE=1

cd "{REPO_ROOT}"
exec {py} -uB proxy/mneme_proxy.py
""")
    os.chmod(path, 0o755)
    return path


def free_port(start=8080):
    for p in range(start, 8100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if s.connect_ex(("127.0.0.1", p)) != 0:
            s.close()
            return p
        s.close()
    return 8080


def start_proxy(py, cfg, api_key):
    env = os.environ.copy()
    env.update({
        "OPENROUTER_API_KEY": api_key,
        "MNEME_CHUNK_DIR": MEMORY_DIR,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    log = open("/tmp/mneme_openrouter.log", "w")
    subprocess.Popen(
        [py, "-uB", "proxy/mneme_proxy.py"],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=log,
        start_new_session=True,
    )
    print(f"  Starting proxy on port {cfg['port']}...", end=" ", flush=True)
    for _ in range(20):
        time.sleep(1)
        try:
            d = json.loads(urllib.request.urlopen(f"http://localhost:{cfg['port']}/health", timeout=3).read())
            print(f"running ({d.get('chunks', 0)} chunks, backend={d.get('backend')})")
            return True
        except Exception:
            continue
    print("timeout — check /tmp/mneme_openrouter.log")
    return False


# ── Main ─────────────────────────────────────────────────────────
def main():
    global REPO_ROOT
    banner()
    REPO_ROOT = find_repo()
    print(f"  Repo: {REPO_ROOT}")

    os.makedirs(MEMORY_DIR, exist_ok=True)
    existing_db = os.path.join(MEMORY_DIR, "mneme.db")
    existing_config = os.path.join(MEMORY_DIR, "mneme.yaml")

    prev = {}
    if os.path.exists(existing_db):
        chunk_count = "?"
        try:
            import sqlite3
            cdb = sqlite3.connect(existing_db)
            chunk_count = cdb.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            cdb.close()
        except Exception:
            pass
        print(f"\n  Existing memory DB found: {chunk_count} chunks at {MEMORY_DIR}")
        if os.path.exists(existing_config):
            prev = _read_yaml_values(existing_config)

        opts = [
            "Reconfigure (change models/key, keep existing memory)",
            "Start fresh (wipe memory DB and reinstall)",
        ]
        _, choice = choose("\nWhat would you like to do?", opts)
        if "Reconfigure" in choice:
            print(f"\n  Reconfiguring. Current: model={prev.get('model')}, "
                  f"embed={prev.get('embed_model')}, label={prev.get('label_model')}")
        else:
            print("\n  Wiping existing memory DB...")
            shutil.rmtree(MEMORY_DIR, ignore_errors=True)
            os.makedirs(MEMORY_DIR, exist_ok=True)
            prev = {}

    # 1. API key
    api_key = ask_and_validate_key()

    # 2. Models
    print("\n\033[1mModels (all hosted on OpenRouter — nothing downloaded)\033[0m")
    idx, _ = choose("Main model", [m[0] for m in MAIN_MODELS])
    if MAIN_MODELS[idx][1] == "__custom__":
        model = ask("Enter OpenRouter model id", DEFAULT_MAIN) or DEFAULT_MAIN
    else:
        model = MAIN_MODELS[idx][1]

    idx, _ = choose("Embedder (memory vectors)", [m[0] for m in EMBED_MODELS])
    if EMBED_MODELS[idx][1] == "__custom__":
        embed_model = ask("Enter embedder model id (must be 1024-dim)", DEFAULT_EMBED) or DEFAULT_EMBED
    else:
        embed_model = EMBED_MODELS[idx][1]

    idx, _ = choose("Labeler (topic labels)", [m[0] for m in LABEL_MODELS])
    if LABEL_MODELS[idx][1] == "__custom__":
        label_model = ask("Enter labeler model id (must be non-thinking)", DEFAULT_LABEL) or DEFAULT_LABEL
    else:
        label_model = LABEL_MODELS[idx][1]

    # 3. Injection preference
    inject_opts = [
        ("Yes — inject Mneme instructions (default)", "1"),
        ("No — skip (use a merged prompt from your harness)", "0"),
    ]
    idx, _ = choose("\nInject Mneme system instructions?", [o[0] for o in inject_opts])
    inject_system = inject_opts[idx][1]

    port = int(ask("Proxy port", "8080") or "8080")

    # 4. venv
    py = ensure_venv()

    # 5. Write config + start script, start proxy
    cfg = {
        "backend": "openrouter",
        "model": model,
        "embed_model": embed_model,
        "label_model": label_model,
        "port": port,
        "inject_system": inject_system,
    }
    write_config(cfg)
    start_script = write_start_script(py, cfg)
    started = start_proxy(py, cfg, api_key)

    # 6. Wrap up
    print("\n\033[1mSetup complete.\033[0m")
    print(f"  Memory DB:   {MEMORY_DIR}")
    print(f"  Config:      {MEMORY_DIR}/mneme.yaml")
    print(f"  Start/stop:  {start_script}")
    print(f"  Log:         /tmp/mneme_openrouter.log")
    print(f"\n  Stop it:      kill $(ss -tlnp | grep :{port} | grep -oP 'pid=\\K[0-9]+')")
    print(f"  Restart it:   {start_script}")
    print("\n  The proxy is OpenAI-compatible at http://localhost:%d/v1" % port)
    print("  Test:  curl http://localhost:%d/health" % port)
    print()
    print("  The API key lives only in ~/mneme/env (chmod 600).")
    print("  To rotate it, re-run this wizard or edit that file.")
    return 0 if started else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Cancelled.")
        sys.exit(130)

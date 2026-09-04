#!/usr/bin/env python3
"""Mneme — unified setup wizard (one wizard, every backend).

Walks the user through ONE flow that configures the whole system:
  1. Backend   → OpenRouter (hosted) or Ollama (local)
  2. Models    → chat / embedder / labeler (per backend)
  3. Pi        → optional terminal assistant (or use the built-in chat / any client)
  4. Port      → the HTTP port the proxy listens on

Then writes ONE config file (mneme.yaml), a start script, and launches the proxy.

Run it after the installer:
  curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/mneme_setup.py && python3 /tmp/setup.py

Self-contained (stdlib only) so it runs via curl | python3 with no pip installs.
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
import re

# ── Defaults ─────────────────────────────────────────────────────
DEFAULT_PORT = 8080
MEMORY_DIR = os.environ.get("MNEME_CHUNK_DIR", os.path.expanduser("~/mneme/chunks"))
KEY_FILE = os.environ.get("MNEME_KEY_FILE", os.path.expanduser("~/mneme/env"))
_REPO_DERIVED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = None

# OpenRouter defaults (hosted)
OR_BASE = "https://openrouter.ai/api/v1"
OR_DEFAULT_MAIN = "deepseek/deepseek-v4-flash"
OR_DEFAULT_EMBED = "voyageai/voyage-4-lite"          # 1024-dim
OR_DEFAULT_LABEL = "meta-llama/llama-3.2-3b-instruct"  # non-thinking

# Ollama defaults (local)
OL_DEFAULT_EMBED = "snowflake-arctic-embed2"   # 1024-dim
OL_DEFAULT_LABEL = "qwen2.5:1.5b"              # small non-thinking labeler (better labels than 0.5b)


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
                return idx
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

  Conversational memory proxy — setup wizard
""")


def detect_branch(repo_root):
    """Which repo branch is installed? Drives the memory-only default and the Pi
    extension download URL. Prefers the git branch of the cloned repo; falls back
    to MNEME_BRANCH (set by the README's install command), then unified_mneme."""
    if repo_root and os.path.isdir(os.path.join(repo_root, ".git")):
        r = run(f"git -C {repo_root} rev-parse --abbrev-ref HEAD", timeout=10)
        b = (r.stdout or "").strip()
        if b and b != "HEAD":  # detached HEAD (tarball install) -> fall back
            return b
    return os.environ.get("MNEME_BRANCH", "unified_mneme")


def find_repo():
    """Locate the repo (needs proxy/mneme_proxy.py). Tries known paths, then clones."""
    candidates = [
        _REPO_DERIVED,
        os.path.expanduser("~/mneme/repo"),
        os.getcwd(),
    ]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "proxy", "mneme_proxy.py")):
            return c
    print("\n  Mneme proxy code not found. Run the installer first:")
    print("    curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/install.sh | MNEME_BRANCH=main bash")
    print("  ...or enter the repo path below (blank to git-clone it now).")
    path = input("  Repo path [clone]: ").strip()
    if path:
        if os.path.exists(os.path.join(path, "proxy", "mneme_proxy.py")):
            return path
        print(f"  proxy/mneme_proxy.py not found in {path}.")
        sys.exit(1)
    dest = os.path.expanduser("~/mneme/repo")
    print(f"  Cloning into {dest} ...")
    _br = os.environ.get("MNEME_BRANCH", "unified_mneme")
    r = run(f"git clone --depth 1 -b {_br} https://github.com/flyersean/Mneme.git {dest}", timeout=300)
    if r.returncode != 0:
        print(f"  ✗ Clone failed: {r.stderr[:300]}")
        sys.exit(1)
    return dest


# ── OpenRouter backend ──────────────────────────────────────────
def or_get(key, path):
    req = urllib.request.Request(OR_BASE + path, headers={"Authorization": f"Bearer {key}"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return None
    except Exception:
        return None


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


def save_key(key):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    with open(KEY_FILE, "w") as f:
        f.write(f"OPENROUTER_API_KEY={key}\n")
    os.chmod(KEY_FILE, 0o600)
    print(f"  Saved key to {KEY_FILE} (chmod 600).")


def ask_and_validate_key():
    saved = load_saved_key()
    if saved:
        info = or_get(saved, "/auth/key")
        if info and "data" in info:
            print("  ✓ Saved OpenRouter key is valid — keeping it.")
            return saved
        print("  Saved key is invalid — please re-enter.")
    while True:
        print("\n\033[1mOpenRouter API key\033[0m (create one at https://openrouter.ai/keys)")
        key = getpass.getpass("  API key (input is hidden): ").strip()
        if not key:
            continue
        info = or_get(key, "/auth/key")
        if info and "data" in info:
            print("  ✓ Valid key.")
            save_key(key)
            return key
        print("  ✗ Invalid key — check it and try again.")


CTX_PRESETS = [
    ("32K", 32000),
    ("64K", 64000),
    ("128K", 128000),
    ("200K", 200000),
    ("1M (frontier — e.g. stealth/ox-alpha)", 1000000),
    ("Custom (enter any token count)", None),
]


def pick_context_window(default=64000):
    """Pick the model's context window.

    Used for both Ollama (a derived Modelfile pins num_ctx to this) and OpenRouter
    (frontier models span wildly different windows — pick one matching the MODEL's
    capability, not the proxy default, or the context-budget math will be off).
    """
    idx = choose("Context window (match the model's capability)", [c[0] for c in CTX_PRESETS])
    val = CTX_PRESETS[idx][1]
    if val is None:  # custom
        raw = ask("Context window in tokens", str(default))
        try:
            return max(2048, int(str(raw).replace(",", "").replace("_", "")))
        except ValueError:
            print(f"  → not a number; using {default}")
            return default
    return val


def _budget_parts(ctx_tokens):
    """Split the context window into (completion_reserve, tool_followup_tokens).

    Both scale with the window, so the recent-context slice (ctx - reserve - tool)
    is always the remainder and the three input consumers (memory injection +
    recent window + tool results) can never sum past the model's limit. Floored at
    2048 so tiny models still get sane slices."""
    reserve = max(2048, int(ctx_tokens) // 8)
    tool = max(2048, int(ctx_tokens) // 6)
    return reserve, tool


def setup_openrouter_models():
    """Pick main/embed/label models (OpenRouter IDs). Returns a dict."""
    print("\n\033[1mModels (all hosted on OpenRouter — nothing downloaded)\033[0m")
    main_opts = [
        ("stealth/ox-alpha           (free frontier coder, 1M ctx, reasoning)", "stealth/ox-alpha"),
        ("deepseek/deepseek-v4-flash  (cheapest thinking MoE)", "deepseek/deepseek-v4-flash"),
        ("deepseek/deepseek-chat      (V3, non-thinking)", "deepseek/deepseek-chat"),
        ("qwen/qwen3-32b              (open-weight)", "qwen/qwen3-32b"),
        ("Custom (enter any OpenRouter model id)", "__custom__"),
    ]
    idx = choose("Main model", [m[0] for m in main_opts])
    if main_opts[idx][1] == "__custom__":
        model = ask("Enter OpenRouter model id", OR_DEFAULT_MAIN) or OR_DEFAULT_MAIN
    else:
        model = main_opts[idx][1]

    embed_opts = [
        ("voyageai/voyage-4-lite  (1024-dim, recommended)", "voyageai/voyage-4-lite"),
        ("Custom (WARNING: must output 1024-dim)", "__custom__"),
    ]
    idx = choose("Embedder (memory vectors)", [m[0] for m in embed_opts])
    if embed_opts[idx][1] == "__custom__":
        embed_model = ask("Enter embedder model id (1024-dim)", OR_DEFAULT_EMBED) or OR_DEFAULT_EMBED
    else:
        embed_model = embed_opts[idx][1]

    label_opts = [
        ("meta-llama/llama-3.2-3b-instruct  (small, non-thinking — recommended)", "meta-llama/llama-3.2-3b-instruct"),
        ("Custom (WARNING: must be NON-thinking)", "__custom__"),
    ]
    idx = choose("Labeler (topic labels)", [m[0] for m in label_opts])
    if label_opts[idx][1] == "__custom__":
        label_model = ask("Enter labeler model id (non-thinking)", OR_DEFAULT_LABEL) or OR_DEFAULT_LABEL
    else:
        label_model = label_opts[idx][1]

    ctx_size = pick_context_window()
    return {"model": model, "embed_model": embed_model, "label_model": label_model, "ctx_size": ctx_size}


# ── Ollama backend ──────────────────────────────────────────────
def ensure_ollama():
    """Make sure ollama is installed and serving. Returns True on success."""
    if not shutil.which("ollama"):
        print("  Ollama not found — installing...")
        run("curl -fsSL https://ollama.com/install.sh | sh", timeout=300)
    if not shutil.which("ollama"):
        print("  ✗ Ollama install failed. Run: curl -fsSL https://ollama.com/install.sh | sh")
        return False
    if run("curl -s --max-time 2 http://localhost:11434 >/dev/null", timeout=5).returncode != 0:
        print("  Starting ollama serve...")
        # Mirror the install.sh systemd drop-in here: this fallback runs when Ollama
        # is NOT under systemd (e.g. RunPod images), so without this the serve
        # process inherits none of the OLLAMA_* settings. keep_alive=-1 keeps models
        # resident; flash_attention=0 avoids a CUDA crash on some vision-patched
        # models; sched_spread=1 spreads models across ALL GPUs instead of packing
        # them onto GPU 0 (the second A40 would otherwise sit idle at 0%).
        _env = os.environ.copy()
        _env["OLLAMA_KEEP_ALIVE"] = "-1"
        _env["OLLAMA_FLASH_ATTENTION"] = "0"
        _env["OLLAMA_SCHED_SPREAD"] = "1"
        subprocess.Popen(["ollama", "serve"], env=_env, stdout=open("/tmp/ollama.log", "ab"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(20):
            if run("curl -s --max-time 2 http://localhost:11434 >/dev/null", timeout=5).returncode == 0:
                break
            time.sleep(1)
    return True


def get_pulled_models():
    out = run("ollama list", timeout=10).stdout
    models = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or "NAME" in line:
            continue
        models.append(parts[0])
    return models


def pull_model(name):
    if name in get_pulled_models():
        print(f"  {name} already pulled — skipping.")
        return
    print(f"  Pulling {name}...")
    r = run(f"ollama pull {name}", timeout=900)
    if r.returncode != 0:
        print(f"  ⚠ could not pull {name} (may be a typo or network) — continuing")


def _menu(prompt, entries):
    """entries: list of (label, value). Entries with value=None are non-selectable
    section headers, printed without a number. Only real options get numbered.
    Returns the chosen value."""
    print(f"\n{prompt}")
    selectable = []
    n = 0
    for label, value in entries:
        if value is None:
            print(f"  {label}")
        else:
            n += 1
            selectable.append(value)
            print(f"  {n}. {label}")
    while True:
        try:
            val = input(f"Choice (1-{n}): ").strip()
        except EOFError:
            print()
            sys.exit(1)
        try:
            i = int(val)
            if 1 <= i <= n:
                return selectable[i - 1]
        except ValueError:
            pass
        print(f"  Enter 1-{n}")


def setup_ollama_models():
    """Pick chat/embed/label models (Ollama names), pulling if needed. Returns a dict."""
    ensure_ollama()
    pulled = get_pulled_models()

    print("\n\033[1mModels (local Ollama — pulled to this machine)\033[0m")
    entries = []
    if pulled:
        entries.append(("── Already pulled ──", None))
        for p in pulled:
            entries.append((f"{p}  (pulled)", p))
    entries.append(("── Pull a recommended model ──", None))
    recommended = [
        ("qwen3:32b  (strong general model)", "qwen3:32b"),
        ("qwen3:14b  (lighter)", "qwen3:14b"),
        ("llama3.1:8b  (small)", "llama3.1:8b"),
    ]
    entries.extend(recommended)
    entries.append(("Custom (enter any Ollama model name)", "__custom__"))

    model = _menu("Main (chat) model", entries)
    if model == "__custom__":
        model = ask("Enter Ollama model name") or "qwen3:32b"
    pull_model(model)

    embed_model = ask("Embedder model (1024-dim)", OL_DEFAULT_EMBED) or OL_DEFAULT_EMBED
    pull_model(embed_model)
    label_model = ask("Labeler model (non-thinking)", OL_DEFAULT_LABEL) or OL_DEFAULT_LABEL
    pull_model(label_model)

    ctx_size = pick_context_window()

    # Pin the context window via a derived Modelfile so the model actually loads
    # with num_ctx == ctx_size (matching sampling.ctx_tokens in the config).
    # Without this, Ollama loads the model's default context and silently
    # truncates when the proxy sends a longer prompt. The base model is already
    # quantized (Ollama library models ship Q4_K_M by default).
    model = create_context_modelfile(model, ctx_size)

    return {"model": model, "embed_model": embed_model, "label_model": label_model, "ctx_size": ctx_size}


def _derived_model_name(base_model, ctx_size):
    """Deterministic derived-model name keyed on the base model + context window
    (NOT the port). Instances sharing a base model + context then share one
    derived name, so Ollama keeps a single resident copy of the weights instead
    of one per port. ':' '/' '.' are sanitized to '-' for a valid name."""
    frag = re.sub(r"[^a-zA-Z0-9]+", "-", base_model).strip("-").lower()
    return f"mneme-chat-{frag}-{int(ctx_size) // 1000}k"


def create_context_modelfile(base_model, ctx_size, name=None):
    """Create a derived Ollama model with an explicit context window, so num_ctx
    matches what the proxy expects. `name` is auto-derived from the base model +
    context window when omitted, so instances pointing at the SAME base + context
    collapse onto one derived model (one resident copy in VRAM). Returns the
    derived model name on success, or the base model name if `ollama create`
    fails."""
    if not shutil.which("ollama"):
        return base_model
    if name is None:
        name = _derived_model_name(base_model, ctx_size)
    mf = os.path.join(MEMORY_DIR, f"Modelfile.{name}")
    content = (
        f"# Mneme — derived from {base_model} (quantized base)\n"
        f"# Pins the context window to match sampling.ctx_tokens in mneme.yaml.\n"
        f"FROM {base_model}\n"
        f"PARAMETER num_ctx {ctx_size}\n"
    )
    try:
        with open(mf, "w") as f:
            f.write(content)
        r = run(f"ollama create {name} -f {mf}", timeout=300)
        if r.returncode == 0:
            print(f"  ✓ created '{name}' (num_ctx={ctx_size}) from {base_model}")
            return name
    except Exception:
        pass
    print(f"  ⚠ could not create Modelfile — using {base_model} as-is")
    return base_model


# ── Pi (optional terminal assistant) ────────────────────────────
def setup_pi(ctx_size, branch="unified_mneme"):
    """Install Pi + write its provider config pointing at this proxy. Returns True on success."""
    if not shutil.which("node"):
        print("  Node.js not found — installing Node 22...")
        run("curl -fsSL https://deb.nodesource.com/setup_22.x | bash -", timeout=60)
        run("apt-get install -y nodejs", timeout=120)
    if not shutil.which("npm"):
        print("  ✗ npm not found — Pi install skipped.")
        return False
    print("  Installing Pi (terminal AI coding assistant)...")
    run("npm install -g @earendil-works/pi-coding-agent", timeout=180)

    pi_config = {
        "providers": {
            "mneme": {
                "baseUrl": "http://localhost:8080/v1",
                "api": "openai-completions",
                "apiKey": "none",
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [{"id": "text-mneme:64k", "name": "Mneme", "contextWindow": ctx_size or 32000, "reasoning": False}],
            }
        }
    }
    os.makedirs(os.path.expanduser("~/.pi/agent"), exist_ok=True)
    with open(os.path.expanduser("~/.pi/agent/models.json"), "w") as f:
        json.dump(pi_config, f, indent=2)

    # Download the Pi extensions (fresh, cache-busted).
    for name, fname in [("search_memory", "mneme-search-tool.ts"), ("web tools", "mneme-web-tools.ts")]:
        url = f"https://raw.githubusercontent.com/flyersean/Mneme/{branch}/extensions/pi/{fname}"
        r = run(f"curl -sSL --fail -o {os.path.expanduser('~/' + fname)} '{url}?{int(time.time())}'")
        print(f"    {'✓' if r.returncode == 0 else '⚠'} {name} extension")

    print("  ✓ Pi configured → run with:")
    print("      pi --provider mneme --model text-mneme:64k --extension ~/mneme-search-tool.ts --extension ~/mneme-web-tools.ts")
    return True


# ── Config + start script ───────────────────────────────────────
def _instance_dir(db_dir, port):
    """Per-instance config dir. Each proxy instance owns its own config + prompts
    (chunk_dir) while the memory DB lives at db_dir (shared, portable)."""
    return os.path.join(db_dir, "instances", str(port))


def _common_yaml(instance_dir, db_path, port, inject, ctx_tokens, memory_only):
    reserve, tool = _budget_parts(ctx_tokens)
    return f"""# Mneme proxy config — generated by the unified setup wizard.
# Precedence: environment variable > this file > built-in default.
# Full reference: docs/config-spec.md in the repo.

backend:
  type: @@BTYPE@@
  provider: @@BPROV@@
  ollama_url: http://localhost:11434

providers:
  openrouter:
    base_url: {OR_BASE}
    api_key_env: OPENROUTER_API_KEY
    model: "@@MAIN@@"
    embed_model: "@@EMBED@@"
    label_model: "@@LABEL@@"

sampling:
  temperature: 0.2
  top_p: 0.9
  ctx_tokens: {ctx_tokens}
  max_tokens: 65536
  completion_reserve: {reserve}   # reply reserve — scales with ctx (ctx/8)
  # Reasoning/thinking is OFF by default (a reasoning model can runaway-think on
  # a trivial ask). Set reasoning_enabled: 1 to opt back in; reasoning_effort
  # (low/high/max) is for effort-level models (deepseek etc.), not Qwen3.6.
  reasoning_enabled: 0

timeouts:
  chat_timeout: 300
  ollama_chat_timeout: 300
  first_token_timeout: 120
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30

storage:
  chunk_dir: "{instance_dir}"
  db_path: "{db_path}"
  port: {port}
  inject_system: {inject}
  memory_only: {memory_only}
  staging_turns: 1   # swarm default: flush to memory after every turn
  staging_idle: 120
  belief_evolution: false

retrieval:
  max_injected_tokens: 8000
  # inject_min_similarity is EMBEDDER-DEPENDENT: every embedding model has its
  # own similarity scale, so tune this to YOUR embedder (see mneme.yaml.example).
  #   voyage-4-lite: ~0.62   snowflake-arctic-embed2: ~0.45
  inject_min_similarity: 0.45
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
  tool_followup_tokens: {tool}    # tool-results slice — scales with ctx (ctx/6)
  chunk_size: 4000
"""


def write_config(backend, models, port, inject, memory_only, instance_dir, db_path):
    """Write mneme.yaml for the chosen backend into this instance's config dir."""
    os.makedirs(instance_dir, exist_ok=True)
    inject_s = "true" if str(inject) == "1" else "false"
    mo_s = "true" if memory_only else "false"
    if backend == "openrouter":
        btype, bprov = "openai", "openrouter"
    else:
        btype, bprov = "ollama", ""
    yaml = _common_yaml(instance_dir, db_path, port, inject_s, models.get("ctx_size", 64000), mo_s)
    yaml = (yaml.replace("@@BTYPE@@", btype).replace("@@BPROV@@", bprov)
            .replace("@@MAIN@@", models.get("model", ""))
            .replace("@@EMBED@@", models.get("embed_model", ""))
            .replace("@@LABEL@@", models.get("label_model", "")))
    path = os.path.join(instance_dir, "mneme.yaml")
    with open(path, "w") as f:
        f.write(yaml)
    return path


def write_start_script(backend, models, port, instance_dir):
    """Write a start script into this instance's config dir."""
    os.makedirs(instance_dir, exist_ok=True)
    path = os.path.join(instance_dir, "start_proxy.sh")
    lines = [
        "#!/bin/bash",
        "# Mneme proxy — generated by the unified setup wizard.",
        f"# Start/restart with:  {path}",
        "",
    ]
    if backend == "openrouter":
        lines += [
            "# Source the saved OpenRouter key unless one is already exported",
            f'if [ -z "${{OPENROUTER_API_KEY:-}}" ] && [ -f "{KEY_FILE}" ]; then',
            f'  export $(grep -v "^#" "{KEY_FILE}" | xargs)',
            "fi",
            'export MNEME_BACKEND="openrouter"',
        ]
    else:
        lines += [
            'export MNEME_BACKEND="ollama"',
            f'export MNEME_MODEL="{models.get("model", "")}"',
            f'export EMBED_MODEL="{models.get("embed_model", "")}"',
            f'export LABEL_MODEL="{models.get("label_model", "")}"',
        ]
    lines += [
        f'export MNEME_CHUNK_DIR="{instance_dir}"',
        f'export MNEME_PORT="{port}"',
        "export PYTHONDONTWRITEBYTECODE=1",
        "",
        f'cd "{REPO_ROOT}"',
        "exec python3 -uB proxy/mneme_proxy.py",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))
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


def _pid_on_port(port):
    """PID of the process listening on `port`, or None. Reads `ss -ltnp` (present
    on standard Linux images, incl. RunPod)."""
    try:
        import re
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            m = re.search(rf":{port}\s.*pid=(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def stop_proxy_on_port(port):
    """Stop a running Mneme proxy on `port` (best-effort). Returns True if one was
    found and killed. SIGTERM first, SIGKILL if it doesn't exit within 5s."""
    pid = _pid_on_port(port)
    if not pid:
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
        for _ in range(10):
            time.sleep(0.5)
            if _pid_on_port(port) is None:
                return True
        os.kill(pid, 9)  # SIGKILL
        return True
    except Exception:
        return False


def start_proxy(backend, models, port, instance_dir):
    env = os.environ.copy()
    env["MNEME_CHUNK_DIR"] = instance_dir
    env["MNEME_PORT"] = str(port)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if backend == "openrouter":
        env["OPENROUTER_API_KEY"] = load_saved_key()
        env["MNEME_BACKEND"] = "openrouter"
    else:
        # Ollama models are read from env vars (not the config), so the choices
        # the user just made MUST be exported here — otherwise the proxy falls
        # back to its code defaults (e.g. label=qwen2.5:1.5b) and silently uses
        # models the user never picked. This was the "labeler 404" bug.
        env["MNEME_BACKEND"] = "ollama"
        env["MNEME_MODEL"] = models.get("model", "")
        env["EMBED_MODEL"] = models.get("embed_model", "")
        env["LABEL_MODEL"] = models.get("label_model", "")
    log = open("/tmp/mneme.log", "w")
    subprocess.Popen([sys.executable, "-uB", "proxy/mneme_proxy.py"],
                     cwd=REPO_ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    print(f"  Starting proxy on port {port}...", end=" ", flush=True)
    for _ in range(30):
        time.sleep(1)
        try:
            d = json.loads(urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3).read())
            print(f"running ({d.get('chunks', 0)} chunks, backend={d.get('backend')})")
            return True
        except Exception:
            continue
    print("timeout — check /tmp/mneme.log")
    return False


# ── Multi-instance (shared DB) ──────────────────────────────────
# Multiple proxy instances share ONE memory DB dir. The FIRST setup writes the
# shared config (mneme.yaml) + saves the shared settings to setup_config.json so
# a later "add instance" can lock the embedder/labeler (the vectors in one DB
# must all come from the SAME embedder or similarity is meaningless). Each added
# instance gets its own start script that overrides only the chat model + port.

def _count_chunks(memory_dir):
    try:
        import sqlite3
        c = sqlite3.connect(os.path.join(memory_dir, "mneme.db"))
        n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        c.close()
        return n
    except Exception:
        return "?"


def _scfg_path(memory_dir):
    return os.path.join(memory_dir, "setup_config.json")


def load_shared_config(memory_dir):
    """Read the shared-instance metadata saved by the first setup. {} when absent."""
    p = _scfg_path(memory_dir)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_shared_config(memory_dir, models, backend, port=None, inject=None, memory_only=None):
    """Persist the shared settings (embedder/labeler + their backends) so a later
    'add instance' locks them to this DB's original choice, and 'reconfigure' can
    recover the original port + injection settings."""
    data = {
        "db_dir": memory_dir,
        "backend": backend,
        "embed_model": models.get("embed_model", ""),
        "embed_backend": backend,
        "label_model": models.get("label_model", ""),
        "label_backend": backend,
    }
    if port is not None:
        data["port"] = int(port)
    if inject is not None:
        data["inject"] = inject
    if memory_only is not None:
        data["memory_only"] = bool(memory_only)
    with open(_scfg_path(memory_dir), "w") as f:
        json.dump(data, f, indent=2)


def db_exists(memory_dir):
    return os.path.exists(os.path.join(memory_dir, "mneme.db"))


def wipe_db(memory_dir):
    """Delete the memory DB, FAISS index, and generated config/start files so a
    'new install' starts from zero. Returns the paths removed. Leaves anything it
    doesn't recognize untouched."""
    removed = []
    for name in ("mneme.db", "mneme.db-wal", "mneme.db-shm",
                 "faiss.index", "faiss.idmap", "faiss.lock",
                 "mneme.yaml", "setup_config.json"):
        p = os.path.join(memory_dir, name)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    # per-instance start scripts (start_proxy.sh, start_proxy_8081.sh, …)
    try:
        for name in sorted(os.listdir(memory_dir)):
            if name.startswith("start_proxy") and name.endswith(".sh"):
                p = os.path.join(memory_dir, name)
                try:
                    os.remove(p)
                    removed.append(p)
                except OSError:
                    pass
    except OSError:
        pass
    # instruction overrides (re-materialized fresh from code defaults on next start)
    inst = os.path.join(memory_dir, "instructions")
    if os.path.isdir(inst):
        shutil.rmtree(inst, ignore_errors=True)
        removed.append(inst)
    # per-instance config dirs (instances/<port>/)
    inst_root = os.path.join(memory_dir, "instances")
    if os.path.isdir(inst_root):
        shutil.rmtree(inst_root, ignore_errors=True)
        removed.append(inst_root)
    return removed


def pick_chat_model(chat_backend):
    """Pick (and pull, for Ollama) the chat model AND its context window for an
    added instance. Returns (model_name, ctx_size). The embedder/labeler are
    locked to the shared DB and are NOT asked here."""
    if chat_backend == "openrouter":
        main_opts = [
            ("stealth/ox-alpha           (free frontier coder, 1M ctx, reasoning)", "stealth/ox-alpha"),
            ("deepseek/deepseek-v4-flash  (cheapest thinking MoE)", "deepseek/deepseek-v4-flash"),
            ("deepseek/deepseek-chat      (V3, non-thinking)", "deepseek/deepseek-chat"),
            ("qwen/qwen3-32b              (open-weight)", "qwen/qwen3-32b"),
            ("Custom (enter any OpenRouter model id)", "__custom__"),
        ]
        idx = choose("Chat model", [m[0] for m in main_opts])
        if main_opts[idx][1] == "__custom__":
            model = ask("Enter OpenRouter model id", OR_DEFAULT_MAIN) or OR_DEFAULT_MAIN
        else:
            model = main_opts[idx][1]
        return model, pick_context_window()

    ensure_ollama()
    pulled = get_pulled_models()
    entries = []
    if pulled:
        entries.append(("── Already pulled ──", None))
        for p in pulled:
            entries.append((f"{p}  (pulled)", p))
    entries.append(("── Pull a recommended model ──", None))
    entries += [
        ("qwen3:32b  (strong general model)", "qwen3:32b"),
        ("qwen3:14b  (lighter)", "qwen3:14b"),
        ("llama3.1:8b  (small)", "llama3.1:8b"),
    ]
    entries.append(("Custom (enter any Ollama model name)", "__custom__"))
    model = _menu("Chat model", entries)
    if model == "__custom__":
        model = ask("Enter Ollama model name") or "qwen3:32b"
    pull_model(model)
    ctx_size = pick_context_window()
    return create_context_modelfile(model, ctx_size), ctx_size


def write_instance_start_script(instance_dir, db_dir, port, chat_backend, chat_model,
                                embed_model, embed_backend, label_model, label_backend,
                                inject, memory_only):
    """Write a per-instance start script: overrides the shared config with this
    instance's chat model + port, points at the shared DB, and reuses the locked
    embedder/labeler (keeping their original backends via MNEME_*_BACKEND)."""
    os.makedirs(instance_dir, exist_ok=True)
    path = os.path.join(instance_dir, f"start_proxy_{port}.sh")
    lines = [
        "#!/bin/bash",
        f"# Mneme proxy instance — chat model: {chat_model} (port {port})",
        f"# Shares the memory DB with other instances at: {db_dir}",
        f"# Start/restart with:  {path}",
        "",
    ]
    if chat_backend == "openrouter":
        lines += [
            "# Source the saved OpenRouter key unless one is already exported",
            f'if [ -z "${{OPENROUTER_API_KEY:-}}" ] && [ -f "{KEY_FILE}" ]; then',
            f'  export $(grep -v "^#" "{KEY_FILE}" | xargs)',
            "fi",
        ]
    lines += [
        f'export MNEME_BACKEND="{chat_backend}"',
        f'export MNEME_MODEL="{chat_model}"',
        f'export EMBED_MODEL="{embed_model}"',
        f'export LABEL_MODEL="{label_model}"',
    ]
    # Aux backends: only set when they differ from this instance's chat backend,
    # so the embedder/labeler keep running where the DB originally set them up.
    if embed_backend and embed_backend != chat_backend:
        lines.append(f'export MNEME_EMBED_BACKEND="{embed_backend}"')
    if label_backend and label_backend != chat_backend:
        lines.append(f'export MNEME_LABEL_BACKEND="{label_backend}"')
    lines += [
        f'export MNEME_CHUNK_DIR="{instance_dir}"',
        f'export MNEME_PORT="{port}"',
        f'export MNEME_MEMORY_ONLY="{"1" if memory_only else "0"}"',
        f'export MNEME_INJECT_SYSTEM="{inject}"',
        "export PYTHONDONTWRITEBYTECODE=1",
        "",
        f'cd "{REPO_ROOT}"',
        "exec python3 -uB proxy/mneme_proxy.py",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path


def start_instance(instance_dir, port, chat_backend, chat_model,
                   embed_model, embed_backend, label_model, label_backend,
                   inject, memory_only):
    """Launch an added instance and wait for its health check."""
    env = os.environ.copy()
    env["MNEME_CHUNK_DIR"] = instance_dir
    env["MNEME_PORT"] = str(port)
    env["MNEME_BACKEND"] = chat_backend
    env["MNEME_MODEL"] = chat_model
    env["EMBED_MODEL"] = embed_model
    env["LABEL_MODEL"] = label_model
    if embed_backend and embed_backend != chat_backend:
        env["MNEME_EMBED_BACKEND"] = embed_backend
    if label_backend and label_backend != chat_backend:
        env["MNEME_LABEL_BACKEND"] = label_backend
    env["MNEME_MEMORY_ONLY"] = "1" if memory_only else "0"
    env["MNEME_INJECT_SYSTEM"] = inject
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if chat_backend == "openrouter":
        env["OPENROUTER_API_KEY"] = load_saved_key()
    log = open(f"/tmp/mneme_{port}.log", "w")
    subprocess.Popen([sys.executable, "-uB", "proxy/mneme_proxy.py"],
                     cwd=REPO_ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
    print(f"  Starting proxy on port {port}...", end=" ", flush=True)
    for _ in range(30):
        time.sleep(1)
        try:
            d = json.loads(urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3).read())
            print(f"running ({d.get('chunks', 0)} chunks, backend={d.get('backend')})")
            return True
        except Exception:
            continue
    print(f"timeout — check /tmp/mneme_{port}.log")
    return False


def _add_instance(memory_dir, shared, memory_only):
    """Add a new proxy instance to an existing shared DB."""
    print("\n\033[1mAdd a proxy instance to the existing DB\033[0m")
    print(f"  Shared DB:  {memory_dir}")
    embed_model = shared.get("embed_model") or OL_DEFAULT_EMBED
    embed_backend = shared.get("embed_backend") or "ollama"
    label_model = shared.get("label_model") or OL_DEFAULT_LABEL
    label_backend = shared.get("label_backend") or "ollama"
    print(f"  Embedder (locked): {embed_model}  ({embed_backend})")
    print(f"  Labeler  (locked): {label_model}  ({label_backend})")

    idx = choose("Chat backend for this instance?", [
        "OpenRouter (hosted — needs an API key)",
        "Ollama (local — models run on this machine)",
    ])
    chat_backend = "openrouter" if idx == 0 else "ollama"

    port = int(ask("Port for this instance", str(free_port(DEFAULT_PORT))) or DEFAULT_PORT)
    instance_dir = _instance_dir(memory_dir, port)
    db_path = os.path.join(memory_dir, "mneme.db")

    if chat_backend == "openrouter":
        ask_and_validate_key()
    chat_model, ctx_size = pick_chat_model(chat_backend)

    idx = choose("Inject Mneme's system instructions?", [
        "Yes (default — inject the memory instructions + toolset prompt)",
        "No (skip — use a merged prompt from your own harness)",
    ])
    inject = "1" if idx == 0 else "0"

    # Per-instance config: this instance's own settings + the shared DB path.
    instance_models = {
        "model": chat_model,
        "embed_model": embed_model,
        "label_model": label_model,
        "ctx_size": ctx_size,
    }
    cfg = write_config(chat_backend, instance_models, port, inject, memory_only,
                       instance_dir, db_path)

    script = write_instance_start_script(instance_dir, memory_dir, port, chat_backend, chat_model,
                                         embed_model, embed_backend, label_model, label_backend,
                                         inject, memory_only)
    started = start_instance(instance_dir, port, chat_backend, chat_model,
                             embed_model, embed_backend, label_model, label_backend,
                             inject, memory_only)
    create_access_symlinks()

    print("\n\033[1mInstance added.\033[0m")
    print(f"  Chat model:  {chat_model}  (backend {chat_backend})")
    print(f"  Port:        {port}")
    print(f"  Config:      {cfg}")
    print(f"  Start:       {script}")
    print(f"  Log:         /tmp/mneme_{port}.log")
    print(f"  Shared DB:   {memory_dir}")
    print(f"  Chat UI:     http://localhost:{port}/")
    return 0 if started else 1


def create_access_symlinks():
    """Expose the memory dir + repo to JupyterLab's file browser via /workspace
    symlinks, so a user can browse and download the DB, config, and prompts from
    the GUI (RunPod's JupyterLab is rooted at /workspace, which ~/mneme isn't
    under, and /root is mode 700 so the GUI can't climb to it). No-op off-pod."""
    if not os.path.isdir("/workspace"):
        return
    links = {
        "/workspace/mneme-chunks": MEMORY_DIR,
        "/workspace/mneme-repo": REPO_ROOT,
    }
    made = []
    for link, target in links.items():
        try:
            if os.path.islink(link):
                os.remove(link)
            elif os.path.exists(link):
                continue  # a real file/dir is there; don't clobber it
            os.symlink(target, link)
            made.append(link)
        except Exception:
            pass
    if made:
        print("\n  JupyterLab shortcuts (browse/download your DB):")
        for link in made:
            print(f"    {link}  →  {links[link]}")


# ── Main ─────────────────────────────────────────────────────────
def main():
    global REPO_ROOT, MEMORY_DIR
    banner()
    REPO_ROOT = find_repo()
    print(f"  Repo: {REPO_ROOT}")
    branch = detect_branch(REPO_ROOT)
    memory_only = (branch == "main")
    if memory_only:
        print(f"  Branch: {branch} → memory-only build (strategy/learning layer off)")
    else:
        print(f"  Branch: {branch} → full build (strategy/learning layer on)")

    # 0. Memory DB location — shared by every instance of this Mneme install.
    #    Point it at a mounted shared volume to let instances on OTHER machines
    #    use the same memory (same-machine multi-instance needs no special setup).
    print("\n\033[1mStep 0/4 — Memory DB location\033[0m")
    _default_db = os.path.abspath(os.environ.get("MNEME_CHUNK_DIR") or os.path.expanduser("~/mneme/chunks"))
    # Normalize to an ABSOLUTE path. The proxy resolves a relative chunk_dir against
    # ITS OWN cwd (the repo), which differs from where setup runs — so "./workspace/chunks"
    # silently lands in /root/mneme/repo/workspace/chunks (ephemeral) instead of
    # /workspace/chunks (persistent). abspath() makes the stored path deterministic
    # no matter where setup or the proxy runs from.
    _raw = ask("Memory DB directory (shared by all instances)", _default_db) or _default_db
    MEMORY_DIR = os.path.abspath(os.path.expanduser(_raw))
    if MEMORY_DIR != _raw:
        print(f"  → resolved to: {MEMORY_DIR}")
    if os.path.isdir("/workspace") and not (MEMORY_DIR == "/workspace" or MEMORY_DIR.startswith("/workspace/")):
        print("  ⚠ /workspace is the persistent volume here — a path outside it is wiped on stop/restart.")
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # Existing DB? Offer to add an instance, reconfigure, or wipe it for a fresh
    # install.
    reconf_port = None
    if db_exists(MEMORY_DIR):
        shared = load_shared_config(MEMORY_DIR)
        print(f"\n  Existing memory DB found at {MEMORY_DIR} ({_count_chunks(MEMORY_DIR)} chunks).")
        idx = choose("What would you like to do?", [
            "Add another proxy instance (new chat model + port, sharing this DB)",
            "Reconfigure this install (re-pick backend / models / port — keeps the DB)",
            "New install (wipe this DB and start fresh with all new settings)",
        ])
        if idx == 0:
            return _add_instance(MEMORY_DIR, shared, memory_only)
        if idx == 2:
            # New install: stop any running instance, wipe the DB, then fall
            # through to the fresh setup below (reconf_port stays None so the
            # fresh defaults — port 8080 etc. — apply).
            ans = ask("Wipe the existing memory DB and start fresh? This permanently deletes ALL saved memory. [y/N]", "N").strip().lower()
            if ans not in ("y", "yes"):
                print("  Cancelled — keeping the existing install.")
                return 0
            _old_port = shared.get("port")
            if _old_port and stop_proxy_on_port(_old_port):
                print(f"  Stopped old instance on port {_old_port}.")
                time.sleep(1)
            wiped = wipe_db(MEMORY_DIR)
            print(f"  Wiped {len(wiped)} file(s). Starting a fresh install...")
        else:
            # Reconfigure: reuse the SAVED port as the default (not the next free
            # port) and stop the old instance there so it's a true stop-and-restart,
            # not a duplicate on a new port.
            reconf_port = shared.get("port")
            if reconf_port:
                print(f"  Reconfiguring — reusing port {reconf_port} (old instance there will be stopped).")

    # 1. Backend
    print("\n\033[1mStep 1/4 — Backend\033[0m")
    idx = choose("Which backend?", [
        "OpenRouter (hosted — no GPU, no downloads; needs an API key)",
        "Ollama (local — private, free; models run on this machine)",
    ])
    backend = "openrouter" if idx == 0 else "ollama"

    # 2. Models
    print("\n\033[1mStep 2/4 — Models\033[0m")
    if backend == "openrouter":
        ask_and_validate_key()
        models = setup_openrouter_models()
    else:
        models = setup_ollama_models()

    # 3. Pi (optional)
    print("\n\033[1mStep 3/4 — Chat interface\033[0m")
    print("  Pi is a lightweight terminal AI assistant. If you say no, you can still:")
    print("    • use the built-in chat page at http://localhost:8080/")
    print("    • connect any OpenAI-compatible client to http://localhost:8080/v1")
    idx = choose("Install Pi?", ["No — proxy only (built-in chat / my own client)", "Yes — install Pi"])
    install_pi = (idx == 1)

    # 4. Port + injection
    print("\n\033[1mStep 4/4 — Port & instructions\033[0m")
    port = int(ask("Proxy port", str(reconf_port or free_port(DEFAULT_PORT))) or (reconf_port or DEFAULT_PORT))
    idx = choose("Inject Mneme's system instructions?", [
        "Yes (default — inject the memory instructions + toolset prompt)",
        "No (skip — use a merged prompt from your own harness)",
    ])
    inject = "1" if idx == 0 else "0"

    # Per-instance config dir + shared DB path.
    instance_dir = _instance_dir(MEMORY_DIR, port)
    db_path = os.path.join(MEMORY_DIR, "mneme.db")

    # Write config + start script, then launch.
    cfg_path = write_config(backend, models, port, inject, memory_only, instance_dir, db_path)
    save_shared_config(MEMORY_DIR, models, backend, port=port, inject=inject, memory_only=memory_only)
    start_script = write_start_script(backend, models, port, instance_dir)
    print(f"\n  Config:      {cfg_path}")
    print(f"  Start/stop:  {start_script}")

    if install_pi:
        setup_pi(models.get("ctx_size"), branch)

    # Reconfigure: stop the old instance on its saved port before starting the
    # new one (a restart, not a second instance).
    if reconf_port is not None:
        if stop_proxy_on_port(reconf_port):
            print(f"  Stopped old instance on port {reconf_port}.")
            time.sleep(1)

    started = start_proxy(backend, models, port, instance_dir)
    create_access_symlinks()

    # Wrap up
    print("\n\033[1mSetup complete.\033[0m")
    print(f"  Backend:    {backend}")
    print(f"  Memory DB:  {MEMORY_DIR}")
    print(f"  Config:     {instance_dir}")
    print(f"  Start/stop: {start_script}")
    print(f"  Log:       /tmp/mneme.log")
    print("\n  Chat UI:        http://localhost:%d/" % port)
    print("  Prompt editor:  http://localhost:%d/instructions" % port)
    print("  OpenAI API:     http://localhost:%d/v1" % port)
    print("  Health:         http://localhost:%d/health" % port)
    if backend == "openrouter":
        print("\n  The API key lives only in ~/mneme/env (chmod 600).")
    print()
    return 0 if started else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Cancelled.")
        sys.exit(130)

#!/usr/bin/env python3
"""Mneme — unified setup wizard (one wizard, every backend).

Walks the user through ONE flow that configures the whole system:
  1. Backend   → OpenRouter (hosted) or Ollama (local)
  2. Models    → chat / embedder / labeler (per backend)
  3. Pi        → optional terminal assistant (or use the built-in chat / any client)
  4. Port      → the HTTP port the proxy listens on

Then writes ONE config file (mneme.yaml), a start script, and launches the proxy.

Run it after the installer:
  curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_setup.py && python3 /tmp/setup.py

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
OL_DEFAULT_LABEL = "qwen2.5:0.5b"              # tiny non-thinking labeler


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
    print("    curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/install.sh | bash")
    print("  ...or enter the repo path below (blank to git-clone it now).")
    path = input("  Repo path [clone]: ").strip()
    if path:
        if os.path.exists(os.path.join(path, "proxy", "mneme_proxy.py")):
            return path
        print(f"  proxy/mneme_proxy.py not found in {path}.")
        sys.exit(1)
    dest = os.path.expanduser("~/mneme/repo")
    print(f"  Cloning into {dest} ...")
    r = run(f"git clone --depth 1 -b unified_mneme https://github.com/flyersean/Mneme.git {dest}", timeout=300)
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

    return {"model": model, "embed_model": embed_model, "label_model": label_model}


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
        subprocess.Popen(["ollama", "serve"], stdout=open("/tmp/ollama.log", "ab"),
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

    ctx_opts = [("32K (fast)", 32000), ("64K", 64000), ("129K (needs big VRAM)", 129000)]
    idx = choose("Context window", [c[0] for c in ctx_opts])
    ctx_size = ctx_opts[idx][1]

    # Pin the context window via a derived Modelfile so the model actually loads
    # with num_ctx == ctx_size (matching sampling.ctx_tokens in the config).
    # Without this, Ollama loads the model's default context and silently
    # truncates when the proxy sends a longer prompt. The base model is already
    # quantized (Ollama library models ship Q4_K_M by default).
    model = create_context_modelfile(model, ctx_size)

    return {"model": model, "embed_model": embed_model, "label_model": label_model, "ctx_size": ctx_size}


def create_context_modelfile(base_model, ctx_size):
    """Create a derived Ollama model ('mneme-chat') with an explicit context
    window, so num_ctx matches what the proxy expects. Returns the derived model
    name on success, or the base model name if `ollama create` fails."""
    if not shutil.which("ollama"):
        return base_model
    mf = os.path.join(MEMORY_DIR, "Modelfile")
    content = (
        f"# Mneme — derived from {base_model} (quantized base)\n"
        f"# Pins the context window to match sampling.ctx_tokens in mneme.yaml.\n"
        f"FROM {base_model}\n"
        f"PARAMETER num_ctx {ctx_size}\n"
    )
    try:
        with open(mf, "w") as f:
            f.write(content)
        r = run(f"ollama create mneme-chat -f {mf}", timeout=300)
        if r.returncode == 0:
            print(f"  ✓ created 'mneme-chat' (num_ctx={ctx_size}) from {base_model}")
            return "mneme-chat"
    except Exception:
        pass
    print(f"  ⚠ could not create Modelfile — using {base_model} as-is")
    return base_model


# ── Pi (optional terminal assistant) ────────────────────────────
def setup_pi(ctx_size):
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
        url = f"https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/extensions/pi/{fname}"
        r = run(f"curl -sSL --fail -o {os.path.expanduser('~/' + fname)} '{url}?{int(time.time())}'")
        print(f"    {'✓' if r.returncode == 0 else '⚠'} {name} extension")

    print("  ✓ Pi configured → run with:")
    print("      pi --provider mneme --model text-mneme:64k --extension ~/mneme-search-tool.ts --extension ~/mneme-web-tools.ts")
    return True


# ── Config + start script ───────────────────────────────────────
def _common_yaml(memory_dir, port, inject, ctx_tokens):
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
  completion_reserve: 8192

timeouts:
  chat_timeout: 60
  ollama_chat_timeout: 120
  first_token_timeout: 30
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30

storage:
  chunk_dir: "{memory_dir}"
  port: {port}
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


def write_config(backend, models, port, inject):
    """Write mneme.yaml for the chosen backend."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    inject_s = "true" if str(inject) == "1" else "false"
    if backend == "openrouter":
        btype, bprov = "openai", "openrouter"
    else:
        btype, bprov = "ollama", ""
    yaml = _common_yaml(MEMORY_DIR, port, inject_s, models.get("ctx_size", 256000))
    yaml = (yaml.replace("@@BTYPE@@", btype).replace("@@BPROV@@", bprov)
            .replace("@@MAIN@@", models.get("model", ""))
            .replace("@@EMBED@@", models.get("embed_model", ""))
            .replace("@@LABEL@@", models.get("label_model", "")))
    path = os.path.join(MEMORY_DIR, "mneme.yaml")
    with open(path, "w") as f:
        f.write(yaml)
    return path


def write_start_script(backend, models, port):
    """Write a start script next to the DB that sources the right env for the backend."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, "start_proxy.sh")
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
        f'export MNEME_CHUNK_DIR="{MEMORY_DIR}"',
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


def start_proxy(backend, models, port):
    env = os.environ.copy()
    env["MNEME_CHUNK_DIR"] = MEMORY_DIR
    env["MNEME_PORT"] = str(port)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if backend == "openrouter":
        env["OPENROUTER_API_KEY"] = load_saved_key()
        env["MNEME_BACKEND"] = "openrouter"
    else:
        # Ollama models are read from env vars (not the config), so the choices
        # the user just made MUST be exported here — otherwise the proxy falls
        # back to its code defaults (e.g. label=qwen2.5:0.5b) and silently uses
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
    global REPO_ROOT
    banner()
    REPO_ROOT = find_repo()
    print(f"  Repo: {REPO_ROOT}")

    os.makedirs(MEMORY_DIR, exist_ok=True)

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
    port = int(ask("Proxy port", str(free_port(DEFAULT_PORT))) or DEFAULT_PORT)
    idx = choose("Inject Mneme's system instructions?", [
        "Yes (default — full memory + edge-overcome behavior)",
        "No (skip — use a merged prompt from your own harness)",
    ])
    inject = "1" if idx == 0 else "0"

    # Write config + start script, then launch.
    cfg_path = write_config(backend, models, port, inject)
    start_script = write_start_script(backend, models, port)
    print(f"\n  Config:      {cfg_path}")
    print(f"  Start/stop:  {start_script}")

    if install_pi:
        setup_pi(models.get("ctx_size"))

    started = start_proxy(backend, models, port)
    create_access_symlinks()

    # Wrap up
    print("\n\033[1mSetup complete.\033[0m")
    print(f"  Backend:   {backend}")
    print(f"  Memory DB: {MEMORY_DIR}")
    print(f"  Start/stop:{start_script}")
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

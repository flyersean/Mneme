#!/usr/bin/env python3
"""
Mneme pod setup wizard — zero external dependencies.

Interactive 5-step setup that installs Ollama, pulls models, creates a
large-context Modelfile if needed, installs Python deps, and starts the
Mneme proxy via subprocess.Popen(start_new_session=True).

Safe to run via:  python3 /tmp/mneme_setup.py < /dev/tty
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

# ─── Constants ─────────────────────────────────────────────────
WORKSPACE = "/workspace"
PROXY_DIR = os.path.join(WORKSPACE, "proxy")
PROXY_PY = os.path.join(PROXY_DIR, "mneme_proxy.py")
PROXY_PROMPT = os.path.join(PROXY_DIR, "system_prompt.md")
OLLAMA_MODELS_DIR = os.path.join(WORKSPACE, "ollama_models")
LOG = "/tmp/mneme.log"
HEALTH_URL = "http://localhost:8080/health"

REPO = "https://github.com/flyersean/Mneme.git"
BRANCH = "build-roadmap"
RAW = "https://raw.githubusercontent.com/flyersean/Mneme/build-roadmap"

EMBED_CHOICES = [
    ("snowflake-arctic-embed2:latest", "Snowflake Arctic Embed 2 (required for DB compat)"),
    ("nomic-embed-text", "Nomic Embed Text (classic, older DBs)"),
]
LABEL_CHOICES = [
    ("qwen2.5:0.5b", "Qwen2.5 0.5B (fastest, default)"),
    ("qwen2.5:1.5b", "Qwen2.5 1.5B (smarter, slower)"),
    ("qwen2.5:3b", "Qwen2.5 3B (best quality, most VRAM)"),
]
CTX_CHOICES = [
    ("32768", "32K  — plenty for Pi / small clients"),
    ("131072", "129K — recommended for Hermes (~78KB system prompt)"),
    ("264192", "264K — max for Qwen3.6 35B on A40"),
]
PIP_PACKAGES = ["flask", "flask-cors", "requests", "numpy", "faiss-cpu"]


# ─── Small helpers ─────────────────────────────────────────────
def sh(cmd, check=False, capture=False, env=None, **kw):
    """Run a shell command. Returns CompletedProcess."""
    if capture:
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
    return subprocess.run(cmd, shell=True, check=check, env=env, **kw)


def out(cmd, default=""):
    """Run command, return stripped stdout or default."""
    try:
        r = sh(cmd, capture=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else default
    except Exception:
        return default


def have(cmd):
    return shutil.which(cmd) is not None


def step(n, msg):
    print(f"\n[{n}/7] {msg}")


def ok(msg):
    print(f"  ✓ {msg}")


def warn(msg):
    print(f"  ⚠ {msg}")


def err(msg):
    print(f"  ✗ {msg}", file=sys.stderr)


def ask(prompt, options, default=1):
    """
    Numbered menu. `options` is a list of (value, label) tuples.
    Separator lines (labels starting with '─' or empty) are displayed but
    not selectable — matching is done by value, never by display index.
    Returns the value of the chosen option.
    """
    print(f"\n{prompt}")
    selectable = []
    for i, (val, label) in enumerate(options, 1):
        selectable.append(val)
        print(f"  {i}) {label}")
    while True:
        try:
            raw = input(f"Choice [1-{len(options)}] (default {default}): ").strip()
        except EOFError:
            # No TTY (Docker without -it) — use default
            print(f"(no interactive terminal — using default {default})")
            return options[default - 1][0]
        if not raw:
            return options[default - 1][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        # Also accept typing the value itself (for custom entries)
        for val, _ in options:
            if raw.lower() == str(val).lower():
                return val
        print("  Invalid choice, try again.")


def ask_text(prompt, default=""):
    try:
        raw = input(f"{prompt}" + (f" [{default}]: " if default else ": ")).strip()
    except EOFError:
        return default
    return raw or default


def confirm(prompt, default=True):
    d = "Y/n" if default else "y/N"
    try:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw.startswith("y")


# ─── Ollama server management ──────────────────────────────────
def ollama_env():
    """Env with OLLAMA_MODELS pointed at the big /workspace mount."""
    e = os.environ.copy()
    e["OLLAMA_MODELS"] = OLLAMA_MODELS_DIR
    return e


def ensure_ollama_server():
    """Start ollama serve if not up; poll up to 20s."""
    if out("curl -s --max-time 2 http://localhost:11434/api/tags"):
        ok("Ollama server already running")
        return True
    if not have("ollama"):
        return False
    os.makedirs(OLLAMA_MODELS_DIR, exist_ok=True)
    logf = open("/tmp/ollama_serve.log", "ab")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=logf, stderr=subprocess.STDOUT,
        env=ollama_env(), start_new_session=True,
    )
    print("  Starting ollama serve", end="", flush=True)
    for _ in range(40):  # 20s
        time.sleep(0.5)
        print(".", end="", flush=True)
        if out("curl -s --max-time 2 http://localhost:11434/api/tags"):
            print()
            ok("Ollama server is up")
            return True
    print()
    err("Ollama server did not start within 20s — check /tmp/ollama_serve.log")
    return False


def pulled_models():
    """Return list of model names from ollama list."""
    txt = out("ollama list 2>/dev/null")
    models = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


# ─── Installation steps ────────────────────────────────────────
def install_ollama():
    if have("ollama"):
        ok(f"Ollama already installed ({out('ollama --version')})")
        return True
    print("  Installing prerequisites (zstd, curl)...")
    sh("apt-get update -qq && apt-get install -y -qq zstd curl", check=False)
    print("  Installing Ollama...")
    r = sh("curl -fsSL https://ollama.com/install.sh | sh", check=False)
    if have("ollama"):
        ok("Ollama installed")
        return True
    err("Ollama install failed")
    return False


def install_python_deps():
    """pip with --break-system-packages fallback to apt."""
    try:
        import flask, requests, numpy  # noqa
        ok("Core Python deps already importable (flask/requests/numpy)")
    except ImportError:
        pass
    pkgs = " ".join(PIP_PACKAGES)
    attempts = [
        f"pip3 install -q {pkgs}",
        f"pip3 install -q --break-system-packages {pkgs}",
    ]
    for cmd in attempts:
        print(f"  $ {cmd}")
        r = sh(cmd, check=False, capture=True)
        if r.returncode == 0:
            ok("Python deps installed via pip")
            return True
        # Print actual error tail so user sees why
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        for line in tail[-4:]:
            print(f"    {line}")
    warn("pip blocked — falling back to apt-get")
    sh("apt-get update -qq && apt-get install -y -qq python3-flask python3-flask-cors python3-requests python3-numpy", check=False)
    # faiss-cpu has no apt package — last-ditch pip attempt
    sh("pip3 install -q --break-system-packages faiss-cpu", check=False)
    try:
        import flask  # noqa
        ok("flask importable")
        return True
    except ImportError:
        warn("flask still not importable — proxy may fail. Manual fix: pip3 install flask flask-cors requests numpy faiss-cpu")
        return False  # non-fatal; spec says warn, not abort


def pull_model(name):
    if not name:
        return True
    if name in pulled_models():
        ok(f"Already pulled: {name}")
        return True
    print(f"  Pulling {name} (this can take a while)...")
    r = subprocess.run(["ollama", "pull", name], env=ollama_env())
    if r.returncode == 0:
        ok(f"Pulled {name}")
        return True
    err(f"ollama pull {name} failed")
    return False


def make_modelfile(base_model, num_ctx):
    """Create a <base>-<ctx>k variant with PARAMETER num_ctx."""
    ctxk = max(1, round(int(num_ctx) / 1024))
    # Short display name: strip tag, append ctx
    stem = base_model.split(":")[0].split("/")[-1].lower()
    new_name = f"{stem}:{ctxk}k"
    if new_name in pulled_models():
        ok(f"Modelfile variant already exists: {new_name}")
        return new_name
    modelfile = f"/tmp/Modelfile.{ctxk}k"
    with open(modelfile, "w") as f:
        f.write(f"FROM {base_model}\nPARAMETER num_ctx {num_ctx}\n")
    print(f"  Creating {new_name} (FROM {base_model}, num_ctx={num_ctx})...")
    r = subprocess.run(["ollama", "create", new_name, "-f", modelfile], env=ollama_env())
    if r.returncode == 0:
        ok(f"Created {new_name}")
        return new_name
    err(f"ollama create failed for {new_name}")
    return base_model


def start_proxy(env_vars):
    # Kill anything on :8080 first (without fuser — not on minimal RunPod)
    sh("kill $(ss -tlnp 2>/dev/null | grep :8080 | grep -oP 'pid=\\K[0-9]+') 2>/dev/null", check=False)
    time.sleep(1)
    # .pyc cache overrides source silently — always clear
    sh("find /workspace -name '*.pyc' -delete 2>/dev/null; rm -rf /workspace/proxy/__pycache__", check=False)
    env = os.environ.copy()
    env.update(env_vars)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["OLLAMA_FLASH_ATTENTION"] = "1"
    env["OLLAMA_KV_CACHE_TYPE"] = "q8_0"
    env["OLLAMA_MODELS"] = OLLAMA_MODELS_DIR
    logf = open(LOG, "ab")
    subprocess.Popen(
        ["python3", "-uB", PROXY_PY],
        cwd=WORKSPACE, stdout=logf, stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
    )
    print(f"  Waiting for proxy on :8080", end="", flush=True)
    for _ in range(30):  # 15s
        time.sleep(0.5)
        print(".", end="", flush=True)
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    print()
                    ok(f"Proxy is up — {HEALTH_URL}")
                    return True
        except Exception:
            pass
    print()
    err(f"Proxy did not respond within 15s — check {LOG}")
    return False


def install_pi(models_json):
    print("  Installing Node.js 22...")
    r = sh("curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y -qq nodejs", check=False)
    if not have("node"):
        err("Node install failed — skipping Pi")
        return False
    print("  Installing pi-coding-agent...")
    sh("npm install -g pi-coding-agent", check=False)
    os.makedirs(os.path.expanduser("~/.pi"), exist_ok=True)
    with open(os.path.expanduser("~/.pi/models.json"), "w") as f:
        json.dump(models_json, f, indent=2)
    ok("Pi installed with ~/.pi/models.json")
    return True


# ─── Wizard ────────────────────────────────────────────────────
def wizard():
    print("=" * 56)
    print("  Mneme Setup Wizard")
    print("=" * 56)
    print("""
Model recommendations:
  • Hermes: 120K+ context (Hermes sends ~78KB of system prompt)
  • Pi:     32K context is plenty (Pi's prompt is tiny)
  • Any OpenAI-compatible model works. Pull models first with: ollama pull <name>
  • For large context, create a Modelfile with PARAMETER num_ctx, or use 'Custom' below
""")

    models = pulled_models()
    # Step 1 — main model
    opts = [(m, m) for m in models]
    opts.append(("__modelfile__", "Create 129K Modelfile from a pulled model"))
    opts.append(("__custom__", "Custom (type any ollama model name)"))
    choice = ask("Step 1 — Choose main chat model:", opts)

    if choice == "__custom__":
        main_model = ask_text("Enter model name (e.g. qwen2.5:7b)")
    elif choice == "__modelfile__":
        if not models:
            warn("No pulled models to base a Modelfile on — pick Custom instead")
            main_model = ask_text("Enter model name")
        else:
            base = ask("Base model for the 129K variant:", [(m, m) for m in models])
            main_model = make_modelfile(base, 131072)
    else:
        main_model = choice

    # Step 2 — context window
    ctx_opts = [(v, lbl) for v, lbl in CTX_CHOICES] + [("__custom__", "Custom (enter a number)")]
    ctx = ask("Step 2 — Context window size:", ctx_opts, default=2)
    if ctx == "__custom__":
        ctx = ask_text("Enter num_ctx (tokens)", "131072")
    ctx = int(ctx)
    # If user picked a big context on a plain pulled model, offer Modelfile
    if ctx > 32768 and ":" in main_model and not main_model.endswith(f":{ctx//1024}k"):
        if confirm(f"Create a Modelfile variant of {main_model} with num_ctx={ctx}?", default=True):
            main_model = make_modelfile(main_model, ctx)

    # Step 3 — chat interface
    iface = ask("Step 3 — Chat interface:", [
        ("pi", "Install Pi (pi-coding-agent)"),
        ("skip", "Skip — proxy only"),
        ("both", "Both proxy + Pi"),
    ], default=2)

    # Step 4 — embedding model
    emb_opts = EMBED_CHOICES + [("__custom__", "Custom")]
    embed = ask("Step 4 — Embedding model:", emb_opts)
    if embed == "__custom__":
        embed = ask_text("Enter embedding model", "snowflake-arctic-embed2:latest")

    # Step 5 — labeling model
    lbl_opts = LABEL_CHOICES + [("__custom__", "Custom")]
    label = ask("Step 5 — Labeling model:", lbl_opts)
    if label == "__custom__":
        label = ask_text("Enter labeling model", "qwen2.5:0.5b")

    # Summary
    print("\n" + "─" * 56)
    print(f"  Main model   : {main_model}")
    print(f"  Context      : {ctx} tokens")
    print(f"  Interface    : {iface}")
    print(f"  Embed model  : {embed}")
    print(f"  Label model  : {label}")
    print("─" * 56)
    if not confirm("Proceed with installation?", default=True):
        print("Aborted.")
        sys.exit(0)

    return {
        "model": main_model, "ctx": ctx, "iface": iface,
        "embed": embed, "label": label,
    }


# ─── Main ──────────────────────────────────────────────────────
def main():
    # Pre-flight: proxy code must exist
    if not (os.path.exists(PROXY_PY) and os.path.exists(PROXY_PROMPT)):
        print("Proxy code not found in /workspace/proxy — fetching...")
        os.makedirs(PROXY_DIR, exist_ok=True)
        if have("git"):
            r = sh(f"git clone -b {BRANCH} {REPO} /tmp/mneme_repo", check=False)
            if r.returncode == 0:
                sh(f"cp /tmp/mneme_repo/proxy/* {PROXY_DIR}/", check=False)
        if not os.path.exists(PROXY_PY):
            sh(f"curl -sSL -o {PROXY_PY} {RAW}/proxy/mneme_proxy.py", check=False)
            sh(f"curl -sSL -o {PROXY_PROMPT} {RAW}/proxy/system_prompt.md", check=False)
    if not os.path.exists(PROXY_PY):
        err("Could not obtain proxy/mneme_proxy.py — aborting")
        sys.exit(1)
    ok("Proxy code present")

    cfg = wizard()

    step(1, "GPU detection")
    gpu = out("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null",
              "Unknown (0GB VRAM)")
    print(f"  GPU: {gpu}")

    step(2, "Ollama install")
    if not install_ollama():
        sys.exit(1)
    if not ensure_ollama_server():
        sys.exit(1)

    step(3, "Python dependencies")
    install_python_deps()  # non-fatal on failure

    step(4, "Model pulling")
    pull_model(cfg["model"])
    pull_model(cfg["embed"])
    pull_model(cfg["label"])

    step(5, "Context Modelfile")
    if cfg["ctx"] > 32768:
        cfg["model"] = make_modelfile(cfg["model"], cfg["ctx"])
    else:
        ok("32K context — no Modelfile needed")

    step(6, "Starting Mneme proxy")
    env_vars = {
        "MNEME_MODEL": cfg["model"],
        "EMBED_MODEL": cfg["embed"],
        "LABEL_MODEL": cfg["label"],
    }
    proxy_ok = start_proxy(env_vars)

    step(7, "Pi install")
    if cfg["iface"] in ("pi", "both"):
        models_json = {
            "models": [{
                "name": cfg["model"],
                "provider": "ollama",
                "base_url": "http://localhost:8080/v1",
                "api_key": "none",
            }]
        }
        install_pi(models_json)
    else:
        ok("Skipped (proxy-only)")

    print("\n" + "=" * 56)
    if proxy_ok:
        print("  ✅ Mneme setup complete!")
        print(f"  Proxy : http://localhost:8080/v1  (health: /health)")
        print(f"  Model : {cfg['model']}")
        print(f"  Log   : {LOG}")
    else:
        print(f"  ⚠ Setup finished but proxy not responding — check {LOG}")
    print("=" * 56)


if __name__ == "__main__":
    main()

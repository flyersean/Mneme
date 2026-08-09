#!/usr/bin/env python3
"""Mneme setup wizard — interactive terminal setup for pod deployment.
No external dependencies — uses built-in input() only.
"""
import subprocess, sys, os, shutil, time, json

# Self-update: always fetch latest before running
_SETUP_URL = "https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py"
if __file__.startswith("/tmp/") or __file__.startswith("/var/"):
    # Running from temp file — check if we're stale by comparing size
    try:
        import urllib.request
        remote_size = int(urllib.request.urlopen(_SETUP_URL).headers.get("Content-Length", 0))
        local_size = os.path.getsize(__file__)
        if remote_size > 0 and remote_size != local_size:
            print("Updating to latest version...")
            os.remove(__file__)
            subprocess.run(["curl", "-sSL", "-o", __file__, _SETUP_URL], check=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except:
        pass  # network unavailable, continue with current version

# Reopen stdin from terminal if piped (curl|bash closes the pipe)
if not sys.stdin.isatty():
    try:
        sys.stdin = open("/dev/tty", "r")
    except:
        pass  # fall through, input() will get EOF

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
        except:
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

  Conversational memory proxy for AI agents
""")

def detect_gpu():
    try:
        out = run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader").stdout.strip()
        parts = out.split(",")
        gpu = parts[0].strip()
        vram_mb = int(parts[1].strip().replace(" MiB", ""))
        return gpu, vram_mb // 1024
    except:
        return "Unknown", 0

def install_ollama():
    if shutil.which("ollama"):
        run("ollama serve > /dev/null 2>&1 &")
        return True
    print("Installing Ollama...")
    run("apt-get update -qq && apt-get install -y -qq zstd curl")
    run("curl -fsSL https://ollama.com/install.sh | sh")
    if not shutil.which("ollama"):
        print("  Ollama install failed. Check network and re-run.")
        sys.exit(1)
    print("  Ollama installed.")
    run("ollama serve > /dev/null 2>&1 &")
    time.sleep(3)
    return True

def install_python_deps():
    print("Installing Python dependencies...")
    
    # Try pip with visible output so we can see errors
    for args in [
        [sys.executable, "-m", "pip", "install", "flask", "flask-cors", "faiss-cpu", "numpy", "requests"],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "flask", "flask-cors", "faiss-cpu", "numpy", "requests"],
    ]:
        print(f"  Running: {' '.join(args)}")
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  pip error: {r.stderr[-300:]}")
        try:
            import flask
            print("  Dependencies OK")
            return
        except ImportError:
            pass
        # Check via subprocess (pip may have installed to a new path)
        r2 = subprocess.run([sys.executable, "-c", "import flask"], capture_output=True)
        if r2.returncode == 0:
            print("  Dependencies OK (restart Python to use)")
            return
        print("  Flask still not importable, trying next method...")
    
    # Fall back to apt
    print("  Trying apt-get...")
    subprocess.run(["apt-get", "update", "-qq"], capture_output=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "python3-flask", "python3-requests", "python3-numpy"], capture_output=True)
    # pip install faiss-cpu (not in apt)
    subprocess.run([sys.executable, "-m", "pip", "install", "--break-system-packages", "faiss-cpu", "flask-cors"], capture_output=True)
    
    # Check via subprocess
    r = subprocess.run([sys.executable, "-c", "import flask"], capture_output=True)
    if r.returncode == 0:
        print("  Dependencies OK (apt)")
    else:
        print("  WARNING: Flask not importable. Proxy will fail to start.")
        print("  Manual fix: pip install --break-system-packages flask flask-cors faiss-cpu numpy requests")

def pull_model(name):
    """Pull a model if not already present. Retries ollama list in case server is busy."""
    # Check if already pulled (retry up to 3 times)
    for attempt in range(3):
        try:
            r = run(f"ollama list", timeout=10)
            if r.returncode == 0 and name in r.stdout:
                print(f"  {name} already pulled, skipping.")
                return
        except:
            pass
        if attempt < 2:
            time.sleep(2)
    
    print(f"  Pulling {name}...")
    r = run(f"ollama pull {name}", timeout=600)
    if r.returncode != 0:
        print(f"  Warning: could not pull {name}")

def get_pulled_models():
    """Return list of (display_name, model_name) for models already in Ollama."""
    if not shutil.which("ollama"):
        return []
    
    # Start ollama if not running, poll until ready
    run("ollama serve > /dev/null 2>&1 &")
    for _ in range(20):
        time.sleep(1)
        try:
            out = run("ollama list", timeout=10).stdout
            if "NAME" in out:
                break
        except:
            continue
    
    models = []
    exclude = {"qwen2.5:0.5b", "snowflake-arctic-embed2", "nomic-embed-text", "mneme-chat:latest"}
    for line in out.splitlines():
        parts = line.split()
        if not parts or "NAME" in line:
            continue
        name = parts[0]
        if name in exclude:
            continue
        size = parts[1] if len(parts) > 1 else "?"
        tag = " (pulled)"
        models.append((f"{name}{tag}", name))
    return models

def main():
    banner()
    
    # Cleanup from previous runs
    subprocess.run(["pkill", "-f", "mneme_proxy.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
    time.sleep(1)
    
    gpu, vram = detect_gpu()
    print(f"GPU: {gpu} ({vram}GB VRAM)")
    
    print("\nThis wizard will set up the Mneme memory proxy on this machine.\n")
    
    print("\033[1mModel recommendations:\033[0m")
    print("  • Hermes: 120K+ context (Hermes sends ~78KB of system prompt)")
    print("  • Pi:     32K context is plenty (Pi's prompt is tiny)")
    print("  • Any OpenAI-compatible model works. Pull models first with: ollama pull <name>")
    print("  • For large context, create a Modelfile with PARAMETER num_ctx, or use 'Custom' below")
    print()
    
    # ── Step 1: Model ──
    pulled = get_pulled_models()
    
    opts = []
    models = []
    if pulled:
        opts.append("── Pulled models ──")
        for p in pulled:
            opts.append(p[0])
            models.append(p)
    
    opts.append("── Other options ──")
    models.append(("+ 129K Modelfile from a pulled model (creates custom high-context model)", "__120k__"))
    opts.append("+ 129K Modelfile from a pulled model (creates custom high-context model)")
    models.append(("Custom (enter any Ollama model name)", "__custom__"))
    opts.append("Custom (enter any Ollama model name)")
    
    idx, _ = choose("Step 1/4: Choose main model", opts)
    
    # Look up chosen model by value, not index (opts has separators models doesn't)
    chosen_label = opts[idx]
    model_entry = None
    for m in models:
        if m[0] == chosen_label:
            model_entry = m
            break
    
    if not model_entry:
        print("Invalid selection")
        sys.exit(1)
    
    if model_entry[1] == "__custom__":
        model_name = ask("Enter Ollama model name")
        if not model_name:
            sys.exit(1)
    elif model_entry[1] == "__120k__":
        # Use any already-pulled model as base for Modelfile
        base = None
        pulled_names = {p[1] for p in pulled}
        for pname in pulled_names:
            if len(pname) > 5:
                base = pname
                break
        model_name = base if base else ask("No pulled models found. Enter base model name (e.g. qwen3.6:35b-a3b)")
        if base:
            print(f"  Using already-pulled {base} as base for 120K model")
    else:
        model_name = model_entry[1]
    
    print(f"  Selected: {model_name}")
    
    # ── Step 2: Context size ──
    ctx_options = [
        ("32K (default, fast)", 32000),
        ("129K (recommended — use with q8_0 KV cache)", 129000),
        ("264K (needs q4_0 KV cache on A40)", 264000),
        ("Custom (enter value)", 0),
    ]
    
    idx, _ = choose("\nStep 2/4: Context window size", [c[0] for c in ctx_options])
    
    if ctx_options[idx][1] == 0:
        ctx_val = ask("Enter context size (e.g. 32000, 65536, 129000)")
        if not ctx_val:
            sys.exit(1)
        ctx_size = int(ctx_val)
    else:
        ctx_size = ctx_options[idx][1]
    
    print(f"  Selected: {ctx_size}")
    
    # ── Step 3: Chat interface ──
    chat_options = [
        "Install Pi (terminal AI coding assistant)",
        "Skip — proxy only (connect external app or agent)",
        "Both",
    ]
    
    _, choice = choose("\nStep 3/4: Chat interface", chat_options)
    install_pi = "Pi" in choice or "Both" in choice
    
    # ── Step 4: Embedding model ──
    embed_options = [
        ("snowflake-arctic-embed2 (1.2GB, recommended)", "snowflake-arctic-embed2"),
        ("nomic-embed-text (274MB, lighter)", "nomic-embed-text"),
        ("Custom (enter any Ollama embedding model)", "__custom__"),
    ]
    
    idx, _ = choose("\nStep 4/5: Embedding model for memory retrieval", [e[0] for e in embed_options])
    
    if embed_options[idx][1] == "__custom__":
        embed_model = ask("Enter embedding model name")
        if not embed_model:
            sys.exit(1)
    else:
        embed_model = embed_options[idx][1]
    
    print(f"  Selected: {embed_model}")
    
    # ── Step 5: Labeling model ──
    label_options = [
        ("qwen2.5:0.5b (default, tiny, 397MB)", "qwen2.5:0.5b"),
        ("qwen2.5:1.5b (better labels, ~1GB)", "qwen2.5:1.5b"),
        ("qwen2.5:3b (best labels, ~2GB)", "qwen2.5:3b"),
        ("Custom (enter any Ollama model name)", "__custom__"),
    ]
    
    idx, _ = choose("\nStep 5/5: Labeling model (generates topic labels for stored chunks)", 
                    [l[0] for l in label_options])
    
    if label_options[idx][1] == "__custom__":
        label_model = ask("Enter labeling model name")
        if not label_model:
            sys.exit(1)
    else:
        label_model = label_options[idx][1]
    
    print(f"  Selected: {label_model}")
    
    # ── INSTALLATION ──
    print(f"\n{'='*50}")
    print("Installing...")
    print("="*50)
    
    # Quick check: are deps already installed?
    deps_ok = subprocess.run([sys.executable, "-c", "import flask, numpy, requests, faiss"],
                              capture_output=True).returncode == 0
    
    if not deps_ok:
        install_ollama()
        install_python_deps()
    else:
        print("  Python dependencies already installed, skipping.")
        # Still ensure Ollama is running
        if shutil.which("ollama"):
            if subprocess.run("curl -s --max-time 2 http://localhost:11434 >/dev/null", 
                            shell=True).returncode != 0:
                subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(3)
    
    # Pull models
    print(f"\nPulling models (this may take a few minutes)...")
    pull_model(model_name)
    pull_model(embed_model)
    pull_model(label_model)
    print("  Models ready.")
    
    # Create context-size modelfile if needed
    proxy_model = model_name
    if ctx_size > 32000:
        print(f"  Creating modelfile with {ctx_size} context...")
        modelfile = f"FROM {model_name}\nPARAMETER num_ctx {ctx_size}\n"
        with open("/tmp/Modelfile.mneme", "w") as f:
            f.write(modelfile)
        run("ollama create mneme-chat -f /tmp/Modelfile.mneme", timeout=60)
        proxy_model = "mneme-chat:latest"
    
    # Start proxy
    print(f"\nStarting Mneme proxy...")
    subprocess.run(["pkill", "-f", "mneme_proxy.py"], capture_output=True)
    time.sleep(1)
    
    os.makedirs("/workspace/mneme_chunks", exist_ok=True)
    
    env = os.environ.copy()
    env["OLLAMA_FLASH_ATTENTION"] = "1"
    env["OLLAMA_KV_CACHE_TYPE"] = "q8_0"
    env["MNEME_MODEL"] = proxy_model
    env["EMBED_MODEL"] = embed_model
    env["LABEL_MODEL"] = label_model
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    
    log = open("/tmp/mneme.log", "w")
    subprocess.Popen(
        [sys.executable, "-uB", "proxy/mneme_proxy.py"],
        cwd="/workspace", env=env, stdout=log, stderr=log,
        start_new_session=True
    )
    
    # Wait for proxy to start
    print("  Waiting for proxy...", end=" ", flush=True)
    for _ in range(15):
        time.sleep(1)
        try:
            import urllib.request
            d = json.loads(urllib.request.urlopen("http://localhost:8080/health", timeout=2).read())
            print(f"running ({d.get('chunks',0)} chunks)")
            break
        except:
            continue
    else:
        print("timeout — check /tmp/mneme.log")
    time.sleep(3)
    
    # Install Pi if requested
    ext_path = "/workspace/mneme-search-tool.ts"
    web_ext_path = "/workspace/mneme-web-tools.ts"
    if install_pi:
        print("\nInstalling Pi (AI coding assistant)...")
        if not shutil.which("node") or "v22" not in run("node --version").stdout:
            print("  Installing Node.js 22...")
            run("curl -fsSL https://deb.nodesource.com/setup_22.x | bash -", timeout=60)
            run("apt-get install -y nodejs", timeout=60)
        
        run("npm install -g @earendil-works/pi-coding-agent", timeout=120)
        
        # Configure Pi
        pi_config = {
            "providers": {
                "mneme": {
                    "baseUrl": "http://localhost:8080/v1",
                    "api": "openai-completions",
                    "apiKey": "none",
                    "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                    "models": [{"id": "text-mneme:64k", "name": f"Mneme ({model_name})", 
                                "contextWindow": ctx_size, "reasoning": False}]
                }
            }
        }
        os.makedirs(os.path.expanduser("~/.pi/agent"), exist_ok=True)
        with open(os.path.expanduser("~/.pi/agent/models.json"), "w") as f:
            json.dump(pi_config, f, indent=2)
        print("  Pi configured to use Mneme at localhost:8080")
        
        # Install search_memory extension
        if not os.path.exists(ext_path):
            print("  Installing search_memory extension...")
            ext_url = "https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/extensions/pi/mneme-search-tool.ts"
            r = run(f"curl -sSL -o {ext_path} {ext_url}")
            if r.returncode == 0:
                print("    ✓ search_memory installed")
            else:
                print("    Warning: could not download extension")
        
        # Install web tools extension (search + scrape)
        if not os.path.exists(web_ext_path):
            print("  Installing web tools extension...")
            web_url = "https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/extensions/pi/mneme-web-tools.ts"
            r = run(f"curl -sSL -o {web_ext_path} {web_url}")
            if r.returncode == 0:
                print("    ✓ web_search + web_scrape installed")
            else:
                print("    Warning: could not download web tools")
    
    # ── DONE ──
    health = ""
    try:
        import urllib.request
        d = json.loads(urllib.request.urlopen("http://localhost:8080/health", timeout=3).read())
        health = f" ({d.get('chunks',0)} chunks, {d.get('backend','?')})"
    except:
        pass
    
    print(f"""
\033[1m{'='*50}\033[0m
\033[32m  Mneme is running!\033[0m{' ' + health}
{'='*50}

  Proxy:    http://localhost:8080
  Health:   http://localhost:8080/health
  API base: http://localhost:8080/v1

  Connect from this machine or via SSH:
    OpenAI client → http://localhost:8080/v1""")
    
    if install_pi:
        extensions = ""
        if os.path.exists(ext_path):
            extensions += f" --extension {ext_path}"
        if os.path.exists(web_ext_path):
            extensions += f" --extension {web_ext_path}"
        print(f"    Pi agent:     pi --provider mneme --model text-mneme:64k{extensions}")
    
    print(f"\n  Logs: tail -f /tmp/mneme.log\n")

if __name__ == "__main__":
    main()

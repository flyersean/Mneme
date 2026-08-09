#!/usr/bin/env python3
"""Mneme setup wizard — interactive terminal setup for pod deployment.
No external dependencies — uses built-in input() only.
"""

import subprocess, sys, os, shutil, time, json

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
    r = run("curl -fsSL https://ollama.com/install.sh | sh")
    if r.returncode != 0:
        print("Failed to install Ollama. Install manually and re-run.")
        sys.exit(1)
    run("ollama serve > /dev/null 2>&1 &")
    time.sleep(2)
    return True

def install_python_deps():
    print("Installing Python dependencies...")
    r = run(f"{sys.executable} -m pip install flask flask-cors faiss-cpu numpy requests")
    if r.returncode != 0:
        print(f"  pip install failed: {r.stderr[:200]}")
        print("  Trying with --break-system-packages...")
        run(f"{sys.executable} -m pip install --break-system-packages flask flask-cors faiss-cpu numpy requests")
    # Verify flask is importable
    try:
        import flask
        print("  Dependencies OK")
    except ImportError:
        print("  WARNING: Flask still not importable. Proxy may fail to start.")

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
        tag = " (pulled)" if name not in [p[1] for p in MODULE_PRESETS] else " (pulled)"
        models.append((f"{name}{tag}", name))
    return models

# Module-level presets so get_pulled_models can reference them
MODULE_PRESETS = [
    ("fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest (32K)", 
     "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"),
    ("fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-120k:latest (120K)", 
     "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-120k:latest"),
    ("qwen3.6:35b-a3b (official Ollama, 32K)", "qwen3.6:35b-a3b"),
    ("qwen3.6:35b-a3b + 129K Modelfile (creates custom model)", "__120k__"),
    ("qwen2.5:7b (lightweight, ~5GB)", "qwen2.5:7b"),
    ("qwen2.5:14b (mid-range, ~10GB)", "qwen2.5:14b"),
    ("Custom (enter any Ollama model name)", "__custom__"),
]

def main():
    banner()
    
    gpu, vram = detect_gpu()
    print(f"GPU: {gpu} ({vram}GB VRAM)")
    
    print("\nThis wizard will set up the Mneme memory proxy on this machine.\n")
    
    # ── Step 1: Model ──
    # Detect already-pulled models
    pulled = get_pulled_models()
    
    # Build options: pulled models first, then presets not already shown
    pulled_names = {p[1] for p in pulled}
    remaining_presets = [p for p in MODULE_PRESETS if p[1] not in pulled_names]
    
    opts = []
    models = []
    if pulled:
        opts.append("── Already in Ollama ──")
        for p in pulled:
            opts.append(p[0])
            models.append(p)
        if remaining_presets:
            opts.append("── Available to install ──")
    for p in remaining_presets:
        opts.append(p[0])
        models.append(p)
    
    idx, _ = choose("Step 1/4: Choose main model", opts)
    
    if models[idx][1] == "__custom__":
        model_name = ask("Enter Ollama model name")
        if not model_name:
            sys.exit(1)
    elif models[idx][1] == "__120k__":
        # Use any already-pulled Qwen 3.6 35B as base
        base = None
        for pname in pulled_names:
            if "qwen" in pname.lower() and "35" in pname:
                base = pname
                break
        model_name = base if base else "qwen3.6:35b-a3b"
        if base:
            print(f"  Using already-pulled {base} as base for 120K model")
    else:
        model_name = models[idx][1]
    
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
    
    idx, _ = choose("\nStep 4/4: Embedding model for memory retrieval", [e[0] for e in embed_options])
    
    if embed_options[idx][1] == "__custom__":
        embed_model = ask("Enter embedding model name")
        if not embed_model:
            sys.exit(1)
    else:
        embed_model = embed_options[idx][1]
    
    print(f"  Selected: {embed_model}")
    
    # ── INSTALLATION ──
    print(f"\n{'='*50}")
    print("Installing...")
    print("="*50)
    
    install_ollama()
    install_python_deps()
    
    # Pull models
    print(f"\nPulling models (this may take a few minutes)...")
    pull_model(model_name)
    pull_model(embed_model)
    pull_model("qwen2.5:0.5b")  # labeler
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
                print("    Extension installed at /workspace/mneme-search-tool.ts")
            else:
                print("    Warning: could not download extension")
    
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
        ext_flag = f" --extension {ext_path}" if os.path.exists(ext_path) else ""
        print(f"    Pi agent:     pi --provider mneme --model text-mneme:64k{ext_flag}")
    
    print(f"\n  Logs: tail -f /tmp/mneme.log\n")

if __name__ == "__main__":
    main()

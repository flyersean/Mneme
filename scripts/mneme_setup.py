#!/usr/bin/env python3
"""Mneme setup wizard — interactive terminal setup for pod deployment.
No external dependencies — uses built-in input() only.
"""

import subprocess, sys, os, shutil, time, json

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
    run(f"{sys.executable} -m pip install flask flask-cors faiss-cpu numpy requests -q")

def pull_model(name):
    print(f"  Pulling {name}...")
    r = run(f"ollama pull {name}", timeout=600)
    if r.returncode != 0:
        print(f"  Warning: could not pull {name}")

def main():
    banner()
    
    gpu, vram = detect_gpu()
    print(f"GPU: {gpu} ({vram}GB VRAM)")
    
    print("\nThis wizard will set up the Mneme memory proxy on this machine.\n")
    
    # ── Step 1: Model ──
    models = [
        ("qwen3.6-35b-120k:latest (120K context, needs ~45GB VRAM)", "qwen3.6-35b-120k:latest"),
        ("fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest (32K, ~25GB)", 
         "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"),
        ("qwen2.5:7b (lightweight, ~5GB)", "qwen2.5:7b"),
        ("qwen2.5:14b (mid-range, ~10GB)", "qwen2.5:14b"),
        ("Custom (enter any Ollama model name)", "__custom__"),
    ]
    
    idx, _ = choose("Step 1/4: Choose main model", [m[0] for m in models])
    
    if models[idx][1] == "__custom__":
        model_name = ask("Enter Ollama model name")
        if not model_name:
            sys.exit(1)
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
    run("pkill -f mneme_proxy.py")
    time.sleep(1)
    
    os.makedirs("/workspace/mneme_chunks", exist_ok=True)
    
    run(
        f"cd /workspace && OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 "
        f"MNEME_MODEL={proxy_model} EMBED_MODEL={embed_model} "
        f"OLLAMA_KEEP_ALIVE=24h PYTHONDONTWRITEBYTECODE=1 "
        f"nohup {sys.executable} -uB proxy/mneme_proxy.py > /tmp/mneme.log 2>&1 &"
    )
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

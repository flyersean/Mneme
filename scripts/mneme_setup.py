#!/usr/bin/env python3
"""Mneme setup wizard — interactive terminal TUI for pod deployment."""

import subprocess, sys, os, socket, shutil

# Check for questionary
try:
    import questionary
except ImportError:
    print("Installing questionary...")
    subprocess.run([sys.executable, "-m", "pip", "install", "questionary", "-q"], check=True)
    import questionary

def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)

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
    """Detect GPU and VRAM."""
    try:
        out = run("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader").stdout.strip()
        parts = out.split(",")
        gpu = parts[0].strip()
        vram_mb = int(parts[1].strip().replace(" MiB", ""))
        return gpu, vram_mb // 1024  # GB
    except:
        return "Unknown", 0

def install_ollama():
    """Install Ollama if not present."""
    if shutil.which("ollama"):
        run("ollama serve > /dev/null 2>&1 &")
        return True
    print("Installing Ollama...")
    r = run("curl -fsSL https://ollama.com/install.sh | sh", timeout=120)
    if r.returncode != 0:
        print("Failed to install Ollama. Install manually and re-run.")
        sys.exit(1)
    run("ollama serve > /dev/null 2>&1 &")
    import time; time.sleep(2)
    return True

def install_python_deps():
    """Install Flask and other Python deps."""
    print("Installing Python dependencies...")
    run(f"{sys.executable} -m pip install flask flask-cors faiss-cpu numpy requests -q")
    print("  Done.")

def pull_model(name):
    """Pull an Ollama model."""
    print(f"  Pulling {name}...")
    r = run(f"ollama pull {name}", timeout=600)
    if r.returncode != 0:
        print(f"  Warning: could not pull {name}")

def main():
    banner()
    
    # Check system
    gpu, vram = detect_gpu()
    print(f"GPU: {gpu} ({vram}GB VRAM)")
    
    if not shutil.which("python3") and not shutil.which("python"):
        print("Python 3 required. Install and re-run.")
        sys.exit(1)
    
    print("\nThis wizard will set up the Mneme memory proxy on this machine.\n")
    
    # Step 1: Main model
    model_options = [
        "qwen3.6-35b-120k:latest (120K context, needs ~45GB VRAM)",
        "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest (32K context, needs ~25GB VRAM)",
        "qwen2.5:7b (lightweight, ~5GB VRAM)",
        "qwen2.5:14b (mid-range, ~10GB VRAM)",
        "Custom (enter any Ollama model name)",
    ]
    
    choice = questionary.select(
        "Step 1/4: Choose main model",
        choices=model_options,
    ).ask()
    
    if choice is None:
        sys.exit(1)
    
    if "Custom" in choice:
        model_name = questionary.text("Enter Ollama model name:").ask()
        if not model_name:
            sys.exit(1)
    elif "120K" in choice:
        model_name = "qwen3.6-35b-120k:latest"
    elif "32K" in choice:
        model_name = "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"
    elif "7b" in choice:
        model_name = "qwen2.5:7b"
    elif "14b" in choice:
        model_name = "qwen2.5:14b"
    else:
        model_name = choice.split(" ")[0]
    
    print(f"  Selected: {model_name}")
    
    # Step 2: Context size
    ctx_options = [
        "32K (default, fast)",
        "120K (needs 45GB+ VRAM)",
        "Custom (enter value like 8K, 64K, 200K)",
    ]
    
    choice = questionary.select(
        "\nStep 2/4: Context window size",
        choices=ctx_options,
    ).ask()
    
    if choice is None:
        sys.exit(1)
    
    if "Custom" in choice:
        ctx_val = questionary.text("Enter context size (e.g., 32000, 65536, 120000):").ask()
        if not ctx_val:
            sys.exit(1)
        ctx_size = int(ctx_val)
    elif "120K" in choice:
        ctx_size = 120000
    else:
        ctx_size = 32000
    
    print(f"  Selected: {ctx_size}")
    
    # Step 3: Chat interface
    chat_options = [
        "Install Pi (terminal-based AI coding assistant)",
        "Skip — proxy only (connect external app or agent)",
        "Both",
    ]
    
    choice = questionary.select(
        "\nStep 3/4: Chat interface",
        choices=chat_options,
    ).ask()
    
    if choice is None:
        sys.exit(1)
    
    install_pi = "Pi" in choice or "Both" in choice
    
    # Step 4: Embedding model
    embed_options = [
        "snowflake-arctic-embed2 (1.2GB, recommended)",
        "nomic-embed-text (274MB, lighter)",
        "Custom (enter any Ollama embedding model)",
    ]
    
    choice = questionary.select(
        "\nStep 4/4: Embedding model for memory retrieval",
        choices=embed_options,
    ).ask()
    
    if choice is None:
        sys.exit(1)
    
    if "Custom" in choice:
        embed_model = questionary.text("Enter embedding model name:").ask()
        if not embed_model:
            sys.exit(1)
    elif "arctic" in choice:
        embed_model = "snowflake-arctic-embed2"
    else:
        embed_model = "nomic-embed-text"
    
    print(f"  Selected: {embed_model}")
    
    # ── INSTALLATION PHASE ──
    print(f"\n{'='*50}")
    print("Installing...")
    print("="*50)
    
    install_ollama()
    install_python_deps()
    
    # Pull models
    print(f"\nPulling models (this may take a few minutes)...")
    pull_model(model_name)
    pull_model(embed_model)
    pull_model("qwen2.5:0.5b")  # labeler, always needed
    print("  Models ready.")
    
    # Create 120K modelfile if needed
    if ctx_size > 32000:
        print(f"  Creating modelfile with {ctx_size} context...")
        modelfile = f"FROM {model_name}\nPARAMETER num_ctx {ctx_size}\n"
        with open("/tmp/Modelfile.mneme", "w") as f:
            f.write(modelfile)
        run(f"ollama create mneme-chat -f /tmp/Modelfile.mneme", timeout=60)
        proxy_model = "mneme-chat:latest"
    else:
        proxy_model = model_name
    
    # Start proxy
    print(f"\nStarting Mneme proxy...")
    run("pkill -f mneme_proxy.py", check=False)
    import time; time.sleep(1)
    
    os.makedirs("/workspace/mneme_chunks", exist_ok=True)
    
    env = os.environ.copy()
    env["MNEME_MODEL"] = proxy_model
    env["EMBED_MODEL"] = embed_model
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    
    run(
        f"cd /workspace && nohup {sys.executable} -uB proxy/mneme_proxy.py > /tmp/mneme.log 2>&1 &",
        shell=True
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
                    "models": [{"id": "text-mneme:64k", "name": f"Mneme ({model_name})", "contextWindow": ctx_size, "reasoning": False}]
                }
            }
        }
        os.makedirs(os.path.expanduser("~/.pi/agent"), exist_ok=True)
        with open(os.path.expanduser("~/.pi/agent/models.json"), "w") as f:
            import json; json.dump(pi_config, f, indent=2)
        print("  Pi configured to use Mneme at localhost:8080")
        
        # Install search_memory extension
        ext_path = "/workspace/mneme-search-tool.ts"
        if not os.path.exists(ext_path):
            print("  Installing search_memory extension...")
            ext_url = "https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/extensions/pi/mneme-search-tool.ts"
            r = run(f"curl -sSL -o {ext_path} {ext_url}")
            if r.returncode == 0:
                print("    Extension installed at /workspace/mneme-search-tool.ts")
            else:
                print("    Warning: could not download extension (search_memory tool not registered)")
    
    # ── DONE ──
    print(f"\n{'='*50}")
    print("\033[32m✓ Mneme is running!\033[0m")
    print("="*50)
    print(f"  Proxy:    http://localhost:8080")
    print(f"  Health:   http://localhost:8080/health")
    print(f"  API base: http://localhost:8080/v1")
    print()
    print("Connect from this machine (SSH or local):")
    print(f"  OpenAI client → http://localhost:8080/v1")
    if install_pi:
        ext_flag = f" --extension {ext_path}" if os.path.exists(ext_path) else ""
        print(f"  Pi agent:     pi --provider mneme --model text-mneme:64k{ext_flag}")
    print()
    print("Logs: tail -f /tmp/mneme.log")
    print()

if __name__ == "__main__":
    main()

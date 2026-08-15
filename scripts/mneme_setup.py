#!/usr/bin/env python3
"""Mneme setup wizard — interactive terminal setup for pod deployment.
No external dependencies — uses built-in input() only.
"""
import subprocess, sys, os, shutil, time, json

# Self-update: always fetch latest before running
_SETUP_URL = "https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/scripts/mneme_setup.py"
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

def _add_model(existing_db, embed_model, label_model):
    """Add a new model instance to an existing Mneme DB."""
    print("\n\033[1mAdd Model to Existing Mneme Installation\033[0m\n")
    
    # Find next available port
    import socket
    port = 8080
    for p in range(8080, 8100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if s.connect_ex(('127.0.0.1', p)) != 0:
            port = p
            s.close()
            break
        s.close()
    
    # Pick model
    pulled = get_pulled_models()
    opts = []
    models = []
    if pulled:
        opts.append("── Pulled models ──")
        for p in pulled:
            opts.append(p[0])
            models.append(p)
    models.append(("Custom (enter any Ollama model name)", "__custom__"))
    opts.append("Custom (enter any Ollama model name)")
    
    idx, _ = choose(f"Choose model for new instance (port {port})", opts)
    chosen_label = opts[idx]
    model_entry = None
    for m in models:
        if m[0] == chosen_label:
            model_entry = m
            break
    if not model_entry:
        sys.exit(1)
    if model_entry[1] == "__custom__":
        model_name = ask("Enter Ollama model name")
        if not model_name: sys.exit(1)
    else:
        model_name = model_entry[1]
    
    # Context size
    ctx_options = [
        ("32K (default, fast)", 32000),
        ("129K (recommended)", 129000),
        ("Custom (enter value)", 0),
    ]
    idx, _ = choose("\nContext window size", [c[0] for c in ctx_options])
    if ctx_options[idx][1] == 0:
        ctx_size = int(ask("Enter context size") or "32000")
    else:
        ctx_size = ctx_options[idx][1]
    
    # Injection preference
    inject_opts = [
        ("Yes — inject Mneme instructions (default)", "1"),
        ("No — skip (uses merged prompt e.g. Pi SYSTEM.md)", "0"),
    ]
    idx, _ = choose("\nInject Mneme system instructions?", [o[0] for o in inject_opts])
    inject_system = inject_opts[idx][1]
    
    # Pull model if needed
    pull_model(model_name)
    
    # Create context modelfile if > 32K
    proxy_model = model_name
    if ctx_size > 32000:
        modelfile = f"FROM {model_name}\nPARAMETER num_ctx {ctx_size}\n"
        with open("/tmp/Modelfile.mneme", "w") as f:
            f.write(modelfile)
        run(f"ollama create mneme-chat-{port} -f /tmp/Modelfile.mneme", timeout=60)
        proxy_model = f"mneme-chat-{port}:latest"
    
    # Generate start script
    script_path = f"/workspace/start_proxy_{port}.sh"
    with open(script_path, "w") as f:
        f.write(f"""#!/bin/bash
# Mneme proxy — model: {model_name} (port {port})
# Generated by mneme_setup.py — shared DB at {os.path.dirname(existing_db)}

export MNEME_MODEL="{proxy_model}"
export EMBED_MODEL="{embed_model}"
export LABEL_MODEL="{label_model}"
export MNEME_CHUNK_DIR="/workspace/mneme_chunks"
export MNEME_INJECT_SYSTEM="{inject_system}"
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0
export OLLAMA_KEEP_ALIVE=24h
export PYTHONDONTWRITEBYTECODE=1

cd /workspace
exec python3 -uB proxy/mneme_proxy.py
""")
    os.chmod(script_path, 0o755)
    
    # Start proxy
    env = os.environ.copy()
    env.update({
        "MNEME_MODEL": proxy_model, "EMBED_MODEL": embed_model,
        "LABEL_MODEL": label_model, "MNEME_CHUNK_DIR": "/workspace/mneme_chunks",
        "MNEME_INJECT_SYSTEM": inject_system,
        "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
        "OLLAMA_KEEP_ALIVE": "24h", "PYTHONDONTWRITEBYTECODE": "1",
    })
    log = open(f"/tmp/mneme_{port}.log", "w")
    subprocess.Popen(
        [sys.executable, "-uB", "proxy/mneme_proxy.py"],
        cwd="/workspace", env=env, stdout=log, stderr=log,
        start_new_session=True
    )
    
    # Wait for startup
    print(f"  Starting proxy on port {port}...", end=" ", flush=True)
    for _ in range(15):
        time.sleep(1)
        try:
            import urllib.request
            d = json.loads(urllib.request.urlopen(
                f"http://localhost:{port}/health", timeout=2).read())
            print(f"running ({d.get('chunks',0)} chunks)")
            break
        except:
            continue
    else:
        print("timeout — check logs")
    
    print(f"""
\033[32m  Model added!\033[0m
  Port:     {port}
  Model:    {model_name}
  Start:    {script_path}
  Logs:     tail -f /tmp/mneme_{port}.log

  All instances sharing: /workspace/mneme_chunks/
""")

def _reconfigure(existing_db, embed_model, label_model, prev_config):
    """Reconfigure existing Mneme installation — change model/settings, keep DB."""
    print("\n\033[1mReconfigure Mneme Installation\033[0m\n")
    
    prev_model = prev_config.get("model", "unknown")
    prev_port = prev_config.get("port", 8080)
    chunk_count = "?"
    try:
        import sqlite3
        c = sqlite3.connect(existing_db)
        chunk_count = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        c.close()
    except: pass
    
    print(f"  Current: model={prev_model}, port={prev_port}, chunks={chunk_count}")
    print(f"  DB preserved at: {os.path.dirname(existing_db)}")
    print()
    
    # Pick new model
    pulled = get_pulled_models()
    opts = []
    models = [("── Keep current ──", prev_model)]
    opts.append("── Keep current ──")
    opts.append(f"{prev_model} (current)")
    if pulled:
        opts.append("── Other pulled models ──")
        for p in pulled:
            if p[1] != prev_model:
                opts.append(p[0])
                models.append(p)
    models.append(("Custom (enter any Ollama model name)", "__custom__"))
    opts.append("Custom (enter any Ollama model name)")
    
    idx, _ = choose("Choose new model", opts)
    chosen = opts[idx]
    
    if "Keep current" in chosen or chosen == f"{prev_model} (current)":
        model_name = prev_model
        print(f"  Keeping: {model_name}")
    elif chosen == "Custom (enter any Ollama model name)":
        model_name = ask("Enter Ollama model name")
        if not model_name: sys.exit(1)
    else:
        for m in models:
            if m[0] == chosen:
                model_name = m[1]
                break
        else:
            model_name = prev_model
    
    if model_name != prev_model:
        pull_model(model_name)
    
    # Context size
    ctx_opts = [
        ("Keep current (from config)", 0),
        ("32K (default, fast)", 32000),
        ("129K (recommended)", 129000),
        ("Custom (enter value)", -1),
    ]
    idx, _ = choose("\nContext window size", [c[0] for c in ctx_opts])
    if ctx_opts[idx][1] == -1:
        ctx_size = int(ask("Enter context size") or "32000")
    elif ctx_opts[idx][1] == 0:
        ctx_size = prev_config.get("ctx_size", 32000)
    else:
        ctx_size = ctx_opts[idx][1]
    
    # Injection
    inject_opts = [
        ("Keep current", ""),
        ("Yes — inject Mneme instructions", "1"),
        ("No — skip (uses merged prompt)", "0"),
    ]
    idx, _ = choose("\nInject Mneme system instructions?", [o[0] for o in inject_opts])
    if inject_opts[idx][1] == "":
        inject_system = "1"  # default
    else:
        inject_system = inject_opts[idx][1]
    
    # Pi settings
    reinstall_pi = False
    if shutil.which("pi"):
        pi_opts = ["Skip — Pi is fine", "Reinstall Pi and extensions"]
        _, pi_choice = choose("\nPi agent?", pi_opts)
        reinstall_pi = "Reinstall" in pi_choice
    
    # Create context modelfile if > 32K
    proxy_model = model_name
    if ctx_size > 32000:
        modelfile = f"FROM {model_name}\nPARAMETER num_ctx {ctx_size}\n"
        with open("/tmp/Modelfile.mneme", "w") as f:
            f.write(modelfile)
        run(f"ollama create mneme-chat -f /tmp/Modelfile.mneme", timeout=60)
        proxy_model = "mneme-chat:latest"
    
    # Update config
    with open("/workspace/mneme_chunks/setup_config.json", "w") as f:
        json.dump({
            "model": model_name, "embed_model": embed_model,
            "label_model": label_model, "ctx_size": ctx_size,
            "port": prev_port,
        }, f)
    
    # Kill old proxy, restart with new settings
    print(f"\n  Restarting proxy with new config...")
    subprocess.run(["pkill", "-f", "mneme_proxy.py"], capture_output=True)
    time.sleep(2)
    
    env = os.environ.copy()
    env["OLLAMA_FLASH_ATTENTION"] = "1"
    env["OLLAMA_KV_CACHE_TYPE"] = "q8_0"
    env["MNEME_MODEL"] = proxy_model
    env["EMBED_MODEL"] = embed_model
    env["LABEL_MODEL"] = label_model
    env["MNEME_CHUNK_DIR"] = "/workspace/mneme_chunks"
    env["MNEME_INJECT_SYSTEM"] = inject_system
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    
    log = open("/tmp/mneme.log", "w")
    subprocess.Popen(
        [sys.executable, "-uB", "proxy/mneme_proxy.py"],
        cwd="/workspace", env=env, stdout=log, stderr=log,
        start_new_session=True
    )
    
    # Wait for startup
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
    
    # Reinstall Pi extensions if requested
    if reinstall_pi:
        ext_path = "/workspace/mneme-search-tool.ts"
        web_ext_path = "/workspace/mneme-web-tools.ts"
        for name, path, url_part in [
            ("search_memory", ext_path, "mneme-search-tool.ts"),
            ("web tools", web_ext_path, "mneme-web-tools.ts"),
        ]:
            print(f"  Installing {name} extension...")
            url = f"https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/extensions/pi/{url_part}"
            r = run(f"curl -sSL --fail -o {path} '{url}?{int(time.time())}'")
            if r.returncode == 0:
                print(f"    ✓ {name} installed")
            else:
                print(f"    Warning: could not download {name}")
    
    print(f"""
\033[32m  Mneme reconfigured!\033[0m
  Model:   {model_name}
  Context: {ctx_size}
  Chunks:  {chunk_count} (preserved)
  Inject:  {'Yes' if inject_system == '1' else 'No'}
  Proxy:   http://localhost:8080
  DB:      {os.path.dirname(existing_db)}
""")

MUSE_MODEL_NAME = "muse-glimmer:30b"
MUSE_SOURCE = "hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M"
# Ollama auto-detects a WRONG template for this GGUF (stalls ~3 tokens). This is
# the corrected template. See docs/muse-glimmer-model.md.
MUSE_MODELFILE = '''FROM hf.co/Blackfrost-AI/Muse-Glimmer-30B-Abliterated-GGUF:Q5_K_M
TEMPLATE """{{ if .System }}<|begin_of_text|><|start|>system<|message|>{{ .System }}

Reasoning strength: high.

# Valid recipients: "self", "user".<|eot|>{{ end }}{{ if .Prompt }}<|start|>user<|message|>{{ .Prompt }}<|eot|>{{ end }}<|start|>assistant"""
PARAMETER stop "<|eot|>"
PARAMETER stop "<|start|>user<|message|>"
PARAMETER num_ctx 32768
'''

def setup_muse():
    """Pull the Muse GGUF and create muse-glimmer:30b with the corrected template."""
    pull_model(MUSE_SOURCE)
    with open("/tmp/Modelfile.muse", "w") as f:
        f.write(MUSE_MODELFILE)
    r = run(f"ollama create {MUSE_MODEL_NAME} -f /tmp/Modelfile.muse", timeout=180)
    if r.returncode != 0:
        print(f"  Warning: muse create failed: {(r.stderr or '')[-200:]}")
    else:
        print(f"  ✓ {MUSE_MODEL_NAME} created with corrected template")
    return MUSE_MODEL_NAME

def main():
    banner()
    
    # Detect existing Mneme installation
    existing_db = "/workspace/mneme_chunks/mneme.db"
    existing_config = "/workspace/mneme_chunks/setup_config.json"
    is_add_model = False
    
    if os.path.exists(existing_db):
        chunk_count = "?"
        try:
            import sqlite3
            cdb = sqlite3.connect(existing_db)
            chunk_count = cdb.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            cdb.close()
        except:
            pass
        
        print(f"\n  Existing memory DB found: {chunk_count} chunks")
        
        # Read previous config if available
        prev_embed = ""
        prev_labeler = ""
        prev_model = ""
        if os.path.exists(existing_config):
            with open(existing_config) as f:
                prev = json.load(f)
            prev_embed = prev.get("embed_model", "")
            prev_labeler = prev.get("label_model", "")
            prev_model = prev.get("model", "")
        
        opts = [
            "Add another model (new proxy instance sharing this DB)",
            "Reconfigure existing installation (change model, settings, keep DB)",
            "Start fresh (wipe existing DB and reinstall)",
        ]
        _, choice = choose("\nWhat would you like to do?", opts)
        
        if "Add" in choice:
            is_add_model = True
            print(f"\n  Adding model to existing DB.")
            print(f"  Embed model locked to: {prev_embed or 'snowflake-arctic-embed2'}")
            print(f"  Labeler locked to: {prev_labeler or 'qwen2.5:0.5b'}")
        elif "Reconfigure" in choice:
            return _reconfigure(existing_db, prev_embed or "snowflake-arctic-embed2",
                             prev_labeler or "qwen2.5:0.5b", prev)
        else:
            print("\n  Wiping existing DB and starting fresh...")
            import shutil
            shutil.rmtree("/workspace/mneme_chunks", ignore_errors=True)
    
    # Cleanup from previous runs
    subprocess.run(["pkill", "-f", "mneme_proxy.py"], capture_output=True)
    subprocess.run(["pkill", "-f", "ollama serve"], capture_output=True)
    time.sleep(1)
    
    if is_add_model:
        return _add_model(existing_db, prev_embed or "snowflake-arctic-embed2", 
                         prev_labeler or "qwen2.5:0.5b")
    
    # ── Full setup below ──
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
    models.append(("Muse Glimmer 30B (abliterated, agentic — recommended)", "__muse__"))
    opts.append("Muse Glimmer 30B (abliterated, agentic — recommended)")
    models.append(("+ 129K Modelfile from a pulled model (creates custom high-context model)", "__120k__"))
    opts.append("+ 129K Modelfile from a pulled model (creates custom high-context model)")
    models.append(("Custom (enter any Ollama model name)", "__custom__"))
    opts.append("Custom (enter any Ollama model name)")
    
    idx, _ = choose("Step 1/6: Choose main model", opts)
    
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
    
    is_muse = False
    if model_entry[1] == "__custom__":
        model_name = ask("Enter Ollama model name")
        if not model_name:
            sys.exit(1)
    elif model_entry[1] == "__muse__":
        model_name = MUSE_MODEL_NAME
        is_muse = True
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
    
    idx, _ = choose("\nStep 2/6: Context window size", [c[0] for c in ctx_options])
    
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
    
    _, choice = choose("\nStep 3/6: Chat interface", chat_options)
    install_pi = "Pi" in choice or "Both" in choice
    
    # ── Step 4: Embedding model ──
    embed_options = [
        ("snowflake-arctic-embed2 (1.2GB, recommended)", "snowflake-arctic-embed2"),
        ("nomic-embed-text (274MB, lighter)", "nomic-embed-text"),
        ("Custom (enter any Ollama embedding model)", "__custom__"),
    ]
    
    idx, _ = choose("\nStep 4/6: Embedding model for memory retrieval", [e[0] for e in embed_options])
    
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
    
    idx, _ = choose("\nStep 5/6: Labeling model (generates topic labels for stored chunks)", 
                    [l[0] for l in label_options])
    
    if label_options[idx][1] == "__custom__":
        label_model = ask("Enter labeling model name")
        if not label_model:
            sys.exit(1)
    else:
        label_model = label_options[idx][1]
    
    print(f"  Selected: {label_model}")
    
    # ── Step 6: System prompt injection ──
    inject_options = [
        ("Yes — inject Mneme memory instructions separately (default, needed by most agents)", "1"),
        ("No — skip injection (for agents with merged prompts, e.g. Pi with SYSTEM.md)", "0"),
    ]
    
    idx, _ = choose("\nStep 6/6: Inject Mneme system instructions into model context?",
                    [o[0] for o in inject_options])
    inject_system = inject_options[idx][1]
    print(f"  Selected: {'Inject' if inject_system == '1' else 'Skip'}")
    
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
    if is_muse:
        model_name = setup_muse()
    else:
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
    
    # Save setup config for future multi-model additions
    with open("/workspace/mneme_chunks/setup_config.json", "w") as f:
        json.dump({
            "model": model_name, "embed_model": embed_model,
            "label_model": label_model, "ctx_size": ctx_size,
            "port": 8080,
        }, f)
    
    env = os.environ.copy()
    env["OLLAMA_FLASH_ATTENTION"] = "1"
    env["OLLAMA_KV_CACHE_TYPE"] = "q8_0"
    env["MNEME_MODEL"] = proxy_model
    env["EMBED_MODEL"] = embed_model
    env["LABEL_MODEL"] = label_model
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MNEME_INJECT_SYSTEM"] = inject_system
    
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
        
        # Link .pi to /workspace for Jupyter Lab access
        pi_dir = os.path.expanduser("~/.pi")
        if os.path.isdir(pi_dir):
            ws_link = "/workspace/pi-config"
            if os.path.islink(ws_link) or os.path.exists(ws_link):
                os.remove(ws_link)
            os.symlink(pi_dir, ws_link)
            print(f"  .pi linked → {ws_link} (edit prompts in Jupyter Lab)")
        
        # Install search_memory extension (always fresh)
        print("  Installing search_memory extension...")
        ext_url = "https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/extensions/pi/mneme-search-tool.ts"
        r = run(f"curl -sSL --fail -o {ext_path} '{ext_url}?{int(time.time())}'")
        if r.returncode == 0:
            print("    ✓ search_memory installed")
        else:
            print("    Warning: could not download search_memory extension")
        
        # Install web tools extension (always fresh)
        print("  Installing web tools extension...")
        web_url = "https://raw.githubusercontent.com/flyersean/Mneme/novelty-thinking/extensions/pi/mneme-web-tools.ts"
        r = run(f"curl -sSL --fail -o {web_ext_path} '{web_url}?{int(time.time())}'")
        if r.returncode == 0:
            print("    ✓ web_search + web_scrape installed")
        else:
            print("    Warning: could not download web tools extension")
    
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
    
    if inject_system == "0":
        print(f"\n  ⚠ Mneme system instructions injection DISABLED.")
        print(f"    Use a merged SYSTEM.md or APPEND_SYSTEM.md in ~/.pi/agent/")
        print(f"    Edit prompts in Jupyter Lab: /workspace/pi-config/agent/")
    
    print(f"\n  Logs: tail -f /tmp/mneme.log\n")

if __name__ == "__main__":
    main()

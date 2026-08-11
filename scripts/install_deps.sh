#!/bin/bash
# Mneme dependency installer — idempotent, pipe-safe, no interactive prompts.
# Run: curl -sSL <url> | bash
#
# Self-update: always fetch latest version before running.
if [ -z "${MNEME_DEPS_UPDATED:-}" ]; then
    export MNEME_DEPS_UPDATED=1
    curl -sSL -o /tmp/mneme_install.sh "https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/install_deps.sh"
    exec bash /tmp/mneme_install.sh
fi

set -e

echo "=== Mneme === Installing dependencies (pipe-safe, no prompts) ==="

# ── OS detection
if [ -f /etc/os-release ]; then . /etc/os-release; DISTRO=$ID; else DISTRO="unknown"; fi
echo "  Distro: $DISTRO"

# ── 1. Python dependencies
echo; echo "[1/4] Python dependencies"

# Remove system packages that conflict with pip versions
apt-get remove -y -qq python3-flask python3-flask-cors python3-werkzeug python3-blinker 2>/dev/null || true

# Check each package
for pkg in flask flask_cors faiss numpy requests; do
    if python3 -c "import $pkg" 2>/dev/null; then echo "  ✓ $pkg"; else echo "  ✗ $pkg missing"; fi
done

# Install fresh from pip (ignore-installed bypasses pinned system packages like blinker)
echo "  Installing from pip..."
if python3 -m pip install --break-system-packages --ignore-installed flask flask-cors faiss-cpu numpy requests 2>/dev/null; then
    echo "  ✓ pip install OK"
elif [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
    echo "  pip failed, trying apt..."
    apt-get update -qq 2>/dev/null || true
    apt-get install -y -qq python3-flask python3-flask-cors python3-numpy python3-requests 2>/dev/null || true
    python3 -m pip install --break-system-packages faiss-cpu 2>/dev/null || true
fi

# Verify
MISSED=""
for pkg in flask flask_cors faiss numpy requests; do
    if ! python3 -c "import $pkg" 2>/dev/null; then MISSED="$MISSED $pkg"; fi
done
if [ -n "$MISSED" ]; then
    echo "  ⚠ Still missing:$MISSED"
    echo "  Install manually: pip install --break-system-packages flask flask-cors faiss-cpu numpy requests"
else
    echo "  ✓ All Python packages available"
fi

# ── 2. Ollama
echo; echo "[2/4] Ollama"
if command -v ollama >/dev/null 2>&1; then
    echo "  ✓ ollama found ($(ollama --version 2>/dev/null || echo 'installed'))"
else
    echo "  Installing Ollama..."
    apt-get update -qq 2>/dev/null || true
    apt-get install -y -qq zstd curl 2>/dev/null || true
    curl -fsSL https://ollama.com/install.sh | sh
    if command -v ollama >/dev/null 2>&1; then echo "  ✓ ollama installed"
    else echo "  ⚠ Failed — install manually: curl -fsSL https://ollama.com/install.sh | sh"; fi
fi

# Start Ollama if not running
if ! curl -s --max-time 2 http://localhost:11434 >/dev/null 2>&1; then
    echo "  Starting Ollama..."
    nohup ollama serve > /tmp/ollama.log 2>&1 &
    for i in $(seq 1 15); do
        if curl -s --max-time 2 http://localhost:11434 >/dev/null 2>&1; then echo "  ✓ Ollama ready"; break; fi
        sleep 1
    done
else
    echo "  ✓ Ollama already running"
fi

# ── 3. Proxy code
echo; echo "[3/4] Proxy code"
mkdir -p /workspace/proxy /workspace/mneme_chunks
if [ ! -f /workspace/proxy/mneme_proxy.py ]; then
    echo "  Downloading proxy code..."
    if command -v git >/dev/null 2>&1; then
        git clone -b dev-chunks --depth 1 https://github.com/flyersean/Mneme.git /tmp/mneme_repo_install 2>/dev/null && {
            cp /tmp/mneme_repo_install/proxy/* /workspace/proxy/
            rm -rf /tmp/mneme_repo_install
        }
    fi
    if [ ! -f /workspace/proxy/mneme_proxy.py ]; then
        curl -sSL -o /workspace/proxy/mneme_proxy.py   https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/mneme_proxy.py
        curl -sSL -o /workspace/proxy/system_prompt.md https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/system_prompt.md
    fi
    echo "  ✓ Proxy code downloaded"
else
    echo "  ✓ Proxy code already exists (not overwriting)"
fi

# ── 4. Model storage
echo; echo "[4/4] Model storage"
ROOT_SIZE=$(df /root/.ollama 2>/dev/null | awk 'NR==2{print $4}')
WS_SIZE=$(df /workspace 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$ROOT_SIZE" ] && [ -n "$WS_SIZE" ] && [ "$WS_SIZE" -gt "$ROOT_SIZE" ] 2>/dev/null; then
    echo "  /workspace has more space — suggest: export OLLAMA_MODELS=/workspace/ollama_models"
fi
echo "  ✓ OK"

# ── Done
echo
echo "══════════════════════════════════════════"
echo "  Dependencies ready."
echo
echo "  Now run the setup wizard:"
echo "    rm -f /tmp/setup.py; curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py && python3 /tmp/setup.py"
echo
echo "  Or pull models first, then run the wizard:"
echo "    ollama pull fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"
echo "    ollama pull snowflake-arctic-embed2:latest"
echo "    ollama pull qwen2.5:0.5b"
echo "    rm -f /tmp/setup.py; curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py && python3 /tmp/setup.py"
echo "══════════════════════════════════════════"

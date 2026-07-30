#!/bin/bash
# setup_pod.sh — one-command Mneme pod setup
# Usage: bash setup_pod.sh [backend_model] [embed_model]
set -e

BACKEND="${1:-fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-262k}"
EMBED="${2:-snowflake-arctic-embed2}"
REPO="https://raw.githubusercontent.com/flyersean/Mneme/main"

echo "=== Mneme Pod Setup ==="
echo "Backend: $BACKEND"
echo "Embed:   $EMBED"

# ── Dependencies ──
echo ">>> Installing Python deps..."
pip install -q flask flask-cors faiss-cpu numpy requests 2>/dev/null || pip install --ignore-installed flask flask-cors faiss-cpu numpy requests

# ── Ollama models ──
echo ">>> Pulling models (this may take a few minutes)..."
ollama pull "$BACKEND"
ollama pull "$EMBED"

# ── Proxy code ──
echo ">>> Downloading proxy code..."
mkdir -p /workspace/proxy /workspace/mneme_chunks
curl -fsSL "$REPO/proxy/mneme_proxy.py" -o /workspace/proxy/mneme_proxy.py

# Patch model name if different from default
if [ "$BACKEND" != "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-262k" ]; then
    sed -i "s|fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-262k|$BACKEND|" /workspace/proxy/mneme_proxy.py
fi

# ── Restore DB if provided ──
if [ -f /workspace/mneme_chunks/mneme.db ]; then
    echo ">>> Existing DB found — keeping it"
else
    echo ">>> Fresh DB — no existing context"
fi

# ── Kill any existing proxy, start fresh ──
echo ">>> Starting proxy..."
pkill -f "python3.*mneme_proxy" 2>/dev/null || true
sleep 1
cd /workspace
nohup python3 -u proxy/mneme_proxy.py > /tmp/mneme_proxy.log 2>&1 &
sleep 4

# ── Verify ──
if curl -s http://localhost:8080/health | grep -q ok; then
    echo ""
    echo "=== Mneme proxy running on :8080 ==="
    echo "Health: $(curl -s http://localhost:8080/health)"
else
    echo "!!! Proxy may not have started. Check /tmp/mneme_proxy.log"
fi

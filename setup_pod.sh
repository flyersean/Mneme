#!/bin/bash
# Mneme — one-command setup
# Run on any Linux pod with Ollama already installed and running.
set -e

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║         Mneme Setup              ║"
echo "  ║  Conversational memory proxy     ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ── Dependencies ──
echo "→ Installing Python dependencies..."
pip install -q flask flask-cors faiss-cpu numpy requests 2>/dev/null || true

# ── Embed model ──
EMBED="snowflake-arctic-embed2"
if ollama list | grep -q "$EMBED"; then
    echo "→ Embed model '$EMBED' already present"
else
    echo "→ Pulling embed model '$EMBED'..."
    ollama pull "$EMBED"
fi

# ── Detect Ollama models ──
echo ""
echo "→ Available Ollama models:"
echo ""
mapfile -t MODELS < <(ollama list | tail -n +2 | awk '{print $1}' | grep -v "$EMBED")
for i in "${!MODELS[@]}"; do
    printf "  %d) %s\n" "$((i+1))" "${MODELS[$i]}"
done

echo ""
read -p "→ Which model to use? [1] " CHOICE
CHOICE="${CHOICE:-1}"
BACKEND="${MODELS[$((CHOICE-1))]}"
echo ""
echo "→ Using: $BACKEND"

# ── Proxy code ──
echo "→ Downloading proxy..."
mkdir -p /workspace/proxy /workspace/mneme_chunks
curl -fsSL "https://raw.githubusercontent.com/flyersean/Mneme/main/proxy/mneme_proxy.py" -o /workspace/proxy/mneme_proxy.py

# Patch model name
sed -i "s|fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-262k|$BACKEND|" /workspace/proxy/mneme_proxy.py

# ── Start ──
echo "→ Starting Mneme proxy..."
pkill -f "python3.*mneme_proxy" 2>/dev/null || true
sleep 1
cd /workspace
nohup python3 -u proxy/mneme_proxy.py > /tmp/mneme_proxy.log 2>&1 &
sleep 4

if curl -s http://localhost:8080/health 2>/dev/null | grep -q ok; then
    echo ""
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║  Mneme proxy running on :8080            ║"
    echo "  ║                                          ║"
    echo "  ║  Endpoint: http://localhost:8080/v1       ║"
    echo "  ║  Health:   http://localhost:8080/health   ║"
    echo "  ║  Save:     curl -X POST :8080/save        ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo ""
    echo "  Agent config:"
    echo "    provider: custom"
    echo "    base_url: http://localhost:8080/v1"
    echo "    model:    text-mneme:64k"
    echo "    api_key:  none"
    echo ""
else
    echo "!!! Proxy may not have started. Check /tmp/mneme_proxy.log"
fi

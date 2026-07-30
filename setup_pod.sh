#!/bin/bash
# Mneme — one-command setup
# Run on any Linux pod with Ollama already installed.
# Usage: curl -fsSL <url> | bash
#   or:  curl -fsSL <url> | bash -s -- my-model:latest
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

# ── Detect backend model ──
echo "→ Detecting available models..."
ALL_MODELS=$(ollama list | tail -n +2 | awk '{print $1}' | grep -v "$EMBED")

if [ -z "$ALL_MODELS" ]; then
    echo "!!! No Ollama models found. Pull one first: ollama pull <model>"
    exit 1
fi

if [ -n "$1" ]; then
    BACKEND="$1"
    echo "→ Using specified model: $BACKEND"
else
    # Read available models into a bash array
    OLDIFS="$IFS"; IFS=$'\n'; MODELS=($ALL_MODELS); IFS="$OLDIFS"
    
    if [ ${#MODELS[@]} -eq 1 ]; then
        BACKEND="${MODELS[0]}"
        echo "→ Only one model found, using: $BACKEND"
    else
        echo ""
        echo "  Available models:"
        for i in "${!MODELS[@]}"; do
            printf "    %d) %s\n" "$((i+1))" "${MODELS[$i]}"
        done
        echo ""
        
        # Try to read from terminal; fall back to model 1 if no TTY
        if [ -t 0 ]; then
            # stdin is a terminal — read normally
            read -p "  Choose [1]: " CHOICE
        elif [ -e /dev/tty ]; then
            # piped input — read from controlling terminal
            read -p "  Choose [1]: " CHOICE < /dev/tty
        else
            # no TTY available (Docker without -it) — default to first
            echo "  (no interactive terminal — using model 1)"
            CHOICE="1"
        fi
        
        CHOICE="${CHOICE:-1}"
        if [ "$CHOICE" -ge 1 ] 2>/dev/null && [ "$CHOICE" -le "${#MODELS[@]}" ] 2>/dev/null; then
            BACKEND="${MODELS[$((CHOICE-1))]}"
        else
            BACKEND="${MODELS[0]}"
        fi
        echo "→ Using: $BACKEND"
    fi
fi

# ── Proxy code ──
echo "→ Downloading proxy..."
mkdir -p /workspace/proxy /workspace/mneme_chunks
curl -fsSL "https://raw.githubusercontent.com/flyersean/Mneme/main/proxy/mneme_proxy.py" -o /workspace/proxy/mneme_proxy.py
# Strip :latest suffix to avoid doubling
BACKEND=$(echo "$BACKEND" | sed 's|:latest$||')
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

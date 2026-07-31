#!/bin/bash
# Mneme — one-command setup
# Run on any Linux pod with Ollama already installed and running.
# Usage: curl -fsSL <url> | bash [-s model_name]
set -e

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║         Mneme Setup              ║"
echo "  ║  Conversational memory proxy     ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ── Prerequisites ──
if ! command -v python3 &>/dev/null; then
    echo "!!! python3 not found. Install it: apt install python3"
    exit 1
fi

if ! command -v pip &>/dev/null && ! command -v pip3 &>/dev/null; then
    echo "!!! pip not found. Install it: apt install python3-pip"
    exit 1
fi

if ! command -v ollama &>/dev/null; then
    echo "!!! ollama not found. Install it: curl -fsSL https://ollama.com/install.sh | sh"
    exit 1
fi

if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "!!! Ollama not running. Start it: ollama serve"
    exit 1
fi

# ── Dependencies ──
echo "→ Installing Python dependencies..."
PIP=$(command -v pip || command -v pip3)
$PIP install --ignore-installed flask flask-cors faiss-cpu numpy requests 2>&1
if [ $? -ne 0 ]; then
    echo "!!! pip install failed."
    exit 1
fi

# ── Embed model ──
EMBED="snowflake-arctic-embed2"
if ollama list | grep -q "$EMBED"; then
    echo "→ Embed model '$EMBED' already present"
else
    echo "→ Pulling embed model '$EMBED'..."
    ollama pull "$EMBED"
fi

# ── Classifier model (tiny, for topic classification) ──
CLASSIFIER="qwen3:0.5b"
if ollama list | grep -q "$CLASSIFIER"; then
    echo "→ Classifier model '$CLASSIFIER' already present"
else
    echo "→ Pulling classifier model '$CLASSIFIER'..."
    ollama pull "$CLASSIFIER"
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
        
        if [ -t 0 ]; then
            read -p "  Choose [1]: " CHOICE
        elif [ -e /dev/tty ]; then
            read -p "  Choose [1]: " CHOICE < /dev/tty
        else
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
echo "→ Downloading proxy with cache buster..."
mkdir -p /workspace/proxy /workspace/mneme_chunks
curl -fsSL "https://raw.githubusercontent.com/flyersean/Mneme/main/proxy/mneme_proxy.py?$(date +%s)" -o /workspace/proxy/mneme_proxy.py

# Verify syntax
if ! python3 -c "import ast; ast.parse(open('/workspace/proxy/mneme_proxy.py').read())" 2>/dev/null; then
    echo "!!! Downloaded proxy has syntax errors. Trying backup commit..."
    curl -fsSL "https://raw.githubusercontent.com/flyersean/Mneme/8a4f462/proxy/mneme_proxy.py" -o /workspace/proxy/mneme_proxy.py
    python3 -c "import ast; ast.parse(open('/workspace/proxy/mneme_proxy.py').read())"
fi

# Patch model name
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

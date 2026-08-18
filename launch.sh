#!/bin/bash
# Mneme + Pi launcher — OpenRouter backend (local machine)
# One command: starts the Mneme proxy in the background, then launches Pi.
# Exiting Pi (or Ctrl+C) stops the proxy automatically.
#
# Idempotent: sources the saved API key and creates the venv if missing.
# To change models/port, override env vars (MNEME_MODEL, EMBED_MODEL,
# LABEL_MODEL, MNEME_PORT, MNEME_CHUNK_DIR) before running.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV="${MNEME_VENV_DIR:-$HOME/mneme-venv}"
KEY_FILE="${MNEME_KEY_FILE:-$HOME/.mneme/openrouter.env}"
HERMES_ENV="$HOME/.hermes/profiles/deep1/.env"
PORT="${MNEME_PORT:-8080}"
CHUNK_DIR="${MNEME_CHUNK_DIR:-$HOME/mneme_chunks}"

# --- 1. API key: env var, then saved key file, then Hermes .env ---
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$KEY_FILE" ]; then
  OPENROUTER_API_KEY=$(grep -iE '^OPENROUTER_API_KEY=' "$KEY_FILE" | head -1 | cut -d= -f2-)
fi
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$HERMES_ENV" ]; then
  OPENROUTER_API_KEY=$(grep -iE '^OPENROUTER_API_KEY=' "$HERMES_ENV" | head -1 | cut -d= -f2-)
fi
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "No OpenRouter API key found. Run the one-time setup:" >&2
  echo "  python3 $REPO/scripts/mneme_setup_openrouter.py" >&2
  exit 1
fi

# --- 2. venv ---
if [ ! -x "$VENV/bin/python" ]; then
  echo "venv not found — creating $VENV (one-time, ~250MB)..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --no-cache-dir faiss-cpu numpy flask flask-cors requests
fi

# --- 3. Start the proxy in the background ---
export OPENROUTER_API_KEY
export MNEME_BACKEND=openrouter
export MNEME_MODEL="${MNEME_MODEL:-deepseek/deepseek-v4-flash}"
export EMBED_MODEL="${EMBED_MODEL:-voyageai/voyage-4-lite}"
export LABEL_MODEL="${LABEL_MODEL:-meta-llama/llama-3.2-3b-instruct}"
export MNEME_CHUNK_DIR="$CHUNK_DIR"
export MNEME_PORT="$PORT"
export MNEME_CTX_TOKENS="${MNEME_CTX_TOKENS:-256000}"
export MNEME_CHAT_TIMEOUT="${MNEME_CHAT_TIMEOUT:-120}"
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$CHUNK_DIR"
"$VENV/bin/python" -uB "$REPO/proxy/mneme_proxy.py" > "$CHUNK_DIR/mneme.log" 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT INT TERM
echo "Mneme proxy starting (pid $PROXY_PID, port $PORT, log $CHUNK_DIR/mneme.log)..."

# --- 4. Wait for it to be healthy ---
ok=0
for _ in $(seq 1 40); do
  if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  echo "Proxy didn't come up in 40s — tail $CHUNK_DIR/mneme.log" >&2
  exit 1
fi
echo "✓ proxy healthy."

# --- 5. Launch Pi ---
if ! command -v pi >/dev/null 2>&1; then
  echo "Pi not found. Install: npm install -g @earendil-works/pi-coding-agent" >&2
  echo "(Leaving the proxy running — stop it with: kill $PROXY_PID)" >&2
  exit 1
fi
echo "Launching Pi (exit Pi to stop the proxy)..."
pi --provider mneme --model text-mneme:64k \
  --extension "$REPO/extensions/pi/mneme-search-tool.ts" \
  --extension "$REPO/extensions/pi/mneme-web-tools.ts" || true

echo "Proxy stopped."

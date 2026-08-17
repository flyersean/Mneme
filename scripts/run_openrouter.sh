#!/bin/bash
# Run Mneme with the OpenRouter backend — fully hosted, no local Ollama.
#
# Usage:
#   export OPENROUTER_API_KEY=$(grep -iE '^OPENROUTER_API_KEY=' ~/.hermes/profiles/deep1/.env | cut -d= -f2-)
#   scripts/run_openrouter.sh
#
# All model IDs are overridable via env:
#   MNEME_MODEL    (main LLM)          default: deepseek/deepseek-v4-flash
#   EMBED_MODEL    (embedder)          default: voyageai/voyage-4-lite   (1024-dim)
#   LABEL_MODEL    (topic labeler)     default: meta-llama/llama-3.2-3b-instruct (non-thinking)
#   MNEME_CHUNK_DIR (memory DB dir)    default: ~/mneme_chunks
#   MNEME_PORT      (proxy port)       default: 8080
set -euo pipefail

export MNEME_BACKEND="${MNEME_BACKEND:-openrouter}"
export MNEME_MODEL="${MNEME_MODEL:-deepseek/deepseek-v4-flash}"
export EMBED_MODEL="${EMBED_MODEL:-voyageai/voyage-4-lite}"
export LABEL_MODEL="${LABEL_MODEL:-meta-llama/llama-3.2-3b-instruct}"
export MNEME_CHUNK_DIR="${MNEME_CHUNK_DIR:-$HOME/mneme_chunks}"
export MNEME_PORT="${MNEME_PORT:-8080}"
export MNEME_CTX_TOKENS="${MNEME_CTX_TOKENS:-256000}"   # context trim budget (tokens)
export MNEME_CHAT_TIMEOUT="${MNEME_CHAT_TIMEOUT:-600}"   # seconds, anti-grind guardrail
export PYTHONDONTWRITEBYTECODE=1

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "OPENROUTER_API_KEY is not set. Source it first (see header)." >&2
  exit 1
fi

cd "$(dirname "$0")/.."

# Prefer the local venv if it exists (the openrouter branch needs faiss/numpy/flask/requests)
if [ -x "$HOME/mneme-venv/bin/python" ]; then
  PY="$HOME/mneme-venv/bin/python"
else
  PY="python3"
fi

exec "$PY" -uB proxy/mneme_proxy.py

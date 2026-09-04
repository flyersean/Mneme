#!/bin/bash
# ============================================================================
#  Mneme — unified installer (one command, every environment)
# ============================================================================
#  Installs the three things the proxy needs, idempotently and with no prompts:
#    1. Python dependencies (flask / faiss / numpy / requests / pyyaml)
#    2. Ollama (installed + started — harmless even if you use a hosted backend)
#    3. The proxy code (cloned into ~/mneme/repo, branch from MNEME_BRANCH)
#
#  Run it once, then run the setup wizard to choose your backend and models:
#    curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/install.sh | bash
#    curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/mneme_setup.py && python3 /tmp/setup.py
#
#  Pass the branch explicitly when it's not unified_mneme:
#    curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/install.sh | MNEME_BRANCH=main bash
#
#  Safe to re-run — every step checks first and only fills in what's missing.
# ============================================================================
set -e

# Which repo branch to install. The README passes this (main vs unified_mneme);
# it drives the self-update URL, the clone, and the tarball fallback so that
# following the `main` README installs the memory-only build and following the
# `unified_mneme` README installs the full build.
BRANCH="${MNEME_BRANCH:-unified_mneme}"

# Self-update: always run the latest version from the repo (cache-busted).
if [ -z "${MNEME_INSTALL_UPDATED:-}" ]; then
  export MNEME_INSTALL_UPDATED=1
  _URL="https://raw.githubusercontent.com/flyersean/Mneme/$BRANCH/scripts/install.sh"
  if curl -sSL --fail -o /tmp/mneme_install.sh "$_URL?$(date +%s)" 2>/dev/null && [ -s /tmp/mneme_install.sh ]; then
    exec bash /tmp/mneme_install.sh
  fi
  echo "⚠ self-update failed — running the bundled version (network may be slow)."
fi

echo "=== Mneme installer ==="

# ── OS detection
if [ -f /etc/os-release ]; then . /etc/os-release; DISTRO=$ID; else DISTRO="unknown"; fi
echo "  distro: $DISTRO"

# ── 1. Python dependencies ────────────────────────────────────────────
echo; echo "[1/3] Python dependencies"

# Remove system packages that conflict with the pip versions (a known pod/laptop
# gotcha: apt's python3-flask pins old werkzeug/blinker that break the proxy).
apt-get remove -y -qq python3-flask python3-flask-cors python3-werkzeug python3-blinker 2>/dev/null || true

# Install from pip. --break-system-packages handles PEP 668 (Ubuntu 22.04+).
# --ignore-installed bypasses any lingering pinned system packages.
if python3 -m pip install --break-system-packages --ignore-installed flask flask-cors faiss-cpu numpy requests pyyaml ddgs 2>/dev/null; then
  echo "  ✓ pip install OK"
else
  echo "  pip (--break-system-packages) failed — retrying plain install..."
  python3 -m pip install flask flask-cors faiss-cpu numpy requests pyyaml ddgs
fi

# Verify each package imports.
MISSED=""
for pkg in flask flask_cors faiss numpy requests yaml ddgs; do
  if python3 -c "import $pkg" 2>/dev/null; then echo "  ✓ $pkg"; else echo "  ✗ $pkg missing"; MISSED="$MISSED $pkg"; fi
done
if [ -n "$MISSED" ]; then
  echo "  ⚠ Still missing:$MISSED"
  echo "    Install manually: pip install --break-system-packages flask flask-cors faiss-cpu numpy requests pyyaml ddgs"
fi

# ── 2. Ollama ─────────────────────────────────────────────────────────
echo; echo "[2/3] Ollama"

# Disable flash attention. Some vision-patched GGUF models (e.g. the HauhauCS
# Qwen3.6-35B) crash with "CUDA error: an illegal memory access was encountered"
# on prompts longer than ~1-2k tokens when flash attention is on. Off = stable
# decode at the cost of a little speed/memory. Set before starting ollama.
export OLLAMA_FLASH_ATTENTION=0
# Keep models resident in VRAM (never unload). Default is 5m — a turn after an
# idle gap then pays a 30-60s reload of the 27GB model, which can exceed the
# proxy's first-token timeout. -1 = stay loaded until the pod shuts down.
export OLLAMA_KEEP_ALIVE=-1
# Spread models across all available GPUs instead of packing them onto the first
# one. On a multi-GPU pod (e.g. 2×A40), the default scheduler fits every model on
# GPU 0 as long as they collectively fit, leaving the other GPU idle at 0%.
# =1 balances the swarm across GPUs.
export OLLAMA_SCHED_SPREAD=1

if command -v ollama >/dev/null 2>&1; then
  _VER=$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  echo "  ✓ ollama found (${_VER:-unknown})"
else
  echo "  installing Ollama..."
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq zstd curl 2>/dev/null || true
  curl -fsSL https://ollama.com/install.sh | sh
  command -v ollama >/dev/null 2>&1 && echo "  ✓ ollama installed" || echo "  ⚠ install failed — run: curl -fsSL https://ollama.com/install.sh | sh"
fi

# Pin keep-alive as the server DEFAULT via a systemd drop-in. The exports above
# only reach a `nohup ollama serve` child; the official installer runs Ollama as
# a systemd service with a clean environment, so without this the running server
# keeps the 5m default and unloads models between swarm steps. Idempotent.
if systemctl cat ollama.service >/dev/null 2>&1; then
  mkdir -p /etc/systemd/system/ollama.service.d
  cat > /etc/systemd/system/ollama.service.d/10-mneme.conf <<'EOF'
[Service]
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_FLASH_ATTENTION=0
Environment=OLLAMA_SCHED_SPREAD=1
EOF
  systemctl daemon-reload
  systemctl restart ollama 2>/dev/null || systemctl start ollama 2>/dev/null || true
  # Wait for the restarted service to come back before the "is it answering"
  # check below, so we don't fire a competing nohup instance on a port race.
  for _ in $(seq 1 20); do
    curl -s --max-time 2 http://localhost:11434 >/dev/null 2>&1 && break
    sleep 1
  done
  echo "  ✓ ollama systemd drop-in written (keep_alive=-1, flash_attention=0) + restarted"
else
  echo "  (no ollama systemd unit — keep-alive relies on the env export above)"
fi

# Start Ollama if it isn't answering.
if ! curl -s --max-time 2 http://localhost:11434 >/dev/null 2>&1; then
  echo "  starting ollama serve..."
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  for _ in $(seq 1 20); do
    curl -s --max-time 2 http://localhost:11434 >/dev/null 2>&1 && { echo "  ✓ ollama ready"; break; }
    sleep 1
  done
else
  echo "  ✓ ollama already running"
fi

# ── 3. Proxy code ─────────────────────────────────────────────────────
echo; echo "[3/3] Proxy code ($BRANCH)"

REPO_DIR="${MNEME_REPO_DIR:-$HOME/mneme/repo}"
if [ -f "$REPO_DIR/proxy/mneme_proxy.py" ]; then
  echo "  repo already at $REPO_DIR — leaving it (run setup to reconfigure)"
else
  echo "  downloading into $REPO_DIR ..."
  mkdir -p "$(dirname "$REPO_DIR")"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 -b "$BRANCH" https://github.com/flyersean/Mneme.git "$REPO_DIR"
  else
    echo "  git not found — downloading tarball..."
    mkdir -p "$REPO_DIR"
    curl -sSL --fail "https://codeload.github.com/flyersean/Mneme/tar.gz/refs/heads/$BRANCH" | tar xz -C "$REPO_DIR" --strip-components=1
  fi
  if [ -f "$REPO_DIR/proxy/mneme_proxy.py" ]; then
    echo "  ✓ proxy code ready"
  else
    echo "  ✗ FAILED to download proxy code — check network and re-run." >&2
    exit 1
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════════════"
echo "  Install complete."
echo
echo "  Next, run the setup wizard to pick your backend and models:"
echo "    curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/$BRANCH/scripts/mneme_setup.py && python3 /tmp/setup.py"
echo
echo "  (Setup asks: OpenRouter or Ollama → models → Pi yes/no → port.)"
echo "══════════════════════════════════════════════════════════════"

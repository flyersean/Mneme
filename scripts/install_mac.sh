#!/bin/bash
# ============================================================================
#  Mneme — macOS installer   ⚠ UNTESTED — no Mac was available to verify this ⚠
# ============================================================================
#  Installs the proxy's Python dependencies + the repo, idempotently, then
#  offers the BIG optional components one at a time (same as the Linux
#  installer — each auto-skipped when already present, otherwise asked [y/N]):
#    1. Python dependencies  (flask / faiss / numpy / requests / pyyaml / ddgs /
#       mcp / playwright / patchright) — always installed
#    2. Ollama        (local model backend)  — asked y/N, skipped if present
#    3. Chromium      (headless browser)      — asked y/N, skipped if present
#    4. Hound MCP     (web stack + OCR/PDF)   — asked y/N, skipped if present
#    then clones the proxy code into ~/mneme/repo (branch from MNEME_BRANCH).
#
#  KNOWN macOS DIFFERENCES vs the Linux installer (these are the untested bits):
#    - No apt-get / systemd. Python deps come from pip; Ollama comes from the
#      official installer (it ships a native macOS app); Chromium comes from
#      playwright/patchright (both support macOS).
#    - Ollama is NOT auto-started as a service — after install, run
#      `ollama serve` (or launch the Ollama app) before the setup wizard pulls
#      a model.
#    - The Linux systemd keep-alive / flash-attention drop-in does not exist on
#      macOS; those are set per-session with `export` if you need them.
#
#  Run it, then run the setup wizard:
#    curl -sSL -o /tmp/install_mac.sh https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/install_mac.sh && bash /tmp/install_mac.sh
#    curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/mneme_setup.py && python3 /tmp/setup.py
#
#  Flags (same as the Linux installer):
#    MNEME_YES=1                      install all optional components
#    MNEME_INSTALL_OLLAMA / _CHROMIUM / _HOUND =1|0
# ============================================================================
set -e

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer is for macOS. For Linux use scripts/install.sh." >&2
  exit 1
fi

BRANCH="${MNEME_BRANCH:-unified_mneme}"

# Self-update: always run the latest version from the repo (cache-busted).
if [ -z "${MNEME_INSTALL_UPDATED:-}" ]; then
  export MNEME_INSTALL_UPDATED=1
  _URL="https://raw.githubusercontent.com/flyersean/Mneme/$BRANCH/scripts/install_mac.sh"
  if curl -sSL --fail -o /tmp/mneme_install_mac.sh "$_URL?$(date +%s)" 2>/dev/null && [ -s /tmp/mneme_install_mac.sh ]; then
    exec bash /tmp/mneme_install_mac.sh
  fi
  echo "⚠ self-update failed — running the bundled version (network may be slow)."
fi

echo "=== Mneme installer (macOS — untested) ==="

# ── Optional-component prompts (same helper as the Linux installer) ──────
answer_yn() {
  local _ev="$1" _q="$2"
  if [ -n "${!_ev}" ]; then
    case "${!_ev}" in 1|y|Y|yes|YES|true|True) return 0;; *) return 1;; esac
  fi
  if [ -n "${MNEME_YES:-}" ]; then
    case "$MNEME_YES" in 1|y|Y|yes|YES|true|True) return 0;; *) return 1;; esac
  fi
  if [ -t 0 ]; then
    local _a
    printf "%s [y/N] " "$_q"
    read -r _a
    case "$_a" in [Yy]*) return 0;; *) return 1;; esac
  fi
  return 1
}

chromium_present() {
  [ -d "$HOME/Library/Caches/ms-playwright" ] && ls "$HOME/Library/Caches/ms-playwright" 2>/dev/null | grep -qi chromium
}

# ── 1. Python dependencies ────────────────────────────────────────────
echo; echo "[1/4] Python dependencies"
# macOS system/Homebrew python is often externally-managed (PEP 668); try
# --break-system-packages first, then fall back to a --user install.
if python3 -m pip install --break-system-packages flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright 2>/dev/null; then
  echo "  ✓ pip install OK"
else
  echo "  pip (--break-system-packages) failed — trying --user install..."
  python3 -m pip install --user flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright || true
fi

MISSED=""
for pkg in flask flask_cors faiss numpy requests yaml ddgs mcp playwright patchright; do
  if python3 -c "import $pkg" 2>/dev/null; then echo "  ✓ $pkg"; else echo "  ✗ $pkg missing"; MISSED="$MISSED $pkg"; fi
done
if [ -n "$MISSED" ]; then
  echo "  ⚠ Still missing:$MISSED"
  echo "    Install manually: python3 -m pip install flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright"
fi

# ── 2. Chromium (opt-in) ──────────────────────────────────────────────
echo; echo "[2/4] Chromium (browser tools)"
if ! (python3 -c "import patchright" 2>/dev/null || python3 -c "import playwright" 2>/dev/null); then
  echo "  ⚠ playwright/patchright not importable — skipping Chromium (browser tools need: pip install playwright patchright)"
elif chromium_present; then
  echo "  ✓ headless Chromium already downloaded — skipping"
elif answer_yn MNEME_INSTALL_CHROMIUM "Install headless Chromium (browser tools, ~150MB each)?"; then
  echo "  installing browser Chromium (idempotent, ~150MB each)..."
  python3 -m patchright install chromium 2>/dev/null || true
  python3 -m playwright install chromium 2>/dev/null || true
  echo "  ✓ browser chromium ready (patchright + playwright)"
else
  echo "  ⓘ skipping Chromium — web tools fetch without JS rendering"
fi

# ── 3. Hound MCP (opt-in) ─────────────────────────────────────────────
echo; echo "[3/4] Hound MCP (web/OCR/crawl stack)"
if command -v hound >/dev/null 2>&1; then
  echo "  ✓ hound CLI already installed — skipping"
elif answer_yn MNEME_INSTALL_HOUND "Install Hound MCP (web/OCR/crawl stack)?"; then
  python3 -m pip install --break-system-packages "hound-mcp[all]" \
    || python3 -m pip install --user "hound-mcp[all]" \
    || echo "  ⚠ hound-mcp[all] install failed — web/OCR/crawl tools unavailable until fixed."
  command -v hound >/dev/null 2>&1 && echo "  ✓ hound CLI ready" || echo "  ⚠ hound CLI not on PATH (find it: python3 -m pip show -f hound-mcp | grep -i hound)"
else
  echo "  ⓘ skipping Hound MCP — web tools fall back to the built-in fetch_url"
fi

# ── 4. Ollama (opt-in) ────────────────────────────────────────────────
echo; echo "[4/4] Ollama"
export OLLAMA_FLASH_ATTENTION=0
export OLLAMA_KEEP_ALIVE=-1
export OLLAMA_SCHED_SPREAD=1

if command -v ollama >/dev/null 2>&1; then
  _VER=$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  echo "  ✓ ollama already installed (${_VER:-unknown})"
elif answer_yn MNEME_INSTALL_OLLAMA "Install Ollama (local model backend, ~1GB+ download)?"; then
  echo "  installing Ollama (native macOS app)..."
  # The official installer supports macOS directly (no zstd/systemd needed here).
  curl -fsSL https://ollama.com/install.sh | sh
  command -v ollama >/dev/null 2>&1 && echo "  ✓ ollama installed" || echo "  ⚠ install failed — run: curl -fsSL https://ollama.com/install.sh | sh"
  echo "  ⓘ On macOS Ollama is not started as a service — run \`ollama serve\` (or open the Ollama app) before pulling a model."
else
  echo "  ⓘ skipping Ollama — a hosted backend (OpenRouter/Routeway/…) doesn't need it"
fi

# ── 5. Proxy code ─────────────────────────────────────────────────────
echo; echo "[5/5] Proxy code ($BRANCH)"
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
echo "  Install complete (macOS — untested)."
echo
echo "  Next, run the setup wizard to pick your backend and models:"
echo "    curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/$BRANCH/scripts/mneme_setup.py && python3 /tmp/setup.py"
echo "══════════════════════════════════════════════════════════════"

#!/bin/bash
# ============================================================================
#  Mneme — unified installer (one command, every environment)
# ============================================================================
#  Installs the three things the proxy needs, idempotently and with no prompts:
#    1. Python dependencies + browser engine + Hound MCP (flask / faiss / numpy /
#       requests / pyyaml / playwright / patchright / hound-mcp[all])
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

# ── Privilege model ──────────────────────────────────────────────────
# Some steps are privileged: removing conflicting apt packages, installing a
# zstd binary to /usr/local/bin, and writing the Ollama systemd drop-in. Under
# `set -e` those would ABORT the whole install for a non-root user (the README
# says a laptop is a valid host, and laptops are usually non-root).
#
# So: detect root once, and let privileged steps SKIP with a clear message
# instead of failing. Nothing here is required for the proxy to run — the drop-in
# only pins keep-alive, the zstd stub only matters for the tarball fallback, and
# the apt removal only clears packages that conflict with pip's versions.
if [ "$(id -u)" -eq 0 ]; then
  MNEME_HAVE_ROOT=1
else
  MNEME_HAVE_ROOT=0
  echo "  ⓘ Not running as root — skipping privileged steps (apt cleanup,"
  echo "    systemd drop-in, /usr/local/bin). The proxy itself does not need them."
  echo "    Re-run with sudo if you want those applied."
fi

# Run a privileged command only as root; otherwise report and continue.
maybe_root() {
  if [ "$MNEME_HAVE_ROOT" -eq 1 ]; then
    "$@" || true
  else
    return 1
  fi
}

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
# Root-only, and skipped entirely on a non-root install — if these packages are
# present the pip install below still shadows them via --ignore-installed.
if [ "$MNEME_HAVE_ROOT" -eq 1 ]; then
  apt-get remove -y -qq python3-flask python3-flask-cors python3-werkzeug python3-blinker 2>/dev/null || true
else
  echo "  ⓘ Skipping apt removal (not root) — pip will shadow any system packages."
fi

# Install from pip. --break-system-packages handles PEP 668 (Ubuntu 22.04+).
# --ignore-installed bypasses any lingering pinned system packages.
if python3 -m pip install --break-system-packages --ignore-installed flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright 2>/dev/null; then
  echo "  ✓ pip install OK"
else
  echo "  pip (--break-system-packages) failed — retrying plain install..."
  python3 -m pip install flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright
fi

# Verify each package imports.
MISSED=""
for pkg in flask flask_cors faiss numpy requests yaml ddgs mcp playwright patchright; do
  if python3 -c "import $pkg" 2>/dev/null; then echo "  ✓ $pkg"; else echo "  ✗ $pkg missing"; MISSED="$MISSED $pkg"; fi
done
if [ -n "$MISSED" ]; then
  echo "  ⚠ Still missing:$MISSED"
  echo "    Install manually: pip install --break-system-packages flask flask-cors faiss-cpu numpy requests pyyaml ddgs mcp playwright patchright"
fi

# Browser engines for web tools. Hound (the bundled web MCP) launches its browser
# through PATCHRIGHT — a Playwright fork with anti-detection patches — so its
# Chromium comes from `patchright install chromium`, NOT plain playwright. We
# install both pip packages (patchright is what Hound uses; playwright covers any
# other tool) and download both Chromium binaries. A headless pod is missing the
# shared libraries the browser needs, so refresh apt lists first, then install
# the system deps (best-effort — a host that can't apt still gets the binaries).
if python3 -c "import patchright" 2>/dev/null || python3 -c "import playwright" 2>/dev/null; then
  echo "  installing browser Chromium (idempotent, ~150MB each)..."
  apt-get update -qq 2>/dev/null || true
  python3 -m patchright install chromium 2>/dev/null || true
  python3 -m patchright install-deps chromium 2>/dev/null || true
  python3 -m playwright install chromium 2>/dev/null || true
  python3 -m playwright install-deps chromium 2>/dev/null || true
  echo "  ✓ browser chromium ready (patchright + playwright)"
else
  echo "  ⚠ patchright/playwright not importable — skipping browser install (install manually: pip install playwright patchright && patchright install chromium)"
fi

# ── 1c. Hound MCP (full) ─────────────────────────────────────────────
# Hound — the local, keyless web stack (fetch/search/crawl/screenshot/PDF/OCR).
# The [all] extra adds the browser stack (browserforge) + OCR/PDF (rapidocr,
# onnxruntime, pdfplumber, pypdfium2, tokenizers) on top of the patchright/
# playwright packages installed above. Its `hound` CLI lands in the same bin as
# python3's pip, so a proxy MCP entry `command: hound` resolves with no extra
# PATH setup. (Hardcoded for now — becomes an optional-dependency checkbox later.)
echo; echo "[1c/3] Hound MCP (full web stack)"
python3 -m pip install --break-system-packages "hound-mcp[all]" 2>/dev/null \
  || python3 -m pip install "hound-mcp[all]"
if command -v hound >/dev/null 2>&1; then
  echo "  ✓ hound CLI ready ($(hound --version 2>/dev/null | head -1))"
else
  echo "  ⚠ hound CLI not on PATH — the proxy MCP entry \`command: hound\` needs it."
  echo "    locate it: python3 -m pip show -f hound-mcp | grep -i hound"
fi

# ── 2. Ollama ─────────────────────────────────────────────────────────
echo; echo "[2/3] Ollama"

# Flash attention OFF by default, set consistently in BOTH places below.
#
# Why OFF: some vision-patched GGUF models (e.g. the HauhauCS Qwen3.6-35B) crash
# with "CUDA error: an illegal memory access was encountered" on prompts longer
# than ~1-2k tokens when flash attention is ON. A crash mid-swarm-step is worse
# than a slower decode, so the default is the safe one.
#
# Turning it ON is a deliberate speed/memory win — faster decode, lower VRAM,
# useful for fitting a big model on one GPU. Do it ONLY after confirming your
# model does not hit the crash, and set it in the systemd drop-in below (that is
# what the running service actually reads) as well as here.
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
  # Ollama's installer now serves a zstd-compressed tarball and refuses to run
  # without a `zstd` binary. apt-get can fail silently (stale package lists, or
  # apt repos blocked on some pods), so install zstd first and VERIFY it landed.
  # If apt can't provide it, fall back to a Python `zstandard`-based zstd shim —
  # that needs only pip (prebuilt wheel, no compiler) and no package manager.
  if ! command -v zstd >/dev/null 2>&1; then
    echo "  installing zstd (required by Ollama's installer)..."
    if [ "$MNEME_HAVE_ROOT" -eq 1 ]; then
      for _ in 1 2 3; do
        apt-get update -qq 2>/dev/null && break
        sleep 2
      done
      apt-get install -y -qq zstd curl 2>/dev/null || true
    else
      echo "    (not root — apt install skipped)"
    fi
  fi
  if ! command -v zstd >/dev/null 2>&1; then
    echo "  apt has no zstd — installing a Python zstandard shim instead..."
    python3 -m pip install --break-system-packages --quiet zstandard 2>/dev/null \
      || python3 -m pip install --quiet zstandard 2>/dev/null || true
    if python3 -c "import zstandard" 2>/dev/null; then
      # Write the shim somewhere writable: /usr/local/bin needs root, so fall
      # back to ~/.local/bin (on PATH for most users) otherwise. Without this a
      # non-root install under `set -e` died writing to /usr/local/bin.
      if [ "$MNEME_HAVE_ROOT" -eq 1 ]; then
        _ZSTD_DIR=/usr/local/bin
      else
        _ZSTD_DIR="$HOME/.local/bin"
        mkdir -p "$_ZSTD_DIR"
      fi
      cat > "$_ZSTD_DIR/zstd" <<'EOF'
#!/usr/bin/env python3
# Minimal zstd shim (decompress stdin -> stdout), enough for Ollama's installer.
import sys
import zstandard
zstandard.ZstdDecompressor().copy_stream(sys.stdin.buffer, sys.stdout.buffer)
EOF
      chmod +x "$_ZSTD_DIR/zstd"
      export PATH="$_ZSTD_DIR:$PATH"
    fi
  fi
  if ! command -v zstd >/dev/null 2>&1; then
    echo "  ✗ zstd is missing and could not be installed — Ollama's installer needs it to extract its tarball." >&2
    echo "    Install it manually, then re-run this installer:" >&2
    echo "      apt-get update && apt-get install -y zstd" >&2
    exit 1
  fi
  echo "  ✓ zstd ready"
  curl -fsSL https://ollama.com/install.sh | sh
  command -v ollama >/dev/null 2>&1 && echo "  ✓ ollama installed" || echo "  ⚠ install failed — run: curl -fsSL https://ollama.com/install.sh | sh"
fi

# Pin keep-alive as the server DEFAULT via a systemd drop-in. The exports above
# only reach a `nohup ollama serve` child; the official installer runs Ollama as
# a systemd service with a clean environment, so without this the running server
# keeps the 5m default and unloads models between swarm steps. Idempotent.
if systemctl cat ollama.service >/dev/null 2>&1; then
  if [ "$MNEME_HAVE_ROOT" -eq 1 ]; then
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
    echo "  ⓘ systemd drop-in skipped (not root). Ollama will use its 5m keep-alive"
    echo "    default, so models unload between swarm steps. To pin it later:"
    echo "      sudo mkdir -p /etc/systemd/system/ollama.service.d"
    echo "      # add 10-mneme.conf with OLLAMA_KEEP_ALIVE=-1, then: sudo systemctl daemon-reload"
  fi
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

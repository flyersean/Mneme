#!/bin/bash
# Mneme one-line setup (kimi build):
#   curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/kimi_setup.sh | bash
set -e

REPO="https://github.com/flyersean/Mneme.git"
BRANCH="dev-chunks"
RAW="https://raw.githubusercontent.com/flyersean/Mneme/${BRANCH}"

echo "=== Mneme Setup ==="

# 1. Get proxy code into /workspace/proxy
mkdir -p /workspace/proxy
if [ ! -f /workspace/proxy/mneme_proxy.py ]; then
    echo "Fetching proxy code..."
    if command -v git >/dev/null 2>&1; then
        rm -rf /tmp/mneme_repo
        if git clone -b "${BRANCH}" "${REPO}" /tmp/mneme_repo 2>/dev/null; then
            cp /tmp/mneme_repo/proxy/* /workspace/proxy/
        fi
    fi
    # Fallback: raw file download (no git, or clone failed)
    if [ ! -f /workspace/proxy/mneme_proxy.py ]; then
        echo "git unavailable or clone failed — downloading raw files..."
        curl -sSL -o /workspace/proxy/mneme_proxy.py   "${RAW}/proxy/mneme_proxy.py"
        curl -sSL -o /workspace/proxy/system_prompt.md "${RAW}/proxy/system_prompt.md"
    fi
fi

# 2. Download latest wizard and run with the real terminal as stdin.
#    curl|bash leaves stdin as the pipe, which input() can't use.
curl -sSL -o /tmp/mneme_setup.py "${RAW}/scripts/kimi_setup.py"

if [ -e /dev/tty ]; then
    python3 /tmp/mneme_setup.py < /dev/tty
else
    # No controlling terminal (Docker without -it) — wizard uses defaults
    python3 /tmp/mneme_setup.py
fi

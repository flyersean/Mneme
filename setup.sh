#!/bin/bash
# Mneme one-line setup: curl -sSL <url> | bash
set -e

echo "=== Mneme Setup ==="

# Clone Mneme if not present
if [ ! -d "/workspace/mneme" ]; then
    echo "Cloning Mneme..."
    git clone https://github.com/flyersean/Mneme.git /workspace/mneme 2>/dev/null || {
        mkdir -p /workspace/mneme/proxy /workspace/mneme/scripts
        curl -sSL -o /workspace/mneme/proxy/mneme_proxy.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/mneme_proxy.py
        curl -sSL -o /workspace/mneme/proxy/system_prompt.md https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/system_prompt.md
        curl -sSL -o /workspace/mneme/scripts/mneme_setup.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py
    }
fi

# Copy proxy to workspace
cp /workspace/mneme/proxy/* /workspace/proxy/ 2>/dev/null || {
    mkdir -p /workspace/proxy
    cp /workspace/mneme/proxy/* /workspace/proxy/
}

# Fetch latest wizard and run with terminal stdin (curl|bash closes fd 0)
curl -sSL -o /tmp/mneme_setup.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py
python3 /tmp/mneme_setup.py < /dev/tty

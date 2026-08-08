#!/bin/bash
# Mneme one-line setup: curl -sSL <url> | bash
set -e

echo "=== Mneme Setup ==="

# Install Python deps for setup
python3 -m pip install questionary -q 2>/dev/null || pip install questionary -q

# Clone Mneme if not present
if [ ! -d "/workspace/mneme" ]; then
    echo "Cloning Mneme..."
    git clone https://github.com/flyersean/Mneme.git /workspace/mneme 2>/dev/null || {
        mkdir -p /workspace/mneme/proxy
        curl -sSL -o /workspace/mneme/proxy/mneme_proxy.py https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/mneme_proxy.py
        curl -sSL -o /workspace/mneme/proxy/system_prompt.md https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/proxy/system_prompt.md
    }
fi

# Copy proxy to workspace
cp /workspace/mneme/proxy/* /workspace/proxy/ 2>/dev/null || {
    mkdir -p /workspace/proxy
    cp /workspace/mneme/proxy/* /workspace/proxy/
}

# Download and run the setup wizard
SETUP_URL="https://raw.githubusercontent.com/flyersean/Mneme/dev-chunks/scripts/mneme_setup.py"
if [ -f "/workspace/mneme/scripts/mneme_setup.py" ]; then
    python3 /workspace/mneme/scripts/mneme_setup.py
elif curl -s --max-time 10 -o /tmp/mneme_setup.py "$SETUP_URL" 2>/dev/null; then
    python3 /tmp/mneme_setup.py
else
    echo "No setup wizard found. Proxy files are at /workspace/proxy/"
    echo "Start manually: MNEME_MODEL=<model> python3 proxy/mneme_proxy.py"
fi

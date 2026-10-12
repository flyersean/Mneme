#!/bin/bash
# Fetch the models.dev registry into the Mneme cache so provider/model metadata
# (base URLs, key env vars, context windows, reasoning support) resolves without a
# hand-written catalog entry. Re-run to refresh; the proxy reads it at startup.
# Source: https://models.dev (MIT, free). See proxy/mneme/models_dev.py.
set -euo pipefail
DEST="${1:-/home/ubuntu/mneme/chunks/models-dev.api.json}"
echo "Fetching models.dev -> $DEST"
curl -fsSL --max-time 60 https://models.dev/api.json -o "$DEST.tmp"
mv "$DEST.tmp" "$DEST"
echo "OK: $(wc -c < "$DEST") bytes"

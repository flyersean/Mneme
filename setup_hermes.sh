#!/bin/bash
# setup_hermes.sh — one-command Hermes install + Mneme config
# Run on any machine with Hermes Agent installed.
set -e

echo "=== Configuring Hermes for Mneme proxy ==="
hermes config set model.default text-mneme:64k
hermes config set model.provider custom
hermes config set model.base_url http://localhost:8080/v1
hermes config set model.name text-mneme:64k
hermes config set model.api_key none
hermes config set memory.memory_enabled false
hermes config set memory.user_profile_enabled false
hermes config set compression.enabled false

echo "=== Done. Start the SSH tunnel, then: hermes chat ==="

#!/bin/bash
# Mneme Gateway — one reverse proxy fronting every Mneme proxy instance.
#
# Binds MNEME_GATEWAY_PORT (internal). On RunPod the reserved port is the one
# that forwards here: RunPod's nginx listens on 8001 and proxies to
# localhost:8000, so the gateway binds 8000 and is externally reachable at the
# reserved 8001 (https://<pod>-8001.proxy.runpod.net).
#
# Env:
#   MNEME_CHUNK_DIR     shared dir holding instances/   (default ~/mneme/chunks)
#   MNEME_GATEWAY_HOST  bind address                    (default 127.0.0.1)
#   MNEME_GATEWAY_PORT  internal port                   (default 8000)
#   MNEME_GATEWAY_TOKEN optional bearer token; set it to require auth (default off)
set -e
cd "$(dirname "$0")/.."

: "${MNEME_CHUNK_DIR:=$HOME/mneme/chunks}"
: "${MNEME_GATEWAY_HOST:=127.0.0.1}"
: "${MNEME_GATEWAY_PORT:=8000}"
export MNEME_CHUNK_DIR MNEME_GATEWAY_HOST MNEME_GATEWAY_PORT

exec python3 proxy/gateway.py

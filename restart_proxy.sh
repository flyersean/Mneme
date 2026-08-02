#!/bin/bash
fuser -k 8080/tcp 2>/dev/null
sleep 2
rm -rf /workspace/proxy/__pycache__
> /tmp/mneme_proxy.log
cd /workspace
nohup python3 -uB proxy/mneme_proxy.py > /tmp/mneme_proxy.log 2>&1 &
sleep 5
curl -s http://localhost:8080/health

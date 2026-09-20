#!/usr/bin/env python3
"""Run one weather task through the Mneme proxy and time it. Usage: weather_test.py "<question>" [port]"""
import json, sys, time, urllib.request

q = sys.argv[1] if len(sys.argv) > 1 else "What's the weather in Charleston, Maine this weekend?"
port = sys.argv[2] if len(sys.argv) > 2 else "8080"

payload = {
    "model": "orcarouter/Qwen3.8-27B-Uncensored",
    "messages": [{"role": "user", "content": q}],
    "stream": False,
}
t0 = time.time()
req = urllib.request.Request(
    f"http://localhost:{port}/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=900) as r:
        body = r.read().decode()
    dt = time.time() - t0
    d = json.loads(body)
    content = d["choices"][0]["message"].get("content") or ""
    print(f"=== DONE in {dt:.1f}s ===", flush=True)
    print(f"content chars={len(content)}", flush=True)
    print(content, flush=True)
except Exception as e:
    print(f"=== ERROR after {time.time()-t0:.1f}s: {e} ===", flush=True)

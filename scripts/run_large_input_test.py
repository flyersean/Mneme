#!/usr/bin/env python3
"""Send the 15k-token doc + a reading task through the Mneme proxy and time it."""
import json, time, urllib.request

DOC = open("/tmp/test_doc.txt").read()
TASK = ("Read the following report carefully. List every finding where status='critical'. "
        "For each, give the Finding ID and the observed value. At the end, state the total count "
        "of critical findings. Do not use any tools; the full report is provided below.\n\n"
        "=== REPORT START ===\n" + DOC + "\n=== REPORT END ===")

payload = {
    "model": "orcarouter/Qwen3.8-27B-Uncensored",
    "messages": [{"role": "user", "content": TASK}],
    "stream": False,
}
print(f"doc chars={len(DOC)}; sending request...", flush=True)
t0 = time.time()
req = urllib.request.Request(
    "http://localhost:8080/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=1800) as r:
        body = r.read().decode()
    dt = time.time() - t0
    d = json.loads(body)
    content = d["choices"][0]["message"].get("content") or ""
    print(f"=== DONE in {dt:.1f}s ===", flush=True)
    print(f"content chars={len(content)}", flush=True)
    print(content[:2000], flush=True)
except Exception as e:
    print(f"=== ERROR after {time.time()-t0:.1f}s: {e} ===", flush=True)

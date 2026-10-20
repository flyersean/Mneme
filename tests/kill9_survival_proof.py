#!/usr/bin/env python3
"""kill -9 survival proof: a committed chunk must survive a SIGKILL of the proxy.

Spawns a child that imports the (patched) proxy, saves a chunk via save_chunk,
commits, then sleeps. The parent SIGKILLs it and reopens the DB to prove the
chunk survived (synchronous=FULL makes the commit durable even under SIGKILL).

IMPORTANT: the child pins MNEME_DB_PATH / MNEME_HARNESS_DB / MNEME_CHUNK_DIR to
the temp dir explicitly, so an inherited MNEME_DB_PATH from the caller's shell
(crucially: the 8080 proxy's own env when run via its bash tool) cannot redirect
the write into the production DB. A fail-loud assert in the child enforces it.
"""
import os
import sys
import time
import sqlite3
import tempfile
import subprocess
import signal

TMP = tempfile.mkdtemp(prefix="mneme_kill9_")
DB = os.path.join(TMP, "mneme.db")
CWD = "/home/ubuntu/mneme/test-repo"

child_code = r'''
import os, sys, time
_tmp = "{tmp}"
# Pin EVERY path the proxy resolves from the environment, so an inherited
# MNEME_DB_PATH (from the 8080 proxy's env) cannot redirect the write to prod.
os.environ["MNEME_CHUNK_DIR"] = _tmp
os.environ["MNEME_DB_PATH"] = os.path.join(_tmp, "mneme.db")
os.environ["MNEME_HARNESS_DB"] = os.path.join(_tmp, "harness.db")
os.environ["MNEME_CONFIG"] = os.path.join(_tmp, "empty_config.json")
open(os.environ["MNEME_CONFIG"], "w").write("{{}}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_ASK_REUSABLE"] = "0"
os.environ["MNEME_MEMORY_ONLY"] = "0"
sys.path.insert(0, "proxy")
import mneme_proxy as mp
# Fail loud if the DB path escaped the temp dir (should be impossible now).
assert mp.DB_PATH == os.path.join(_tmp, "mneme.db"), (
    "DB_PATH escaped temp dir: %s (inherited env leak)" % mp.DB_PATH)
mp.save_chunk("mem_kill9_proof", "topic",
              [{{"role": "user", "content": "kill9 durability proof"}}], None)
print("CHUNK_COMMITTED", flush=True)
time.sleep(3600)
'''.format(tmp=TMP)

proc = subprocess.Popen(
    [sys.executable, "-c", child_code],
    cwd=CWD,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
)

deadline = time.time() + 90
marker_seen = False
buf = ""
while time.time() < deadline:
    line = proc.stdout.readline()
    if line:
        buf += line
        if "CHUNK_COMMITTED" in line:
            marker_seen = True
            break
    elif proc.poll() is not None:
        break

if not marker_seen:
    print("FAIL: child never reported CHUNK_COMMITTED")
    print("--- child output tail ---")
    print(buf[-3000:])
    try:
        proc.kill()
    except Exception:
        pass
    sys.exit(1)

# Pre-kill sanity: the child must have written to the TEMP db, not prod.
if not os.path.exists(DB):
    print("FAIL: temp DB %s missing before kill — child wrote elsewhere" % DB)
    print("--- child output tail ---")
    print(buf[-3000:])
    try:
        proc.kill()
    except Exception:
        pass
    sys.exit(1)

print("OK: child committed to temp DB (pid=%d). Sending SIGKILL." % proc.pid)
os.kill(proc.pid, signal.SIGKILL)
proc.wait()

# Reopen the DB and confirm the chunk survived.
try:
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    row = con.execute(
        "SELECT chunk_id FROM chunks WHERE chunk_id='mem_kill9_proof'").fetchone()
    con.close()
except sqlite3.OperationalError as e:
    print("FAIL: could not reopen temp DB %s: %s" % (DB, e))
    sys.exit(1)

if row is None:
    print("FAIL: chunk mem_kill9_proof LOST after kill -9 (durability failure)")
    sys.exit(1)

print("PASS: chunk mem_kill9_proof survived kill -9")
print("db:", DB)

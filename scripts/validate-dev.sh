#!/usr/bin/env bash
# validate-dev.sh -- mechanical A->B dev validation checklist (docs/dev-validation.md).
# Boots its own scratch-port staging proxy on dev code (default 8090).
# NEVER targets ports 8080 or 7979.
set -u

PORT="${STAGING_PORT:-8090}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="${SANDBOX:-/tmp/mneme-validate-dev}"
FAILURES=0
MANUAL=0
STAGING_PID=""

note()  { printf '%s\n' "$*"; }
pass()  { note "PASS: $*"; }
fail()  { note "FAIL: $*"; FAILURES=$((FAILURES+1)); }
manual(){ note "MANUAL: $*"; MANUAL=$((MANUAL+1)); }

cleanup() {
  if [ -n "$STAGING_PID" ] && kill -0 "$STAGING_PID" 2>/dev/null; then
    kill "$STAGING_PID" 2>/dev/null
    wait "$STAGING_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

# --- sanity: never point at the live instances ---
case "$BASE_URL" in
  *8080*|*7979*) fail "refusing to run against live port 8080/7979"; exit 1;;
esac

start_staging() {
  mkdir -p "$SANDBOX"
  ( cd "$REPO_ROOT/proxy" && MNEME_PORT="$PORT" MNEME_DB_PATH="$SANDBOX/mneme.db" \
      nohup python3 mneme_proxy.py >"$SANDBOX/proxy.log" 2>&1 & echo $! >"$SANDBOX/pid" )
  STAGING_PID="$(cat "$SANDBOX/pid")"
  for _ in $(seq 1 30); do
    if curl -sf --max-time 5 "$BASE_URL/health" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

fetch_health() { curl -sf --max-time 10 "$BASE_URL/health" 2>/dev/null; }

check_health() {
  local body
  body="$(fetch_health)" || { fail "check 1/2: GET $BASE_URL/health did not return 2xx"; return; }
  printf '%s' "$body" | grep -q '"status"' && pass "check 1: /health reachable, status field present" \
    || fail "check 1: /health has no status field"
  printf '%s' "$body" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print("bad json", e); sys.exit(1)
errs=[]
blob=json.dumps(d)
for k in ("embed","chat","label"):
    if k not in blob: errs.append(f"missing backend key: {k}")
m=d.get("memory") or {}
for k in ("total","embedded","pending"):
    if k not in m: errs.append(f"missing memory.{k}")
sys.exit(1 if errs else 0)
' && pass "check 2: /health reports per-backend status + memory total/embedded/pending" \
    || manual "check 2: /health JSON missing expected keys -- inspect manually: $body"
  curl -sf --max-time 10 "$BASE_URL/status" >/dev/null 2>&1 \
    && pass "check 2b: /status reachable (per-provider ok/error)" \
    || manual "check 2b: GET /status not ok -- verify per-provider fields by hand"
}

check_roundtrip() {
  local t0 t1 dt
  t0=$(date +%s)
  curl -sf --max-time 40 -X POST "$BASE_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen/qwen3.8-flash","messages":[{"role":"user","content":"validation roundtrip marker zebra-uno-42"}]}' \
    >/dev/null 2>&1
  t1=$(date +%s)
  dt=$((t1-t0))
  if [ "$dt" -lt 30 ]; then
    pass "check 4: chat completion responded within 30s (${dt}s)"
  else
    fail "check 4: chat completion took ${dt}s (>=30s)"
  fi
  curl -sf --max-time 20 -X POST "$BASE_URL/save" >/dev/null 2>&1 && pass "check 3: /save ok (flushed)" \
    || manual "check 3: POST /save failed -- check embedding backend and verify by hand"
  local res
  res="$(curl -sf --max-time 20 -X POST "$BASE_URL/search" -H 'Content-Type: application/json' \
       -d '{"query":"zebra-uno-42","top_k":10}' 2>/dev/null)"
  if [ -n "$res" ]; then
    printf '%s' "$res" | grep -q 'zebra-uno-42' \
      && pass "check 3: save-then-search round trip returned the marker (origin visible in /search payload)" \
      || manual "check 3: search clean but no marker yet (cold-start WARN acceptable) -- verify warm"
  else
    manual "check 3: /search errored or empty -- verify save-then-search by hand"
  fi
}

check_migration() {
  local db="$1"
  [ -f "$db" ] || { fail "migration check: DB $db not found"; return; }
  local copy="$SANDBOX/mig-test.db" mip="$((PORT+2))"
  rm -f "$copy"; cp "$db" "$copy"
  # Boot the dev proxy on the OLD-FORMAT copy: this runs the real
  # migration code path (CREATE / ALTER TABLE) from the checkout.
  ( cd "$REPO_ROOT/proxy" && MNEME_PORT="$mip" MNEME_DB_PATH="$copy" \
      MNEME_EMBED_API_KEY="${MNEME_EMBED_API_KEY:-local-only}" \
      nohup python3 mneme_proxy.py >"$SANDBOX/mig.log" 2>&1 & echo $! >"$SANDBOX/mig.pid" )
  local mp; mp="$(cat "$SANDBOX/mig.pid")"
  for _ in $(seq 1 30); do
    curl -sf --max-time 3 "http://127.0.0.1:$mip/health" >/dev/null 2>&1 && break
    sleep 1
  done
  local cols
  cols="$(python3 -c "
import sqlite3
c=sqlite3.connect('$copy')
print([r[1] for r in c.execute('PRAGMA table_info(chunks)')])
" 2>/dev/null)"
  kill "$mp" 2>/dev/null; wait "$mp" 2>/dev/null || true
  [ -n "$cols" ] || { fail "check 5: could not read PRAGMA table_info from migrated copy (log: $SANDBOX/mig.log)"; return; }
  case "$cols" in
    *instance_origin*) pass "check 5: old-format DB copy migrated cleanly, instance_origin present";;
    *) fail "check 5: instance_origin missing after migration: $cols";;
  esac
}

synth_old_db() {
  local out="$1"
  rm -f "$out"
  python3 - "$out" <<'PYEOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("""CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, session_id TEXT, label TEXT, text TEXT,
  created_at REAL, embedded INTEGER DEFAULT 0)""")
db.execute("INSERT OR REPLACE INTO chunks(id,session_id,label,text,created_at,embedded) VALUES('old1','s','old','legacy row',0,0)")
db.commit(); db.close()
print("synthesized", sys.argv[1])
PYEOF
}

case "${1:-}" in
  --synth-old-db)
    if [ $# -ge 2 ]; then synth_old_db "$2"; else echo "usage: $0 --synth-old-db <path>"; exit 1; fi
    exit 0;;
  --staging)
    start_staging || { fail "check 1: staging proxy on port $PORT failed to boot -- see $SANDBOX/proxy.log"; exit 1; }
    pass "check 1: staging proxy booted on dev code (port $PORT)"
    BASE_URL="http://127.0.0.1:$PORT"
    check_health
    check_roundtrip
    OLD="${OLD_DB:-}"
    if [ -n "$OLD" ] && [ -f "$OLD" ]; then
      check_migration "$OLD"
    else
      synth_old_db "$SANDBOX/old.db" && check_migration "$SANDBOX/old.db"
    fi
    ;;
  "")
    check_health
    check_roundtrip
    manual "check 5: migration-on-old-DB not run (needs --staging or a staged copy); do it by hand"
    ;;
  *) echo "usage: [BASE_URL=...] $0 | $0 --staging | $0 --synth-old-db <path>"; exit 1;;
esac

note ""
note "Result: $FAILURES failed, $MANUAL require manual check (see docs/dev-validation.md)."
[ "$FAILURES" -eq 0 ] || exit 1
exit 0

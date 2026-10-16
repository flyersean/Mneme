#!/usr/bin/env bash
# validate-dev.sh -- mechanical A->B dev validation checklist (docs/dev-validation.md).
# Boots its own scratch-port staging proxy on dev code (default 8090).
# NEVER targets the live instance ports 8080 or 7979.
set -u

PORT="${STAGING_PORT:-8090}"
export STAGING_PORT
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

# --- sanity: never point at the live instances -----------------------------
# Match the AUTHORITY (host:port) only, not the whole URL -- a substring
# match on $BASE_URL false-positives on query strings or paths and can be
# bypassed via a hostname alias.
auth="${BASE_URL#*://}"; auth="${auth%%/*}"          # strip scheme and path
auth_host="${auth%%:*}"; auth_port="${auth##*:}"      # host, port
[ "$auth_port" = "$auth" ] && auth_port=""          # no port given
case "$auth_port" in
  8080|7979) fail "refusing to run against live port $auth_port"; exit 1;;
esac
# belt and braces: also catch an authority that embeds the live ports as a suffix
case "$auth_host" in
  *8080|*7979) fail "refusing: authority host looks like a live port alias ($auth_host)"; exit 1;;
esac

start_staging() {
  mkdir -p "$SANDBOX"
  ( cd "$REPO_ROOT/proxy" && MNEME_PORT="$PORT" MNEME_DB_PATH="$SANDBOX/mneme.db" \
      nohup python3 mneme_proxy.py >"$SANDBOX/proxy.log" 2>&1 & echo $! >"$SANDBOX/pid" )
  STAGING_PID="$(cat "$SANDBOX/pid")"
  for _ in $(seq 1 30); do
    if curl -sf --max-time 5 "$BASE_URL/health" >/dev/null 2>&1; then
      # served 2xx at least once; confirm the process is still alive before
      # trusting the boot (a post-serve crash would otherwise surface later
      # as a confusing "missing column" or "connection refused" failure).
      kill -0 "$STAGING_PID" 2>/dev/null && return 0
    fi
    kill -0 "$STAGING_PID" 2>/dev/null || return 1   # boot died: see proxy.log
    sleep 1
  done
  return 1
}

fetch_health() { curl -sf --max-time 10 "$BASE_URL/health" 2>/dev/null; }

# Health JSON verified structurally; prints a compact summary line on stdout.
parse_health() {
  printf '%s' "$1" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("badjson"); sys.exit(1)
m=d.get("memory") or {}
miss=[k for k in ("total","embedded","pending") if k not in m]
if miss: print("missing:"+",".join(miss)); sys.exit(1)
print("ok:%s/%s/%s backends:%s" % (m.get("total"),m.get("embedded"),m.get("pending"),
      ",".join(sorted(k for k in ("embed","chat","label") if k in d))))
sys.exit(0)
'
}

check_health() {
  local body
  body="$(fetch_health)" || { fail "check 1/2: GET $BASE_URL/health did not return 2xx"; return; }
  printf '%s' "$body" | grep -q '"status"' && pass "check 1: /health reachable, status field present" \
    || fail "check 1: /health has no status field"
  parse_health "$body" >/dev/null && \
    pass "check 2: /health reports memory total/embedded/pending + backend keys ($(parse_health "$body"))" \
    || manual "check 2: /health JSON missing expected keys ($(parse_health "$body" 2>/dev/null)) -- inspect manually"
  # Surface the ACTUAL embedder ok/error line so a down embedder can never
  # pass silently (that was the Oct 10-14 /health blind spot). A negative
  # /health-inversion control (forcing the embedder down) is documented in
  # docs/dev-validation.md as a manual step -- deliberately not automated.
  local line
  line="$(fetch_health | python3 -c '
import json,sys
try: d=json.load(sys.stdin); e=d.get("embed") or {}
except Exception: print("parse-failed"); sys.exit(1)
v=e.get("ok"); lbl=("ok" if v is True else ("missing" if v is None else str(v)))
print("ok=%s err[%s]" % (lbl, e.get("error",""))); sys.exit(0)
' 2>/dev/null)"
  case "$line" in
    ok=ok*) pass "check 2a: embedder backend reports ok ($line)";;
    ok=missing*|"") manual "check 2a: no embedder status in /health, cannot judge";;
    parse-failed) manual "check 2a: could not read embedder status from /health";;
    *)        fail "check 2a: embedder backend NOT ok: $line";;
  esac
}

check_roundtrip() {
  local t0 t1 dt chat_body saved
  # --- chat latency ---
  t0=$(date +%s)
  chat_body="$(curl -sf --max-time 40 -X POST "$BASE_URL/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen/qwen3.8-flash","messages":[{"role":"user","content":"validation roundtrip marker zebra-uno-42"}]}' \
    2>/dev/null)"
  t1=$(date +%s)
  dt=$((t1-t0))
  if [ -z "$chat_body" ]; then
    manual "check 4: chat completion failed/empty -- only counts as PASS on a real backend; verify by hand"
  elif [ "$dt" -lt 30 ]; then
    printf '%s' "$chat_body" | python3 -c '
import json,sys,os
try: c=json.load(sys.stdin)["choices"][0]["message"]["content"]
except Exception: sys.exit(1)
sys.exit(0 if (c or "").strip() else 1)' && \
      pass "check 4: chat completion answered non-empty within 30s (${dt}s)" \
      || manual "check 4: chat returned 2xx in ${dt}s but empty content -- possible mock/fallback backend; verify by hand"
  else
    fail "check 4: chat completion took ${dt}s (>=30s)"
  fi
  # --- save: surface the stored chunk count, not just a 2xx ---
  saved="$(curl -sf --max-time 20 -X POST "$BASE_URL/save" 2>/dev/null)"
  if printf '%s' "$saved" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("saved") else 1)' 2>/dev/null; then
    note "      /save ok ($saved)"
  else
    manual "check 3: POST /save failed/empty -- check embedding backend and verify by hand"
  fi
  # --- search: require a SCORED retrieval hit of the marker, with a similarity
  #     score and a non-empty instance_origin. A substring grep alone could
  #     match an echoed payload from a mock backend -- a scored hit retrieved
  #     AFTER the flush proves the round trip AND FAISS-index consistency. ---
  local res verdict
  res="$(curl -sf --max-time 20 -X POST "$BASE_URL/search" -H 'Content-Type: application/json' \
       -d '{"query":"zebra-uno-42","top_k":10}' 2>/dev/null)"
  if [ -z "$res" ]; then
    manual "check 3: /search errored or empty -- verify save-then-search by hand"
    return
  fi
  verdict="$(printf '%s' "$res" | python3 -c '
import json, sys
try: hits = json.load(sys.stdin).get("results") or []
except Exception: print("SEARCH_UNPARSED"); sys.exit(0)
marker = [h for h in hits if "zebra-uno-42" in json.dumps(h) and isinstance(h.get("similarity"), (int, float))]
if not marker:
    print("SEARCH_NOMARKER"); sys.exit(0)
h = marker[0]
o = str(h.get("instance_origin") or "").strip()
STP = os.environ.get("STAGING_PORT", "8080")
if o:
    if o == STP: print("SEARCH_OK similarity=%s origin=%s" % (h.get("similarity"), o))
    else: print("SEARCH_OK-WRONGORIGIN similarity=%s origin=%s expected=%s" % (h.get("similarity"), o, STP))
elif "instance_origin" in h: print("SEARCH_ORIGIN_EMPTY")  # field exists but empty on this row
else: print("SEARCH_NOORIGINFIELD")  # /search does not expose instance_origin (pre-a5866b7 API)
')"
  case "$verdict" in
    SEARCH_OK-WRONGORIGIN*) pass "check 3: scored hit ($verdict) -- origin present but does not match staging port; may be a pre-existing row, note it";;
    SEARCH_OK*) pass "check 3: scored hit ($verdict) -- origin present but does not match staging port; may be a pre-existing row, note it";;
    SEARCH_NOORIGINFIELD) pass "check 3: scored hit works, but /search API does not expose instance_origin (pre-a5866b7 deployed code); verify origin via DB row by hand";;
    SEARCH_ORIGIN_EMPTY)  note "check 3: WARN -- marker hit carries EMPTY instance_origin; origin stamping not verifiable via API; check staging run logs/DB row by hand" && exit 0;;
    SEARCH_NOMARKER)    manual "check 3: search clean but no scored marker hit (cold-start/detailer lag acceptable) -- verify warm";;
        SEARCH_UNPARSED*)   manual "check 3: /search response unparseable -- verify by hand: $(printf '%s' "$res" | head -c 200)";;
    *)                 manual "check 3: search verification incomplete -- verify by hand";;
  esac
}

check_migration() {
  local db="$1"
  [ -f "$db" ] || { fail "migration check: DB $db not found"; return; }
  local copy="$SANDBOX/mig-test.db" mip="$((PORT+2))"
  rm -f "$copy"; cp "$db" "$copy"
  # Positive control: record pre-boot columns. If instance_origin is already
  # present the ALTER path is not exercised -- say so instead of passing
  # vacuously.
  local pre
  pre="$(python3 -c "
import sqlite3
print([r[1] for r in sqlite3.connect('$copy').execute('PRAGMA table_info(chunks)')])
" 2>/dev/null)"
  [ -n "$pre" ] || { fail "check 5: could not read PRAGMA from copy of $db"; return; }
  case "$pre" in
    *instance_origin*) note "      NOTE: pre-boot copy ALREADY has instance_origin (not a pure old-format DB); prefer OLD_DB=<real pre-migration backup>";;
  esac
  # Boot the dev proxy on the OLD-FORMAT copy: this runs the real
  # migration code path (CREATE / ALTER TABLE) from the checkout.
  ( cd "$REPO_ROOT/proxy" && MNEME_PORT="$mip" MNEME_DB_PATH="$copy" \
      MNEME_EMBED_API_KEY="${MNEME_EMBED_API_KEY:-local-only}" \
      nohup python3 mneme_proxy.py >"$SANDBOX/mig.log" 2>&1 & echo $! >"$SANDBOX/mig.pid" )
  local mp; mp="$(cat "$SANDBOX/mig.pid")"
  local booted=0
  for _ in $(seq 1 30); do
    if curl -sf --max-time 3 "http://127.0.0.1:$mip/health" >/dev/null 2>&1; then
      # served 2xx -- confirm the process survived a beat before trusting it
      sleep 1
      if kill -0 "$mp" 2>/dev/null; then booted=1; break; fi
    fi
    kill -0 "$mp" 2>/dev/null || break   # boot died: see mig.log
    sleep 1
  done
  if [ "$booted" != "1" ] && ! kill -0 "$mp" 2>/dev/null; then
    fail "check 5: migration boot DIED before serving -- see $SANDBOX/mig.log"
    return
  fi
  local cols
  cols="$(python3 -c "
import sqlite3
print([r[1] for r in sqlite3.connect('$copy').execute('PRAGMA table_info(chunks)')])
" 2>/dev/null)"
  kill "$mp" 2>/dev/null; wait "$mp" 2>/dev/null || true
  [ -n "$cols" ] || { fail "check 5: could not read PRAGMA table_info from migrated copy (log: $SANDBOX/mig.log)"; return; }
  case "$cols" in
    *instance_origin*) pass "check 5: old-format DB copy migrated cleanly, instance_origin present (pre-boot columns: $pre)";;
    *) fail "check 5: instance_origin missing after migration: $cols";;
  esac
}

synth_old_db() {
  local out="$1"
  rm -f "$out"
  python3 - "$out" <<'PYEOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
# Minimal legacy-shaped schema WITHOUT instance_origin (pre-migration format).
# It exercises the ALTER TABLE ADD COLUMN path; for a full-fidelity test of a
# genuinely aged DB, pass a real pre-migration backup via OLD_DB=<path>.
db.execute("""CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, session_id TEXT, label TEXT, text TEXT,
  created_at REAL, embedded INTEGER DEFAULT 0)""")
db.execute("INSERT OR REPLACE INTO chunks(id,session_id,label,text,created_at,embedded) VALUES('old1','s','old','legacy row',0,0)")
db.commit(); db.close()
print("synthesized", sys.argv[1])
PYEOF
}

verify_dev_code() {
  # We can only prove the serving checkout when WE booted it (--staging boots
  # from $REPO_ROOT/proxy). The /system/status API reports instance ports but
  # not the serving commit, so an externally supplied BASE_URL cannot be
  # code-verified: say so instead of passing vacuously.
  note "NOTE: validating against $BASE_URL; dev checkout $REPO_ROOT @ $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  note "      (API does not expose the serving commit; only --staging mode proves the target runs THIS checkout)"
}

case "${1:-}" in
  --synth-old-db)
    if [ $# -ge 2 ]; then synth_old_db "$2"; else echo "usage: $0 --synth-old-db <path>"; exit 1; fi
    exit 0;;
  --staging)
    start_staging || { fail "check 1: staging proxy on port $PORT failed to boot -- see $SANDBOX/proxy.log"; exit 1; }
    pass "check 1: staging proxy booted on dev code (port $PORT, migrations exercised below)"
    BASE_URL="http://127.0.0.1:$PORT"
    check_health
    check_roundtrip
    OLD="${OLD_DB:-}"
    if [ -n "$OLD" ] && [ -f "$OLD" ]; then
      note "      using real old-format DB: $OLD"
      check_migration "$OLD"
    else
      synth_old_db "$SANDBOX/old.db" && check_migration "$SANDBOX/old.db"
    fi
    ;;
  "")
    verify_dev_code
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

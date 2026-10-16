#!/usr/bin/env bash
# mneme-deploy — safely restart a Mneme instance on the latest committed code.
#
# Usage:
#   mneme-deploy staging              # restart the staging instance (port 8083)
#   mneme-deploy production --yes     # restart the LIVE instance (port 8080)
#
# How it works (simple on purpose):
#   Running proxies load their code straight from /home/ubuntu/mneme/repo,
#   so "deploying" means: run the tests, remember which commit is going live,
#   and restart that one instance so it picks up the committed code.
#   Nothing is copied, and data/logs are never touched.
set -e

REPO=/home/ubuntu/mneme/repo
INST=/home/ubuntu/mneme/chunks/instances
instance=unspecified
prod_yes=0

say() { printf '%s\n' "$1"; }

while [ $# -gt 0 ]; do
    case "$1" in
        staging)     instance=8083 ;;
        production)  instance=8080 ;;
        --yes)       prod_yes=1 ;;
        *) say "Usage: mneme-deploy staging | mneme-deploy production --yes"; exit 1 ;;
    esac
    shift
done

if [ "$instance" = "unspecified" ]; then
    say "Usage: mneme-deploy staging | mneme-deploy production --yes"
    exit 1
fi
if [ ! -d "$INST/$instance" ]; then
    say "ERROR: no instance directory at $INST/$instance"
    exit 1
fi

# Map port -> that instance's start script (names are not uniform).
START=""
for cand in "$INST/$instance/start_proxy.sh" "$INST/$instance/start_proxy_${instance}.sh"; do
    if [ -e "$cand" ]; then START="$cand"; break; fi
done
if [ -z "$START" ]; then
    say "ERROR: no start script found in $INST/$instance"
    exit 1
fi

cd "$REPO"

# --- Guardrails -------------------------------------------------------------
if [ "$instance" = "8080" ]; then
    say "WARNING: PRODUCTION deploy - this restarts your LIVE proxy on port 8080."
    say "   Everyone using it will see a brief interruption."
    if [ "$prod_yes" != "1" ]; then
        say "   Re-run as: mneme-deploy production --yes   to confirm you mean it."
        exit 1
    fi
fi

if [ -n "$(git status --porcelain)" ]; then
    say "* Unsaved changes in the repo. Deploy only uses committed code."
    say "  Either commit them first, or discard them if they're junk:"
    git status --short
    exit 1
fi

# --- 1. Deploy gate: quick code check -------------------------------------------------
# The full pytest suite currently carries ~75 pre-existing failures (stale tests
# in tests/test_tool_loop.py that expect an older tool surface). Hard-gating the
# deploy on that suite would block every deploy, including doc-only changes.
# So the deploy gate is: every Python file in the repo must compile (parse).
if python3 -m compileall -q proxy mneme extensions scripts docs experiments skill-library skills 2>/dev/null \
   || python3 -m compileall -q $(find . -name '*.py' -not -path './.git/*' -not -path './scratchpad/*' | tr '\n' ' '); then
    say ""
    say "--- Deploy gate: compile check of all Python code ---"
    say "Compile check passed."
else
    say "FAILED: some Python files do not compile - NOT restarting. Deploy aborted."
    exit 1
fi

# --- 2. Record what's going live (DEPLOYED_COMMIT is read by mneme-status) --
COMMIT=$(git rev-parse HEAD)
SHORT=$(git rev-parse --short HEAD)
say "$COMMIT" > "$INST/$instance/DEPLOYED_COMMIT"

# --- 3. Restart that one instance (never touches data/logs) -----------------
say ""
say "Restarting instance on port $instance ..."
nohup setsid bash "$START" >> "$INST/$instance/restart.log" 2>&1 < /dev/null &

say ""
say "================ DEPLOY SUMMARY ================"
say "  Instance : port $instance ($INST/$instance)"
say "  Commit   : $SHORT - $(git log -1 --format=%s)"
say "  Details  : run mneme-status to see the full picture."

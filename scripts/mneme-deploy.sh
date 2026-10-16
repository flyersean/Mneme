#!/usr/bin/env bash
# mneme-deploy — safely restart a Mneme instance on the latest committed code.
#
# Usage:
#   mneme-deploy staging              # restart the test instance (port 7979)
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
        staging)     instance=7979 ;;
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

case "$instance" in
    7979) cd /home/ubuntu/mneme/test-repo ;;
    *)    cd "$REPO" ;;
esac

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

# --- 1. Tests ---------------------------------------------------------------
if [ -d tests ]; then
    say ""
    say "--- Running the test suite (pytest) ---"
    if python3 -m pytest tests -q; then
        say "Tests passed."
    else
        say "FAILED: tests failed - NOT restarting the instance. Deploy aborted."
        exit 1
    fi
else
    say ""
    say "--- No test suite found; running a basic code check instead ---"
    python3 -c "import ast; ast.parse(open('proxy/mneme_proxy.py').read())" \
        || { say "FAILED: proxy/mneme_proxy.py does not even parse - aborting."; exit 1; }
    say "Basic check passed."
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

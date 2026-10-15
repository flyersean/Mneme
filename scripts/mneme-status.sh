#!/usr/bin/env bash
# mneme-status — one-command answer to "what's running, where, is it up to date?"
set -u

REPO=/home/ubuntu/mneme/repo
INSTANCES_DIR=/home/ubuntu/mneme/chunks/instances

line() { printf '%s\n' "$1"; }

line "================ MNEME STATUS ================"
line ""
line "--- Code repository (/home/ubuntu/mneme/repo) ---"
if ! cd "$REPO" 2>/dev/null; then
    line "  PROBLEM: repo directory not found!"
    exit 1
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
DIRTY=$(git status --porcelain | wc -l | tr -d ' ')
HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
LAST=$(git log -1 --format='%h %ad %s' --date=format:'%Y-%m-%d %H:%M' 2>/dev/null || echo "unknown")

line "  Branch:        $BRANCH"
if git rev-parse --verify origin/"$BRANCH" >/dev/null 2>&1; then
    AHEAD=$(git rev-list --count origin/"$BRANCH"..HEAD)
    BEHIND=$(git rev-list --count HEAD..origin/"$BRANCH")
    if [ "$AHEAD" = 0 ] && [ "$BEHIND" = 0 ]; then
        line "  GitHub sync:   in sync with GitHub (origin/$BRANCH)"
    else
        [ "$AHEAD" != 0 ]  && line "  GitHub sync:   $AHEAD commit(s) NOT pushed to GitHub yet"
        [ "$BEHIND" != 0 ] && line "  GitHub sync:   $BEHIND commit(s) on GitHub not pulled down yet"
    fi
else
    line "  GitHub sync:   no remote branch named '$BRANCH' (never pushed?)"
fi
if [ "$DIRTY" = 0 ]; then
    line "  Unsaved edits: none — everything committed  (clean)"
else
    line "  Unsaved edits: $DIRTY file(s) changed but not committed:"
    git status --porcelain | head -5 | sed 's/^/      /'
fi
line "  Last commit:   $LAST"

line ""
line "--- Running Mneme instances ---"

found_running=0

for inst in "$INSTANCES_DIR"/*/; do
    [ -d "$inst" ] || continue
    port=$(basename "$inst")
    dc_file="$inst/DEPLOYED_COMMIT"

    state=""
    if curl -sf -m 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        state="RUNNING"; found_running=1
    else
        {
        for pid in $(pgrep -f mneme_proxy.py); do
            if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -q "^MNEME_PORT=$port$"; then
                state="RUNNING (but /health not answering)"; found_running=1; break
            fi
        done
        } 2>/dev/null
        [ -z "$state" ] && continue
    fi

    line ""
    line "  Port $port  ($state)"
    line "    Data folder:  $inst"
    case "$port" in
        8080) line "    Role:         YOUR MAIN MNEME (production)" ;;
        8083) line "    Role:         staging (for testing only)" ;;
    esac

    if [ -f "$dc_file" ]; then
        dc=$(cat "$dc_file")
        dc_short=$(echo "$dc" | cut -c1-7)
        if [ "$dc" = "$(git rev-parse HEAD)" ] && [ "$DIRTY" = 0 ]; then
            line "    Up to date?   YES — running exactly the code in the repo ($dc_short)"
        elif [ "$dc" = "$(git rev-parse HEAD)" ]; then
            line "    Up to date?   code started at commit $dc_short (= repo HEAD),"
            line "                  but repo has uncommitted edits — live code may differ"
        else
            line "    Up to date?   NO — started at commit $dc_short, repo is now at $HEAD"
            line "                  Run: mneme-deploy $([ "$port" = 8080 ] && echo production || echo staging) to update"
        fi
    else
        line "    Up to date?   commit unknown (predates deploy tracking)."
        line "                  The next deploy to this port will record it."
    fi
done

[ "$found_running" = 0 ] && line "  (none found — no Mneme proxy is answering right now)"

line ""
line "Tip: run 'mneme-deploy staging' to push new code to the test instance,"
line "     then check this again before deciding on production."
line "==============================================="

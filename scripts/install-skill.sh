#!/usr/bin/env bash
# Install an optional skill from skill-library/ into the active skills/ dir.
#
# Usage: scripts/install-skill.sh <skill-name> [proxy-port]
#   <skill-name>   the directory name under skill-library/<category>/ (e.g. brainstorming)
#   [proxy-port]   optional; if given, also POST the skill to the live proxy so it is
#                  injectable without a restart.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${1:?usage: install-skill.sh <skill-name> [proxy-port]}"
PORT="${2:-}"

# Locate the skill anywhere under skill-library/ (any category).
SRC="$(find "$REPO_ROOT/skill-library" -maxdepth 3 -type d -name "$NAME" | head -1)"
if [ -z "$SRC" ]; then
    echo "ERROR: no skill named '$NAME' under skill-library/" >&2
    echo "Available:" >&2
    find "$REPO_ROOT/skill-library" -mindepth 2 -maxdepth 2 -type d | sed "s|$REPO_ROOT/skill-library/||" | sort >&2
    exit 1
fi
if [ ! -f "$SRC/SKILL.md" ]; then
    echo "ERROR: $SRC has no SKILL.md" >&2
    exit 1
fi

DEST="$REPO_ROOT/skills/$NAME"
if [ -e "$DEST" ]; then
    echo "already installed: $DEST"
else
    cp -r "$SRC" "$DEST"
    echo "installed: $DEST"
fi

if [ -n "$PORT" ]; then
    python3 - "$DEST/SKILL.md" "$PORT" "$REPO_ROOT" <<'PYEOF'
import sys, json, urllib.request
sys.path.insert(0, sys.argv[3])          # REPO_ROOT, so `proxy/` is importable
sys.path.insert(0, sys.argv[3] + "/proxy")
from mneme.harness.skills import parse_skill_md
path, port = sys.argv[1], sys.argv[2]
meta = parse_skill_md(open(path, encoding="utf-8").read())
payload = {"name": meta.get("name", ""), "description": meta.get("description", ""),
           "body": meta.get("body", ""), "source": "skill-library",
           "tags": meta.get("tags", []), "requires": meta.get("requires", [])}
req = urllib.request.Request(
    f"http://localhost:{port}/skills", data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    r = urllib.request.urlopen(req, timeout=10)
    print(f"posted to proxy :{port} -> HTTP {r.status}")
except Exception as e:
    print(f"WARNING: could not POST to proxy :{port}: {e}")
    print("  the file install still takes effect on next proxy restart")
PYEOF
fi

echo "done"

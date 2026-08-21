#!/usr/bin/env python3
"""Retire junk strategies from the Mneme strategies table.

Reuses the SAME junk filter as the proxy (`mneme_proxy._is_junk_directive`) so
the definition never drifts, but imports it in an isolated environment (temp
chunk dir, dead backend) so the proxy's startup side effects don't run against
the live store.

Safe to run while the proxy is live: SQLite WAL mode allows a concurrent
writer, and the injection query reads `retired = 0` fresh on every turn, so
retired junk stops being injected on the NEXT turn — no restart needed for the
cleanup itself (the code-level filter still needs a restart to take effect).

Usage:
    python3 scripts/retire_junk_strategies.py [path/to/mneme.db]
Default DB: $MNEME_CHUNK_DIR/mneme.db or ~/mneme_chunks/mneme.db
"""

import os
import sys
import sqlite3
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve the LIVE db path BEFORE overwriting MNEME_CHUNK_DIR for the isolated
# import below. (If we read it after, it would point at the throwaway temp dir.)
_DEFAULT_DB = os.path.join(
    os.environ.get("MNEME_CHUNK_DIR", os.path.expanduser("~/mneme_chunks")), "mneme.db"
)

# ── Isolated import of the proxy (no live side effects) ────────────────────
_tmp = tempfile.mkdtemp(prefix="mneme_cleanup_")
os.environ["MNEME_CHUNK_DIR"] = _tmp
os.environ["MNEME_CONFIG"] = os.path.join(_tmp, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"  # dead port -> probes fail fast
os.environ["MNEME_EMBED_TIMEOUT"] = "1"

sys.path.insert(0, os.path.join(REPO, "proxy"))
import mneme_proxy as mp  # noqa: E402


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_DB
    if not os.path.exists(db_path):
        print(f"no db at {db_path}", file=sys.stderr)
        return 2

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT strategy_id, grade, strategy_text FROM strategies WHERE retired = 0"
    ).fetchall()

    retired = []
    kept = []
    for r in rows:
        if mp._is_junk_directive(r["strategy_text"]):
            retired.append(r)
        else:
            kept.append(r)

    for r in retired:
        db.execute("UPDATE strategies SET retired = 1 WHERE strategy_id = ?", (r["strategy_id"],))
    db.commit()

    print(f"retired {len(retired)} junk strategies:")
    for r in retired:
        print(f"  [{r['grade']}] {r['strategy_id']}: {(r['strategy_text'] or '')[:80].strip()!r}")
    print(f"\nkept {len(kept)} non-junk strategies (retired=0):")
    for r in kept:
        print(f"  [{r['grade']}] {r['strategy_id']}: {(r['strategy_text'] or '')[:80].strip()!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

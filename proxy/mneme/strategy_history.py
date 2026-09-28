"""Strategy version history + provenance (harness Phase 3).

The strategies table is written with ``INSERT OR REPLACE`` — a new version used
to overwrite the old row, so "what did this strategy look like before?" had no
answer. This module keeps an append-only ``strategy_versions`` table: every
save snapshots the row that is about to be replaced and the row that replaced
it, with who did it and why. Additive and idempotent, like curation.ensure_schema.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

_COLS = ("strategy_id", "version", "problem_type", "strategy_text", "grade", "outcome",
         "source_chunk", "parent_id", "effective_grade", "use_count", "success_count", "retired")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS strategy_versions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id   TEXT NOT NULL,
            version       INTEGER DEFAULT 1,
            problem_type  TEXT DEFAULT '',
            strategy_text TEXT DEFAULT '',
            grade         TEXT DEFAULT '',
            outcome       TEXT DEFAULT '',
            source_chunk  TEXT DEFAULT '',
            parent_id     TEXT DEFAULT '',
            effective_grade REAL DEFAULT 0,
            use_count     INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            retired       INTEGER DEFAULT 0,
            event         TEXT NOT NULL,        -- saved | superseded
            actor         TEXT DEFAULT '',      -- user | model | run:<id> | harness
            reason        TEXT DEFAULT '',
            recorded_at   TEXT NOT NULL
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_strat_versions ON strategy_versions(strategy_id, id)")
    for col in ("created_by TEXT DEFAULT ''", "derived_from TEXT DEFAULT ''", "validated_by TEXT DEFAULT ''"):
        try:
            db.execute(f"ALTER TABLE strategies ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    db.commit()


def snapshot(db, strategy_id: str, event: str, actor: str = "", reason: str = "") -> bool:
    """Copy the CURRENT strategies row into history. Caller commits. False if absent."""
    row = db.execute(f"SELECT {', '.join(_COLS)} FROM strategies WHERE strategy_id=?",
                     (strategy_id,)).fetchone()
    if row is None:
        return False
    db.execute(
        f"INSERT INTO strategy_versions ({', '.join(_COLS)}, event, actor, reason, recorded_at) "
        f"VALUES ({', '.join('?' for _ in _COLS)}, ?, ?, ?, ?)",
        tuple(row) + (event, actor or "", reason or "", datetime.now(timezone.utc).isoformat()))
    return True


def history(db, strategy_id: str) -> List[dict]:
    cur = db.execute("SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY id", (strategy_id,))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def set_provenance(db, strategy_id: str, created_by: Optional[str] = None,
                   derived_from: Optional[str] = None, validated_by: Optional[str] = None) -> None:
    sets, vals = [], []
    for k, v in (("created_by", created_by), ("derived_from", derived_from), ("validated_by", validated_by)):
        if v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        db.execute(f"UPDATE strategies SET {', '.join(sets)} WHERE strategy_id=?", vals + [strategy_id])

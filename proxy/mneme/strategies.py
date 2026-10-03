"""Shipped + user strategy files, and export/import.

Two durable YAML files, loaded on startup with INSERT OR IGNORE (so they survive
restarts AND a DB reset), plus a JSON export/import for moving strategies between
databases:

  - ``strategies.yaml``       ships with the repo — the curated, versioned defaults
  - ``strategies.user.yaml``  per-instance — user promotions/imports, survives clears

File format (a list of maps):

    - id: web_hidden_api
      problem_type: web_retrieval
      grade: A
      cost: 0
      created_by: shipped
      text: |
        WHEN a page returns empty or JS-only content ...

Export/import uses a flat JSON list of the same fields, because it round-trips
through the browser (download / file picker) and must be trivially parseable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_file(path: str):
    """Return a list of strategy dicts from a YAML (or JSON) file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    data = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except Exception:
            data = None
    if data is None:
        try:
            data = json.loads(text)
        except Exception:
            data = []
    if isinstance(data, dict):
        data = data.get("strategies") or data.get("items") or []
    return data or []


def load_shipped(db, paths=()) -> int:
    """INSERT OR IGNORE every strategy from the shipped/user files. Returns count."""
    loaded = 0
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        for entry in _parse_file(path):
            sid = (entry or {}).get("id") or entry.get("strategy_id")
            if not sid or not (entry or {}).get("text"):
                continue
            try:
                db.execute(
                    "INSERT OR IGNORE INTO strategies "
                    "(strategy_id, problem_type, strategy_text, grade, cost, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (str(sid), entry.get("problem_type") or "other", str(entry["text"]),
                     entry.get("grade") or "B", int(entry.get("cost") or 0), _now(),
                     entry.get("created_by") or "shipped"))
                loaded += 1
            except Exception:
                continue
    try:
        db.commit()
    except Exception:
        pass
    return loaded


def _dump_file(entries) -> str:
    """Serialize a list of strategy dicts to a YAML file body (JSON fallback)."""
    if yaml is not None:
        try:
            return yaml.safe_dump(entries, sort_keys=False, allow_unicode=True,
                                  default_flow_style=False)
        except Exception:
            pass
    return json.dumps(entries, indent=2, ensure_ascii=False)


def promote(db, strategy_id: str, user_path: str) -> bool:
    """Write one DB strategy into the per-instance user file (create/update by id)."""
    row = db.execute(
        "SELECT problem_type, strategy_text, grade, cost, created_by FROM strategies "
        "WHERE strategy_id=?", (strategy_id,)).fetchone()
    if row is None:
        return False
    problem_type, text, grade, cost, created_by = row
    entries = _parse_file(user_path)
    entry = {"id": str(strategy_id), "problem_type": problem_type, "text": text,
             "grade": grade or "B", "cost": cost or 0,
             "created_by": "user"}
    # replace the matching id, else append
    replaced = False
    for i, e in enumerate(entries):
        if (e or {}).get("id") == str(strategy_id):
            entries[i] = entry
            replaced = True
            break
    if not replaced:
        entries.append(entry)
    try:
        os.makedirs(os.path.dirname(user_path) or ".", exist_ok=True)
        with open(user_path, "w", encoding="utf-8") as f:
            f.write(_dump_file(entries) + "\n")
        return True
    except OSError:
        return False


def export_json(db, strategy_id=None) -> str:
    """Serialize strategies (or one) to a JSON list for browser download."""
    if strategy_id:
        rows = db.execute(
            "SELECT strategy_id, problem_type, strategy_text, grade, cost, created_by, retired "
            "FROM strategies WHERE strategy_id=?", (str(strategy_id),)).fetchall()
    else:
        rows = db.execute(
            "SELECT strategy_id, problem_type, strategy_text, grade, cost, created_by, retired "
            "FROM strategies ORDER BY problem_type, strategy_id").fetchall()
    cols = ("strategy_id", "problem_type", "strategy_text", "grade", "cost", "created_by", "retired")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        out.append({"id": d["strategy_id"], "problem_type": d["problem_type"],
                    "text": d["strategy_text"], "grade": d["grade"], "cost": d["cost"],
                    "created_by": d["created_by"], "retired": d["retired"]})
    return json.dumps(out, indent=2, ensure_ascii=False)


def import_json(db, payload) -> int:
    """INSERT OR IGNORE strategies from an exported JSON payload. Returns count."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return 0
    if isinstance(payload, dict):
        payload = payload.get("strategies") or payload.get("items") or [payload]
    if not isinstance(payload, list):
        return 0
    added = 0
    for entry in payload:
        sid = (entry or {}).get("id") or entry.get("strategy_id")
        if not sid or not (entry or {}).get("text"):
            continue
        try:
            db.execute(
                "INSERT OR IGNORE INTO strategies "
                "(strategy_id, problem_type, strategy_text, grade, cost, created_at, created_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(sid), entry.get("problem_type") or "other", str(entry["text"]),
                 entry.get("grade") or "B", int(entry.get("cost") or 0), _now(),
                 entry.get("created_by") or "imported"))
            added += 1
        except Exception:
            continue
    try:
        db.commit()
    except Exception:
        pass
    return added

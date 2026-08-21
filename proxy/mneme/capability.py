"""Capability-edge tracking — grades -> a map of where competence ends.

Turns grades into a self-discovered competence map: a poor grade (D/F) records
against the task's problem type; enough poor grades flag it as a known edge;
the next time that type appears, the model is routed to tool-building (or an
honest "I can't") instead of another grind/fabricate attempt.
"""

import os
from datetime import datetime, timezone

from mneme.util import _log_error
from mneme.instructions import _load_instruction

# Bound by the orchestrator at startup (mneme_proxy sets capability.db = db).
db = None

EDGE_FAILURE_THRESHOLD = int(os.environ.get("MNEME_EDGE_FAILURES", "2"))   # min D/F to flag
EDGE_FAILURE_RATIO = float(os.environ.get("MNEME_EDGE_RATIO", "0.5"))      # D/F ratio to flag


def _record_capability(problem_type: str, grade: str):
    """Record a graded result against its problem type; re-evaluate the edge flag."""
    if not problem_type or problem_type == "other":
        return
    try:
        row = db.execute(
            "SELECT attempts, failures FROM capability_edges WHERE problem_type=?",
            (problem_type,),
        ).fetchone()
        attempts = (row[0] if row else 0) + 1
        failures = (row[1] if row else 0) + (1 if grade in ("D", "F") else 0)
        flagged = 1 if (failures >= EDGE_FAILURE_THRESHOLD and failures / max(attempts, 1) >= EDGE_FAILURE_RATIO) else 0
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO capability_edges (problem_type, attempts, failures, last_grade, flagged, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(problem_type) DO UPDATE SET attempts=excluded.attempts, failures=excluded.failures, "
            "last_grade=excluded.last_grade, flagged=excluded.flagged, updated_at=excluded.updated_at",
            (problem_type, attempts, failures, grade, flagged, now),
        )
        db.commit()
        if flagged:
            print(f"  [EDGE] problem_type '{problem_type}' flagged as capability edge "
                  f"({failures}/{attempts} failures)", flush=True)
    except Exception as e:
        _log_error("record_capability", e)


def _is_capability_edge(problem_type: str) -> bool:
    """True if this problem type has accumulated enough poor grades to be a known edge."""
    if not problem_type or problem_type == "other":
        return False
    try:
        row = db.execute("SELECT flagged FROM capability_edges WHERE problem_type=?", (problem_type,)).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _capability_directive(problem_type: str) -> str:
    """Injected directive when the incoming task is a known capability edge: instead of
    guessing/grinding, the model should propose (or defer to) a tool/script. Text
    externalized to mneme/instructions.py."""
    if not _is_capability_edge(problem_type):
        return ""
    return _load_instruction("capability_edge", vars={"problem_type": problem_type})


def _classify_problem_type(text: str) -> str:
    """Deterministic coarse problem-type classifier (keyword heuristic).

    Capability-oriented: 'compute' (model grinds) and 'live_data' (model
    fabricates) are the categories where the model's competence genuinely ends,
    so capability-edge tracking keys on them. 'code' is checked before 'compute'
    so 'write a function to compute X' is a code task, not a compute task.
    """
    if not text:
        return "other"
    lower = text.lower()
    if any(w in lower for w in ("error", "failed", "crash", "500", "exception", "traceback")):
        return "error"
    if any(w in lower for w in ("code", "function", "def ", "patch", "fix", "debug", "script",
                                 "python", "program", "implement", "refactor", "write a")):
        return "code"
    if any(w in lower for w in ("hash", "sha", "compute", "calculate", "prime", "fibonacci",
                                 "checksum", "encrypt", "decrypt", "sum of")):
        return "compute"
    if any(w in lower for w in ("price", "weather", "stock", "exchange rate", "current",
                                 "today", "latest", "temperature", "forecast", "now")):
        return "live_data"
    if any(w in lower for w in ("search", "fetch", "browser", "http", "page", "article",
                                 "wikipedia", "extract", "url", "web")):
        return "web_retrieval"
    if any(w in lower for w in ("save", "archive", "memory", "store", "remember", "recall")):
        return "memory_operation"
    return "other"

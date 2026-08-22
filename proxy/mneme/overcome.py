"""Overcome mode — turn a noted capability edge into an acted-on one.

DETECT -> STOP -> DELIBERATE -> BUILD -> OUTCOME.

A bounded, self-terminating escalation that fires when the model is stuck
grinding on tool calls (consecutive failures, or too many rounds without an
answer). Instead of the old soft nudge ("diagnose before retrying"), it hard-
stops the loop and forces a decision: build a tool to overcome the edge, or
declare the edge and name the missing capability.

Persistence functions take `db` explicitly so they are testable against a temp
DB and stay free of import cycles with the orchestrator.
"""

import os
import re
import time
from datetime import datetime, timezone

from mneme.util import _extract_text
from mneme.tool_trail import _extract_combined_tool_trail
from mneme.instructions import _load_instruction
from mneme.tools import save_tool as _save_tool_to_registry


# Tunables (env-overridable; mneme.yaml knobs can be layered on later)
STUCK_CONSECUTIVE_FAILURES = int(os.environ.get("MNEME_STUCK_CONSECUTIVE_FAILURES", "2"))
STUCK_MAX_TOOL_ROUNDS = int(os.environ.get("MNEME_STUCK_MAX_TOOL_ROUNDS", "6"))
BUILD_MAX_ITERATIONS = int(os.environ.get("MNEME_BUILD_MAX_ITERATIONS", "3"))
# Each build iteration = one write + one bash (a single build-and-test attempt).
# This bounds the BUILD loop in harness mode; native-mode exploratory bash is NOT
# counted against it (it's bounded by MAX_SERVER_ROUNDS + the redundancy stop).
BUILD_MAX_TOOL_CALLS = BUILD_MAX_ITERATIONS * 2
# Soft "wrap up" nudge: after this many SUCCESSFUL server-side tool calls without
# a final answer, nudge the model to synthesize. Advisory only — the model may keep
# going as long as it has NEW ideas; the hard stop is redundancy-based, not count-based.
TOOL_ROUND_NUDGE = int(os.environ.get("MNEME_TOOL_ROUND_NUDGE", "8"))
# Grinding = repeating the SAME tool call (same name + same args), not making many
# calls. After this many repeats of an already-seen call (with no intervening write
# to invalidate it), hard-stop and force a final answer.
REDUNDANT_STOP = int(os.environ.get("MNEME_REDUNDANT_STOP", "3"))
# Structural grinding = many DIFFERENT bash calls against the SAME target (URL/file).
# After this many distinct calls on one resource, nudge the model to WRITE a single
# script instead of extracting one field at a time (the Jamo's-menu pattern).
STRUCTURAL_BASH_NUDGE = int(os.environ.get("MNEME_STRUCTURAL_BASH_NUDGE", "5"))
# Absolute ceiling on server-tool rounds per turn. A high backstop for pathological
# loops; legitimate multi-source exploration should finish well under this.
MAX_SERVER_ROUNDS = int(os.environ.get("MNEME_MAX_SERVER_ROUNDS", "30"))

_OVERCOME_MARKER = "=== OVERCOME MODE ==="
_DECISION_RE = re.compile(r'DECISION\s*:\s*(build_tool|declare_edge|reuse_tool)\b', re.IGNORECASE)
_PLAN_RE = re.compile(r'PLAN\s*:\s*(.+)', re.IGNORECASE)
_MISSING_RE = re.compile(r'MISSING\s*:\s*(.+)', re.IGNORECASE)
_TOOL_RE = re.compile(r'TOOL\s*:\s*(\S+)', re.IGNORECASE)
_TOOL_SAVE_RE = re.compile(r'TOOL_SAVE\s*:\s*([^:]+?)\s*::\s*([^:]+?)\s*::\s*(\S+)', re.IGNORECASE)


def _last_user_index(messages):
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return 0


def _tool_rounds_since_last_user(messages):
    """Count tool-result messages since the last user turn (passthrough rounds)."""
    start = _last_user_index(messages)
    return sum(1 for m in messages[start:] if m.get("role") == "tool")


def _detect_stuck(messages, n=None, m=None):
    """Return (True, reason) if the model is stuck, else (False, '').

    Two independent signals (explicit non-trigger: a single fail->success):
      (a) N consecutive tool FAILUREs with no SUCCESS between them.
      (b) M total tool rounds since the last user turn without convergence.
    """
    n = STUCK_CONSECUTIVE_FAILURES if n is None else n
    m = STUCK_MAX_TOOL_ROUNDS if m is None else m

    trail = _extract_combined_tool_trail(messages, since_last_user=True)
    streak = 0
    for status, _reason in reversed(trail):
        if status == "FAILURE":
            streak += 1
        else:
            break
    if streak >= n:
        return True, f"{streak} consecutive tool failures"

    rounds = _tool_rounds_since_last_user(messages)
    if rounds >= m:
        return True, f"{rounds} tool rounds without a final answer"
    return False, ""


def _overcome_directive(problem_type, reason):
    """The hard STOP directive, injected instead of the soft nudge."""
    return _load_instruction("overcome", vars={"problem_type": problem_type, "reason": reason})


def _parse_deliberation(reply):
    """Parse a model reply for DECISION / PLAN / MISSING / TOOL markers."""
    reply = reply or ""
    dm = _DECISION_RE.search(reply)
    plan = _PLAN_RE.search(reply)
    missing = _MISSING_RE.search(reply)
    tool = _TOOL_RE.search(reply)
    return {
        "decision": (dm.group(1).lower() if dm else ""),
        "plan": (plan.group(1).strip() if plan else ""),
        "missing": (missing.group(1).strip() if missing else ""),
        "tool": (tool.group(1).strip() if tool else ""),
    }


def _in_build_mode(messages):
    """True if the model decided build_tool and is still building (no resolution)."""
    start = _last_user_index(messages)
    seen_build = False
    seen_resolution = False
    for m in messages[start:]:
        txt = _extract_text(m.get("content", "") or "")
        if "DECISION: build_tool" in txt:
            seen_build = True
        if "TOOL_SAVE:" in txt or "DECISION: declare_edge" in txt:
            seen_resolution = True
    return seen_build and not seen_resolution


def _in_reuse_mode(messages):
    """True if the model decided reuse_tool and is still reusing (no resolution)."""
    start = _last_user_index(messages)
    seen_reuse = False
    seen_resolution = False
    for m in messages[start:]:
        txt = _extract_text(m.get("content", "") or "")
        if "DECISION: reuse_tool" in txt:
            seen_reuse = True
        if "TOOL_SAVE:" in txt or "DECISION: declare_edge" in txt or "DECISION: build_tool" in txt:
            seen_resolution = True
    return seen_reuse and not seen_resolution


def _build_tool_calls(messages):
    """Count write/bash tool calls since the last user turn (build progress).

    This is the UNIFIED build-progress counter for both modes:
      * harness mode — counts the client-echoed write/bash tool calls in the
        incoming message history;
      * native mode — the orchestrator counts native write/bash executions in
        its internal loop and compares against the same BUILD_MAX_TOOL_CALLS.
    """
    start = _last_user_index(messages)
    count = 0
    for m in messages[start:]:
        for tc in (m.get("tool_calls") or []):
            if tc.get("function", {}).get("name") in ("write", "bash"):
                count += 1
    return count


def _build_directive(iteration, max_iterations):
    """The '=== BUILD MODE (iteration K/M) ===' directive for one build turn."""
    return _load_instruction("overcome_build", vars={"iteration": str(iteration), "max": str(max_iterations)})


def _build_exhausted_directive(max_iterations):
    """Force declare_edge once the build loop has exhausted its budget."""
    return _load_instruction("overcome_build_exhausted", vars={"max": str(max_iterations)})


def _reuse_directive(tool_name, tool_path):
    """Instruct the model to run an existing tool (reuse path) and use its output."""
    return _load_instruction("overcome_reuse", vars={"tool": tool_name or "?", "path": tool_path or "?"})


def _synthesize_nudge(count):
    """Soft nudge to wrap up and answer after too many tool calls without convergence."""
    return _load_instruction("synthesize_nudge", vars={"count": str(count)})


def _hard_wrapup_directive(count):
    """Hard stop: strip tools and force a final answer after too many tool rounds."""
    return _load_instruction("hard_wrapup", vars={"count": str(count)})


def _write_script_nudge(count, resource):
    """Nudge the model to write a single script when it is making many distinct
    bash calls against the same target (extracting one field at a time)."""
    return _load_instruction("write_script_nudge", vars={"count": str(count), "resource": resource})


def _reuse_tool_info(messages, db):
    """Extract the reuse tool name from the model's decision and look up its path.

    Returns (name, path). Path comes from the registry (empty if not found —
    the model still knows the name and can list_tools/read_tool to recover it).
    """
    name = ""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            d = _parse_deliberation(_extract_text(m.get("content", "") or ""))
            if d["decision"] == "reuse_tool":
                name = d["tool"]
                break
    path = ""
    if name and db is not None:
        try:
            row = db.execute("SELECT script_path FROM tools WHERE name=? AND retired=0", (name,)).fetchone()
            if row:
                path = row[0] or ""
        except Exception:
            pass
    return name, path


def _save_tool(db, problem_type, name, description, script_path):
    """Persist a working tool into the registry (delegates to mneme.tools)."""
    return _save_tool_to_registry(problem_type, name, description, script_path, db_=db)


def _record_overcome(db, problem_type, outcome):
    """Mark a problem type as attempted/confirmed/overcame on capability_edges.

    outcome in {'attempted', 'confirmed', 'overcame'}. Every call bumps
    overcome_attempts; only 'overcame' also bumps overcome_success.
    """
    if db is None or not problem_type or problem_type == "other":
        return
    try:
        ok = 1 if outcome == "overcame" else 0
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO capability_edges (problem_type, attempts, failures, last_grade, flagged, updated_at, "
            "overcome_attempts, overcome_success) "
            "VALUES (?,0,0,'',1,?,1,?) "
            "ON CONFLICT(problem_type) DO UPDATE SET "
            "overcome_attempts=COALESCE(overcome_attempts,0)+1, "
            "overcome_success=COALESCE(overcome_success,0)+excluded.overcome_success, "
            "updated_at=excluded.updated_at",
            (problem_type, now, ok),
        )
        db.commit()
        print(f"  [OVERCOME] {problem_type}: {outcome}", flush=True)
    except Exception as e:
        print(f"  [OVERCOME][ERR] {e}", flush=True)


def _tool_directive(db, problem_type):
    """Positive injection when a saved tool exists for this problem type."""
    if db is None or not problem_type:
        return ""
    try:
        row = db.execute(
            "SELECT name, description FROM tools WHERE problem_type=? AND retired=0 "
            "ORDER BY success_count DESC LIMIT 1",
            (problem_type,),
        ).fetchone()
    except Exception:
        return ""
    if not row:
        return ""
    return ("\n=== SAVED TOOL AVAILABLE ===\n"
            f"You previously built and saved a working tool for '{problem_type}': {row[0]}.\n"
            f"{row[1]}\nUse it instead of re-solving from scratch.\n")


def _handle_overcome_reply(db, problem_type, reply):
    """After an overcome directive, parse the model's reply and record the outcome.

    - TOOL_SAVE marker -> _save_tool + record 'overcame'
    - DECISION: declare_edge -> record 'confirmed'
    - DECISION: build_tool -> record 'attempted' (the build proceeds over later
      turns via the passthrough/native write/bash tools)
    - DECISION: reuse_tool -> record 'attempted' (reuse proceeds: run the
      existing tool over later turns)
    Returns a status string ('overcame'/'confirmed'/'build_tool'/'reuse_tool'/'none').
    """
    if not (reply or "").strip():
        return "none"
    m = _TOOL_SAVE_RE.search(reply)
    if m:
        name, desc, path = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        _save_tool(db, problem_type, name, desc, path)
        _record_overcome(db, problem_type, "overcame")
        return "overcame"
    delib = _parse_deliberation(reply)
    if delib["decision"] == "declare_edge":
        _record_overcome(db, problem_type, "confirmed")
        return "confirmed"
    if delib["decision"] == "build_tool":
        _record_overcome(db, problem_type, "attempted")
        return "build_tool"
    if delib["decision"] == "reuse_tool":
        _record_overcome(db, problem_type, "attempted")
        return "reuse_tool"
    return "none"

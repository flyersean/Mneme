"""Plan extraction — natural language in, only the structure the harness needs out.

The model reasons freely and marks the parts the harness acts on with the
same lightweight line tags Mneme already uses (``PLAN:`` as in overcome mode,
``[TOOL:...]``, ``[source: ...]``). No JSON is required:

    I should look at the release page first, then record the number.
    PLAN: Find the latest stable Python version on python.org
    PLAN: Write the version number to notes.txt
    VERIFY: grep -Eq '^3\\.[0-9]+' notes.txt

``VERIFY:`` lines attach a command check to the PLAN line above them. Tags are
matched case-insensitively at the start of a line, tolerating list markers
("- ", "1. ", "**PLAN:**"). Everything else is ignored.

A task step may emit ``REPLAN: <why>`` when it discovers the remaining plan is
wrong; the engine then re-enters planning (bounded by ``max_replans``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

MAX_TASKS = 8

_PREFIX = r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?(?:\*\*)?"
_PLAN_LINE = re.compile(_PREFIX + r"PLAN(?:\s*\d+)?\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$", re.I | re.M)
_VERIFY_LINE = re.compile(_PREFIX + r"VERIFY\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$", re.I)
_REPLAN_LINE = re.compile(_PREFIX + r"REPLAN\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$", re.I | re.M)
_CODE_TICKS = re.compile(r"^`+|`+$")

# Structured VERIFY: checks (deterministic, shell-free — no subprocess, no quoting
# traps). The planner is steered toward these; a shell command is only the fallback.
_VERIFY_STRUCTURED = (
    ("file_exists", re.compile(r"^file_exists(?:\s*:)?\s+(.+)$", re.I)),
    ("file_contains", re.compile(r"^file_contains(?:\s*:)?\s+(.+)$", re.I)),
    ("output_contains", re.compile(r"^output_contains(?:\s*:)?\s+(.+)$", re.I)),
    ("output_matches", re.compile(r"^output_matches(?:\s*:)?\s+(.+)$", re.I)),
)


def _parse_verify_spec(spec: str) -> dict:
    """Turn a VERIFY: line body into a check dict.

    Prefers deterministic, shell-free checks:
        file_exists <path>
        file_contains <path> :: <text>     (also accepts "<path> <text>")
        output_contains <text>
        output_matches <pattern>
    Anything else becomes a legacy {"type": "command"} shell check (still run, but
    verify.run_check downgrades a shell parse error to "inconclusive").
    """
    for kind, rx in _VERIFY_STRUCTURED:
        m = rx.match(spec)
        if not m:
            continue
        rest = m.group(1).strip()
        if kind == "file_exists":
            return {"type": "file_exists", "path": rest}
        if kind == "file_contains":
            if "::" in rest:
                path, text = rest.split("::", 1)
                return {"type": "file_contains", "path": path.strip(), "text": text.strip()}
            parts = rest.split(None, 1)
            if len(parts) == 2 and parts[1].strip():
                return {"type": "file_contains", "path": parts[0], "text": parts[1].strip()}
            return {"type": "file_exists", "path": rest}
        if kind == "output_contains":
            return {"type": "output_contains", "text": rest}
        if kind == "output_matches":
            return {"type": "output_matches", "pattern": rest}
    return {"type": "command", "command": spec}


@dataclass
class PlanResult:
    tasks: List[dict] = field(default_factory=list)   # [{"title", "instructions", "verify"?}]
    ok: bool = True
    error: str = ""
    output: str = ""
    model_calls: int = 1
    tool_calls: List[dict] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)


def parse_plan(text: str, max_tasks: int = MAX_TASKS) -> List[dict]:
    tasks: List[dict] = []
    for line in (text or "").splitlines():
        m = _PLAN_LINE.match(line)
        if m:
            title = m.group(1).strip().strip("*").strip()
            if title:
                tasks.append({"title": title[:200], "instructions": title})
            continue
        v = _VERIFY_LINE.match(line)
        if v and tasks:
            spec = _CODE_TICKS.sub("", v.group(1).strip().strip("*").strip()).strip()
            if spec and spec.lower() not in ("none", "n/a", "-"):
                tasks[-1].setdefault("verify", []).append(_parse_verify_spec(spec))
    return tasks[:max_tasks]


_LESSON_LINE = re.compile(_PREFIX + r"LESSON\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$", re.I | re.M)
_SKILL_LINE = re.compile(_PREFIX + r"SKILL\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*$", re.I | re.M)


def parse_reflection(text: str):
    """LESSON: <text>  and  SKILL: <name> :: <description> :: <procedure>  lines."""
    lessons = [m.group(1).strip() for m in _LESSON_LINE.finditer(text or "") if m.group(1).strip()]
    skills = []
    for m in _SKILL_LINE.finditer(text or ""):
        parts = [p.strip() for p in m.group(1).split("::")]
        if len(parts) >= 3 and parts[0] and parts[1]:
            name = re.sub(r"[^a-z0-9_.-]+", "-", parts[0].lower()).strip("-")[:64]
            if name:
                skills.append({"name": name, "description": parts[1], "body": "::".join(parts[2:])})
    return lessons[:5], skills[:2]


def find_replan(text: str) -> str:
    m = _REPLAN_LINE.search(text or "")
    return m.group(1).strip() if m else ""

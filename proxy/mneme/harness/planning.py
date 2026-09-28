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
            cmd = _CODE_TICKS.sub("", v.group(1).strip().strip("*").strip()).strip()
            if cmd and cmd.lower() not in ("none", "n/a", "-"):
                tasks[-1].setdefault("verify", []).append({"type": "command", "command": cmd})
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

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


def find_replan(text: str) -> str:
    m = _REPLAN_LINE.search(text or "")
    return m.group(1).strip() if m else ""

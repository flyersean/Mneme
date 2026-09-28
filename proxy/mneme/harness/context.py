"""Dynamic capability context — the right few capabilities, not all of them.

Built per step from the task text: the top relevant skills (with their
procedure, trimmed) and a short list of the most relevant tools with their
permission level and how to verify them. This goes into the step's harness
context message; the static system prompt stays short and cacheable, and a
small model is not handed every skill and tool every turn.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Tuple

from mneme.harness import capabilities as caps


class CapabilityContext:
    def __init__(self, skills=None, tool_names: Optional[Callable[[], Iterable[str]]] = None,
                 max_skills: int = 2, skill_chars: int = 1500, max_tools: int = 4):
        self.skills = skills
        self.tool_names = tool_names or (lambda: caps.BUILTIN_TOOL_META.keys())
        self.max_skills = max_skills
        self.skill_chars = skill_chars
        self.max_tools = max_tools

    def build(self, query: str, grant=None, brief: bool = False) -> Tuple[str, List[str]]:
        """Returns (text, selected skill names). brief=True: skill descriptions only
        (used for planning, where procedures would be premature detail)."""
        parts, chosen = [], []
        if self.skills is not None:
            picked = self.skills.select(query, k=self.max_skills)
            if picked:
                lines = ["Relevant skills (proven procedures — follow them where they fit):"]
                for s in picked:
                    chosen.append(s["name"])
                    stats = f"used {s['uses']}x, {s['successes']} ok" if s.get("uses") else "new"
                    lines.append(f"- {s['name']} (v{s['version']}, {stats}): {s['description']}")
                    if not brief and s.get("body"):
                        body = s["body"]
                        if len(body) > self.skill_chars:
                            body = body[: self.skill_chars] + "\n  […procedure truncated]"
                        lines.append("  " + body.replace("\n", "\n  "))
                    if not brief and s.get("failure_modes"):
                        lines.append("  Known failure modes: " + "; ".join(map(str, s["failure_modes"]))[:400])
                parts.append("\n".join(lines))
        names = list(self.tool_names() or [])
        if grant is not None:
            names = sorted(caps.allowed_names(names, set(grant)))
        tools = caps.select_tools(query, names, k=self.max_tools)
        if tools:
            lines = ["Most relevant tools for this task:"]
            for m in tools:
                v = f"; verify: {m['verify']}" if m.get("verify") else ""
                lines.append(f"- {m['name']} [{m['permission']}]: {m['does']}{v}")
            parts.append("\n".join(lines))
        if grant is not None:
            parts.append("Tools outside this run's permissions (" + ", ".join(sorted(grant))
                         + ") are unavailable; do not try them.")
        return ("\n\n".join(parts) + "\n") if parts else "", chosen

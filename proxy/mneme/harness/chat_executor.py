"""Step executor that runs a harness task through the proxy's own agent turn.

Reuse, not replacement: one harness step = one ``process_chat`` call, so a run
gets memory retrieval/injection, the server-side tool loop, provenance grading
and archiving exactly as a chat turn does. This module only:

  - builds the step's messages (a short run-context system message + the task),
  - decides success from what the HARNESS observed (grade, done_reason, empty
    output, unexecutable tool calls) — never from the model saying "done",
  - maps the turn's tool trace into ledger tool calls.

``process_chat`` keeps turn-scoped module globals (cancel flag, injected ids,
staging), so harness steps are serialized through one lock. A concurrent chat
request can still interleave with a step (as two chat requests already can);
see docs/harness/00-architecture-audit.md §7.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from mneme.harness.engine import StepContext, StepResult

_OBS_CHARS = 600
_FAIL_DONE_REASONS = {"cancelled", "error", "timeout"}


def _classify(result_text: str, blocked: bool) -> str:
    if blocked:
        return "blocked"
    try:
        from mneme.tool_trail import _classify_tool_outcome
        cls = _classify_tool_outcome(result_text or "")
    except Exception:
        cls = None
    return {"SUCCESS": "success", "FAILURE": "failure"}.get(cls[0], "") if cls else ""


def _budget_line(remaining: dict) -> str:
    parts = [f"{k.replace('max_', '')}={v:g}" if isinstance(v, float) else f"{k.replace('max_', '')}={v}"
             for k, v in (remaining or {}).items() if v is not None]
    return ", ".join(parts) or "unlimited"


def build_task_messages(ctx: StepContext, _load_instruction: Optional[Callable] = None) -> list:
    if _load_instruction is None:
        from mneme.instructions import _load_instruction
    tasks = ctx.engine.ledger.list_tasks(ctx.run["run_id"])
    position = f"{ctx.task['seq'] + 1} of {len(tasks)}"
    completed = ""
    if ctx.observations:
        lines = []
        for i, o in enumerate(ctx.observations, 1):
            res = " ".join((o.get("result") or "").split())
            if len(res) > _OBS_CHARS:
                res = res[:_OBS_CHARS] + " …"
            lines.append(f"  {i}. {o['title']} — {res or '(no output)'}")
        completed = "Completed so far:\n" + "\n".join(lines) + "\n"
    retry_note = ""
    if ctx.attempt > 1 and ctx.task.get("error"):
        retry_note = (f"This is attempt {ctx.attempt}. The previous attempt failed: "
                      f"{ctx.task['error'][:400]}\nTry a different approach.\n")
    system = _load_instruction("harness_task_context", vars={
        "goal": ctx.run["goal"], "task_position": position, "task_title": ctx.task["title"],
        "completed": completed, "retry_note": retry_note,
        "budget": _budget_line(ctx.budget_remaining),
    })
    return [{"role": "system", "content": system},
            {"role": "user", "content": ctx.task.get("instructions") or ctx.task["title"]}]


def make_chat_executor(process_chat: Callable, *, lock: Optional[threading.Lock] = None,
                       _load_instruction: Optional[Callable] = None) -> Callable[[StepContext], StepResult]:
    lock = lock or threading.Lock()

    def execute(ctx: StepContext) -> StepResult:
        messages = build_task_messages(ctx, _load_instruction)
        with lock:
            r = process_chat(messages, session_id=f"run:{ctx.run['run_id']}", tools=None) or {}
        content = (r.get("content") or "").strip()
        grade = r.get("_grade", "C")
        done_reason = r.get("done_reason") or ""
        calls = [{
            "tool": t.get("tool", "?"),
            "args": t.get("args") or {},
            "result": t.get("result") or "",
            "status": _classify(t.get("result") or "", bool(t.get("blocked"))),
            "elapsed_ms": t.get("elapsed_ms") or 0,
        } for t in (r.get("tool_trace") or [])]
        meta = {"grade": grade, "done_reason": done_reason,
                "problem_type": r.get("problem_type", ""),
                "context_injected": bool(r.get("context_injected"))}
        unexecuted = [((tc.get("function") or {}).get("name") or "?") for tc in (r.get("tool_calls") or [])]
        if unexecuted:
            return StepResult(output=content, ok=False, model_calls=1, tool_calls=calls, meta=meta,
                              error=f"model called tools the harness cannot execute: {', '.join(unexecuted)}")
        if done_reason in _FAIL_DONE_REASONS:
            return StepResult(output=content, ok=False, model_calls=1, tool_calls=calls, meta=meta,
                              error=f"model turn ended with done_reason={done_reason}")
        if not content:
            return StepResult(output="", ok=False, model_calls=1, tool_calls=calls, meta=meta,
                              error="empty model output")
        if grade == "F":
            return StepResult(output=content, ok=False, model_calls=1, tool_calls=calls, meta=meta,
                              error="turn graded F (failed/fabricated): " + content[:200])
        return StepResult(output=content, ok=True, model_calls=1, tool_calls=calls, meta=meta)

    return execute

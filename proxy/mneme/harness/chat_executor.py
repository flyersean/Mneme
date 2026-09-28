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

from mneme.harness.engine import PlanContext, StepContext, StepResult
from mneme.harness.planning import MAX_TASKS, PlanResult, find_replan, parse_plan

_OBS_CHARS = 600
_WATCH_INTERVAL = 0.5
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


def _workspace_dir(ws) -> str:
    try:
        return ws.ensure().dir("workspace") if ws is not None else "(none — use the tools directory)"
    except OSError:
        return "(unavailable)"


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
    checks = (ctx.task.get("meta") or {}).get("verify") or []
    verify_note = ""
    if checks:
        what = [c.get("command") or f"{c['type']} {c.get('path') or c.get('text') or c.get('pattern')}"
                for c in checks]
        verify_note = ("The harness will verify this task afterwards with: "
                       + "; ".join(what)[:400] + "\n")
    system = _load_instruction("harness_task_context", vars={
        "goal": ctx.run["goal"], "task_position": position, "task_title": ctx.task["title"],
        "workspace": _workspace_dir(ctx.workspace), "verify_note": verify_note,
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
        stop = threading.Event()     # this step's private cancel event (see _scoped_process_chat)
        done = threading.Event()

        def watch():                 # turn a pause/cancel request into a mid-step interrupt
            while not done.wait(_WATCH_INTERVAL):
                try:
                    if ctx.should_stop():
                        stop.set()
                        return
                except Exception:
                    return
        watcher = threading.Thread(target=watch, name="mneme-step-watch", daemon=True)
        with lock:
            watcher.start()
            try:
                r = process_chat(messages, session_id=f"run:{ctx.run['run_id']}", tools=None,
                                 cancel_event=stop) or {}
            finally:
                done.set()
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
        if stop.is_set():
            return StepResult(output=content, ok=False, model_calls=1, tool_calls=calls,
                              meta={**meta, "interrupted": True},
                              error="interrupted by a pause/cancel request")
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
        return StepResult(output=content, ok=True, model_calls=1, tool_calls=calls, meta=meta,
                          replan=find_replan(content))

    return execute


def _plan_context(pctx: PlanContext) -> str:
    if pctx.mode != "replan":
        return ""
    lines = [f"This is a REPLAN. Reason: {pctx.reason}"]
    done = [t for t in pctx.tasks if t["status"] == "completed"]
    failed = [t for t in pctx.tasks if t["status"] == "failed"]
    left = [t for t in pctx.tasks if t["status"] in ("pending", "running")]
    if done:
        lines.append("Completed (do NOT repeat):")
        lines += [f"  - {t['title']} — {' '.join((t['result'] or '').split())[:_OBS_CHARS]}" for t in done]
    if failed:
        lines.append("Failed:")
        lines += [f"  - {t['title']} — {(t['error'] or '')[:300]}" for t in failed]
    if left:
        lines.append("Not yet done (your new plan replaces these):")
        lines += [f"  - {t['title']}" for t in left]
    lines.append("Plan ONLY the remaining work, using a different approach where something failed.")
    return "\n".join(lines) + "\n"


def build_plan_messages(pctx: PlanContext, _load_instruction: Optional[Callable] = None) -> list:
    if _load_instruction is None:
        from mneme.instructions import _load_instruction
    system = _load_instruction("harness_plan", vars={
        "goal": pctx.run["goal"], "workspace": _workspace_dir(pctx.workspace),
        "max_tasks": str(MAX_TASKS), "context": _plan_context(pctx),
        "budget": _budget_line(pctx.budget_remaining),
    })
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"Plan this goal: {pctx.run['goal']}"}]


def make_chat_planner(process_chat: Callable, *, lock: Optional[threading.Lock] = None,
                      _load_instruction: Optional[Callable] = None) -> Callable[[PlanContext], PlanResult]:
    """Planner that asks the model (through process_chat) for PLAN:/VERIFY: lines.
    Share the executor's lock so planning and task steps never overlap."""
    lock = lock or threading.Lock()

    def plan(pctx: PlanContext) -> PlanResult:
        messages = build_plan_messages(pctx, _load_instruction)
        with lock:
            r = process_chat(messages, session_id=f"run:{pctx.run['run_id']}", tools=None,
                             cancel_event=threading.Event()) or {}
        content = (r.get("content") or "").strip()
        calls = [{"tool": t.get("tool", "?"), "args": t.get("args") or {},
                  "result": t.get("result") or "", "elapsed_ms": t.get("elapsed_ms") or 0,
                  "status": _classify(t.get("result") or "", bool(t.get("blocked")))}
                 for t in (r.get("tool_trace") or [])]
        tasks = parse_plan(content)
        meta = {"grade": r.get("_grade", "C"), "done_reason": r.get("done_reason") or ""}
        if not tasks:
            return PlanResult(ok=False, error="no PLAN: lines in the model's reply", output=content,
                              tool_calls=calls, meta=meta)
        return PlanResult(tasks=tasks, output=content, tool_calls=calls, meta=meta)

    return plan

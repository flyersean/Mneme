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
from mneme.harness.planning import MAX_TASKS, PlanResult, find_replan, parse_plan, parse_reflection

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


def run_grant(run: dict):
    """The run's tool permission grant (None = unrestricted, same power as chat)."""
    g = (run.get("permissions") or {}).get("grant")
    return set(g) if g is not None else None


def capability_text(engine, run: dict, query: str, brief: bool = False):
    cap = getattr(engine, "capabilities", None)
    if cap is None:
        return "", []
    pinned = (run.get("meta") or {}).get("skills") or []
    text, chosen = cap.build(f"{query}\n{' '.join(pinned)}", grant=run_grant(run), brief=brief)
    return text, chosen


def _artifacts_note(ws) -> str:
    try:
        return f" (final deliverables for the user go in {ws.dir('artifacts')})" if ws is not None else ""
    except Exception:
        return ""


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
        # Neutral, result-driven wording. "failed" + "Try a different approach"
        # reads as "you messed up, pivot" to the model's connotation, which made
        # it swap a working tool for another on a false grade. State the result
        # and let the model decide.
        retry_note = (f"Attempt {ctx.attempt}. Previous attempt result: "
                      f"{ctx.task['error'][:400]}\n")
        # On a failed step, surface the saved strategies for THIS problem type —
        # a known technique for the class of problem the step belongs to, not
        # strategies for unrelated problems (a web-scrape playbook never injects
        # into a math task). Grade-first, cheapest wins.
        try:
            from mneme.capability import _classify_problem_type, _strategies_for
            ptype = _classify_problem_type(
                (ctx.task.get("title") or "") + "\n" + (ctx.task.get("error") or ""))
            for s in _strategies_for(ptype):
                retry_note += f"\nKnown approach for '{ptype}' problems: {s[:400]}\n"
        except Exception:
            pass
    checks = (ctx.task.get("meta") or {}).get("verify") or []
    verify_note = ""
    if checks:
        what = [c.get("command") or f"{c['type']} {c.get('path') or c.get('text') or c.get('pattern')}"
                for c in checks]
        verify_note = ("The harness will verify this task afterwards with: "
                       + "; ".join(what)[:400] + "\n")
    system = _load_instruction("harness_task_context", vars={
        "goal": ctx.run["goal"], "task_position": position, "task_title": ctx.task["title"],
        "workspace": _workspace_dir(ctx.workspace) + _artifacts_note(ctx.workspace), "verify_note": verify_note,
        "capabilities": capability_text(ctx.engine, ctx.run, ctx.task.get("instructions") or ctx.task["title"])[0],
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
                                 cancel_event=stop, tool_grant=run_grant(ctx.run)) or {}
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
                "skills": capability_text(ctx.engine, ctx.run, ctx.task.get("instructions") or ctx.task["title"])[1],
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
        if grade == "F" and not calls:
            # Only a PURE-CONTENT turn (no tools used) can fail on provenance. A
            # tool step is judged by its verification, not by whether its narration
            # carried a [source:] tag — the "fabricated" grade was a false positive
            # that made the model (correctly) pivot away from a working approach.
            # Neutral wording too: "un-cited" names the property, where
            # "failed/fabricated" reads as "you lied" to the model's connotation.
            return StepResult(output=content, ok=False, model_calls=1, tool_calls=calls, meta=meta,
                              error="response was un-cited (no [source:] tag for its claims): " + content[:200])
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
    lines.append("Plan ONLY the remaining work. Decompose it into the smallest possible steps, "
                 "each a single operation. Split any failed task into smaller single-purpose "
                 "steps instead of retrying it as-is.")
    return "\n".join(lines) + "\n"


def build_plan_messages(pctx: PlanContext, _load_instruction: Optional[Callable] = None) -> list:
    if _load_instruction is None:
        from mneme.instructions import _load_instruction
    system = _load_instruction("harness_plan", vars={
        "goal": pctx.run["goal"], "workspace": _workspace_dir(pctx.workspace),
        "max_tasks": str(MAX_TASKS), "context": _plan_context(pctx),
        "capabilities": capability_text(pctx.engine, pctx.run, pctx.run["goal"], brief=True)[0],
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
                             cancel_event=threading.Event(), tool_grant=run_grant(pctx.run)) or {}
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


def make_chat_reflector(process_chat: Callable, *, lock: Optional[threading.Lock] = None,
                        _load_instruction: Optional[Callable] = None) -> Callable:
    """on_finish hook: one reflection turn after a run that failed or needed
    recovery. LESSON: lines become L1 knowledge; SKILL: lines become L2 skill
    proposals (auto-applied, versioned). Costs one model call per such run."""
    lock = lock or threading.Lock()
    if _load_instruction is None:
        from mneme.instructions import _load_instruction

    def reflect(engine, run: dict) -> None:
        evo = getattr(engine, "evolution", None)
        if evo is None:
            return
        failures = engine.ledger.events(run["run_id"], types=["task_failed", "verification_failed"])
        if run["status"] == "completed" and not failures:
            return
        tasks = engine.ledger.list_tasks(run["run_id"])
        summary = "\n".join(f"- [{t['status']}] {t['title']}" + (f" — {t['error'][:200]}" if t["error"] else "")
                            for t in tasks)
        prompt = _load_instruction("harness_reflect", vars={
            "goal": run["goal"], "outcome": run["status"] + (f": {run['error'][:300]}" if run.get("error") else ""),
            "tasks": summary})
        with lock:
            r = process_chat([{"role": "user", "content": prompt}], session_id=f"run:{run['run_id']}",
                             tools=None, cancel_event=threading.Event(), tool_grant=set()) or {}
        lessons, skills = parse_reflection(r.get("content") or "")
        for text in lessons:
            evo.propose("knowledge", f"lesson:{run['run_id']}", text, reason="reflection",
                        evidence=[run["run_id"]], created_by=f"run:{run['run_id']}")
        if "skill" in evo.appliers:
            import json as _json
            for sk in skills:
                cur = engine.skills.get(sk["name"]) if getattr(engine, "skills", None) else None
                body = (cur["body"] + "\n\n" if cur and cur.get("body") else "") + sk["body"]
                evo.propose("skill", sk["name"], _json.dumps({"description": sk["description"], "body": body}),
                            reason="reflection", evidence=[run["run_id"]], created_by=f"run:{run['run_id']}")
        engine.ledger.emit(run["run_id"], "reflected", {"lessons": len(lessons), "skills": len(skills)})

    return reflect

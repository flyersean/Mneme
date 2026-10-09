"""Run engine — executes runs durably.

The engine walks a run's tasks in order and executes each through a pluggable
*step executor* (``executor(ctx) -> StepResult``). The executor proposes and
does the work (in the proxy: one ``process_chat`` turn); the engine owns
everything around it:

  - state: every transition, step, tool call and outcome goes to the ledger;
  - checkpoints after every step, so a crash loses at most the in-flight step;
  - budgets (max_steps, max_model_calls, max_tool_calls, max_failures,
    max_runtime, max_cost), enforced before each step;
  - control: pause / cancel are cooperative and take effect at step boundaries;
    resume / retry re-enter the loop from persisted state, never from the
    model's memory;
  - recovery: runs left active by a dead process come back as ``paused`` with a
    ``run_interrupted`` event (at-least-once: the interrupted step re-runs on
    resume, which is why auto-resume is opt-in).

Phase 1 has no planner: a run's tasks are given at creation (or default to one
task = the goal). A step reports ``done=False`` to keep working on the same
task; ``ok=False`` counts as a failure and the task is re-attempted until the
``max_failures`` budget is spent.
"""

from __future__ import annotations

import threading
import time
import traceback
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from mneme.harness.ledger import (
    ACTIVE_STATES, TERMINAL_STATES, InvalidTransition, Ledger, LedgerError, process_owner,
)
from mneme.harness.workspace import RunWorkspace
from mneme.harness import verify as _verify
from mneme.harness.planning import PlanResult
from mneme.harness.failures import classify as _classify_failure

DEFAULT_BUDGET = {
    "max_steps": 100,        # hard safety cap on steps per run
    "max_turns": None,       # free-form goal sessions: model turns before giving up
    "max_failures": 3,       # failed steps before the run fails
    "max_model_calls": None,
    "max_tool_calls": None,
    "max_replans": 2,        # planner re-entries after a dead end (needs a planner)
    "max_runtime": None,     # seconds of execution, summed across resumes
    "max_cost": None,        # executor-reported cost units
}
BUDGET_KEYS = tuple(DEFAULT_BUDGET)
_USAGE_ZERO = {"steps": 0, "model_calls": 0, "tool_calls": 0, "failures": 0,
               "replans": 0, "runtime_s": 0.0, "cost": 0.0}
_BUDGET_TO_USAGE = {"max_steps": "steps", "max_failures": "failures",
                    "max_model_calls": "model_calls", "max_tool_calls": "tool_calls",
                    "max_replans": "replans", "max_runtime": "runtime_s", "max_cost": "cost"}
_RESULT_PREVIEW = 4000


class BudgetExceeded(LedgerError):
    pass


@dataclass
class StepResult:
    output: str = ""
    ok: bool = True
    done: bool = True            # False = keep working on the same task next step
    error: str = ""
    retryable: bool = True       # False = a failure that must not be retried
    model_calls: int = 1
    tool_calls: List[dict] = field(default_factory=list)  # {tool, args, result, status, elapsed_ms}
    cost: float = 0.0
    meta: Dict = field(default_factory=dict)
    replan: str = ""             # non-empty = the step asks the harness to replan (reason)


@dataclass
class StepContext:
    engine: "RunEngine"
    run: dict
    task: dict
    step: dict
    workspace: Optional[RunWorkspace]
    observations: List[dict]     # results of the run's completed tasks, in order
    budget_remaining: Dict
    attempt: int
    note: str = ""              # pending user note (from /note) to inject this step

    def emit(self, type: str, data: Optional[dict] = None) -> int:
        return self.engine.ledger.emit(self.run["run_id"], type, data,
                                       task_id=self.task["task_id"], step_id=self.step["step_id"])

    def add_artifact(self, path: str, kind: str = "file", description: str = "",
                     provenance: Optional[dict] = None) -> dict:
        return self.engine.ledger.add_artifact(
            self.run["run_id"], path, kind=kind, description=description,
            task_id=self.task["task_id"], step_id=self.step["step_id"], provenance=provenance)

    def should_stop(self) -> bool:
        """True when a pause/cancel was requested — long executors may poll this."""
        run = self.engine.ledger.get_run(self.run["run_id"])
        return bool(run and run.get("control"))


Executor = Callable[[StepContext], StepResult]


@dataclass
class PlanContext:
    engine: "RunEngine"
    run: dict
    mode: str                    # "initial" | "replan"
    reason: str                  # why we are (re)planning
    tasks: List[dict]            # the run's tasks so far (with status/result/error)
    budget_remaining: Dict
    workspace: Optional[RunWorkspace]


Planner = Callable[[PlanContext], PlanResult]


def merge_budget(budget: Optional[dict]) -> dict:
    out = dict(DEFAULT_BUDGET)
    for k, v in (budget or {}).items():
        if k not in DEFAULT_BUDGET:
            raise LedgerError(f"unknown budget key {k!r} (known: {', '.join(BUDGET_KEYS)})")
        if v is not None:
            try:
                v = float(v) if k in ("max_runtime", "max_cost") else int(v)
            except (TypeError, ValueError):
                raise LedgerError(f"budget {k} must be a number, got {v!r}")
            if v < 0:
                raise LedgerError(f"budget {k} must be >= 0")
        out[k] = v
    return out


def _extract_plan_tasks(plan_text: str) -> List[str]:
    """Extract numbered/bulleted steps from a free-form self-plan text."""
    tasks = []
    for line in (plan_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(?:\d+[.)]\s*|[-*•]\s+)(.*)$', line)
        if m:
            task = m.group(1).strip()
            if task and task not in tasks:
                tasks.append(task)
    return tasks


_DEFAULT_PLAN = (
    "1. Read the relevant code and understand the current state.\n"
    "2. Implement the change.\n"
    "3. Verify it works (test it) and confirm the goal is met."
)


class RunEngine:
    def __init__(self, ledger: Ledger, executor: Executor, *, planner: Optional[Planner] = None,
                 freeform_turn: Optional[Callable] = None,
                 capabilities=None, skills=None, judge=None, plan_judge=None, step_judge=None, diagnostics=None, evolution=None, profiles=None,
                 runs_root: Optional[str] = None,
                 lease_seconds: float = 120.0, owner_tag: str = "engine",
                 log: Optional[Callable[[str], None]] = None):
        self.ledger = ledger
        self.executor = executor
        self.planner = planner
        self.freeform_turn = freeform_turn  # (engine, run, transcript, turn, remaining, note, cancel_event) -> dict
        self.capabilities = capabilities     # CapabilityContext (Phase 4) — optional
        self.skills = skills                 # SkillRegistry (Phase 3) — optional
        self.judge = judge                   # (criteria, output) -> (bool, why) for llm_judge checks + appeal
        self.plan_judge = plan_judge         # (goal, plan_text) -> (bool, why) — approves a plan before execution
        self.step_judge = step_judge         # (criteria, output, evidence) -> (bool, why) — confirms a checkless step
        self.diagnostics = diagnostics       # () -> str; extra failure context (e.g. bash log tail)
        self.on_finish: List[Callable] = []  # hooks(engine, run) after completed/failed
        self.evolution = evolution            # Evolution (Phase 6) — optional
        self.profiles = profiles              # ProfileStore (Phase 7) — optional
        self.jobs = None                      # JobStore (Phase 9) — bound by the host
        self.on_finish.append(_capture_artifacts)
        if skills is not None:
            self.on_finish.append(_record_skill_outcomes)
        if evolution is not None:
            from mneme.harness.evolution import observe_run
            self.on_finish.append(observe_run)
        self.runs_root = runs_root
        self.lease_seconds = float(lease_seconds)
        self.owner = process_owner(owner_tag)
        self._log = log or (lambda msg: print(f"  [HARNESS] {msg}", flush=True))
        self._workers: Dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()

    # ── creation ─────────────────────────────────────────────────────────

    def create(self, goal: str, tasks: Optional[List] = None, *, budget: Optional[dict] = None,
               start: bool = False, plan: Optional[bool] = None, free_form: bool = False, **kw) -> dict:
        """plan=None: let the planner produce tasks when none are given (if a planner
        is configured); plan=True forces planning; plan=False never plans.
        free_form=True: a durable goal session — one task = the goal, no planner, no
        deterministic verification; the model drives toward the goal across turns and
        the judge checks the result when it declares done.
        profile=<name> merges that profile's budget/grant/skills/approval defaults
        UNDER the explicit arguments."""
        if kw.get("profile"):
            if self.profiles is None:
                raise LedgerError("profiles are not configured on this harness")
            budget, kw["permissions"], kw["meta"], plan = self.profiles.apply_to(
                kw["profile"], budget=budget, permissions=kw.get("permissions"),
                meta=kw.get("meta"), plan=plan)
        if free_form:
            plan = False
            kw["meta"] = {**(kw.get("meta") or {}), "free_form": True}
            budget = {**(budget or {}), "max_turns": (budget or {}).get("max_turns", 20)}
        defer = (not tasks and self.planner is not None) if plan is None else bool(plan)
        if defer and self.planner is None:
            raise LedgerError("plan requested but this engine has no planner")
        if defer and tasks:
            raise LedgerError("give either tasks or plan=true, not both")
        perms = kw.get("permissions") or {}
        if perms.get("grant") is not None:
            from mneme.harness.capabilities import normalize_grant
            try:
                kw["permissions"] = {**perms, "grant": sorted(normalize_grant(perms["grant"]))}
            except ValueError as e:
                raise LedgerError(str(e))
        run = self.ledger.create_run(goal, tasks, budget=merge_budget(budget), defer_plan=defer, **kw)
        ws = self.workspace(run["run_id"])
        if ws is not None:
            ws.ensure()
            self.ledger.update_run(run["run_id"], workspace=ws.path)
        self._log(f"run {run['run_id']} created ({len(self.ledger.list_tasks(run['run_id']))} task(s))")
        if start:
            self.start(run["run_id"])
        return self.ledger.get_run(run["run_id"])

    def workspace(self, run_id: str) -> Optional[RunWorkspace]:
        return RunWorkspace(self.runs_root, run_id) if self.runs_root else None

    # ── background execution ─────────────────────────────────────────────

    def start(self, run_id: str) -> threading.Thread:
        """Execute a run on a background thread (idempotent while it is running)."""
        with self._workers_lock:
            t = self._workers.get(run_id)
            if t is not None and t.is_alive():
                return t
            t = threading.Thread(target=self._thread_main, args=(run_id,),
                                 name=f"mneme-run-{run_id[-8:]}", daemon=True)
            self._workers[run_id] = t
            t.start()
            return t

    def _thread_main(self, run_id: str):
        try:
            self.execute(run_id)
        except Exception as e:  # never let a worker die silently
            self._log(f"run {run_id} worker crashed: {type(e).__name__}: {e}")
            try:
                self.ledger.emit(run_id, "engine_error", {"error": f"{type(e).__name__}: {e}",
                                                          "traceback": traceback.format_exc()[-2000:]})
            except Exception:
                pass

    def is_executing(self, run_id: str) -> bool:
        with self._workers_lock:
            t = self._workers.get(run_id)
            return bool(t and t.is_alive())

    def wait(self, run_id: str, timeout: float = 30.0) -> dict:
        with self._workers_lock:
            t = self._workers.get(run_id)
        if t is not None:
            t.join(timeout)
        return self.ledger.get_run(run_id)

    # ── the run loop ─────────────────────────────────────────────────────

    def execute(self, run_id: str) -> dict:
        """Run until the run completes, fails, pauses or is cancelled. Blocking."""
        run = self.ledger.require_run(run_id)
        if run["status"] in TERMINAL_STATES:
            return run
        if (run.get("meta") or {}).get("external"):
            raise LedgerError(f"run {run_id} is driven externally ({run['meta']['external']}) — "
                              "the harness records it but does not execute it")
        if not self.ledger.claim(run_id, self.owner, self.lease_seconds):
            raise LedgerError(f"run {run_id} is owned by another live process ({run.get('owner')})")
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop, args=(run_id, hb_stop),
                              name=f"mneme-hb-{run_id[-8:]}", daemon=True)
        hb.start()
        try:
            # We hold the claim, so no one else is executing: any step still marked
            # running was cut off by a crash that recover() has not seen yet.
            for s in self.ledger.list_steps(run_id):
                if s["status"] == "running":
                    self.ledger.finish_step(s["step_id"], "interrupted",
                                            error="process stopped during this step")
            run = self.ledger.require_run(run_id)
            if run["status"] not in ACTIVE_STATES:
                resumed = bool(self.ledger.list_steps(run_id)) or run["status"] == "paused"
                self.ledger.transition(run_id, "running",
                                       event="run_resumed" if resumed else "run_started",
                                       data={"owner": self.owner, "attempt": run.get("attempt", 1)})
            if (run.get("meta") or {}).get("free_form"):
                self._free_form_loop(run_id)
            else:
                self._loop(run_id)
        finally:
            hb_stop.set()
            self.ledger.release(run_id, self.owner)
        return self.ledger.get_run(run_id)

    def _heartbeat_loop(self, run_id: str, stop: threading.Event):
        interval = max(0.5, self.lease_seconds / 4.0)
        while not stop.wait(interval):
            try:
                self.ledger.heartbeat(run_id, self.owner)
            except Exception:
                pass

    def _loop(self, run_id: str) -> dict:
        segment_start = time.monotonic()
        while True:
            run = self.ledger.require_run(run_id)
            if run["status"] not in ACTIVE_STATES:
                return run  # a step (or another actor) ended the run
            usage = {**_USAGE_ZERO, **(run.get("usage") or {})}
            usage["runtime_s"] = round(usage["runtime_s"] + (time.monotonic() - segment_start), 3)
            segment_start = time.monotonic()
            self.ledger.update_run(run_id, usage=usage)
            run["usage"] = usage

            control = run.get("control") or ""
            if control == "pause":
                self.ledger.update_run(run_id, control="")
                self.checkpoint(run_id, reason="pause")
                return self.ledger.transition(run_id, "paused")
            if control == "cancel":
                return self._finish_cancel(run_id)

            plan_state = run.get("plan") or {}
            if plan_state.get("source") == "pending":
                self._plan(run_id, "initial", "new run")
                continue
            if plan_state.get("pending_replan"):
                reason = plan_state["pending_replan"]
                self.ledger.update_run(run_id, plan={k: v for k, v in plan_state.items() if k != "pending_replan"})
                if self._can_replan(run_id):
                    self._plan(run_id, "replan", reason)
                else:
                    self._end(run_id, "failed", error=reason, data={"reason": "rejected"})
                continue

            tasks = self.ledger.list_tasks(run_id)
            task = next((t for t in tasks if t["status"] in ("pending", "running")), None)
            if task is None:
                return self._finish_complete(run_id, tasks)

            exceeded = self._budget_exceeded(run.get("budget") or {}, usage)
            if exceeded:
                self.ledger.emit(run_id, "budget_exceeded", {"budget": exceeded, "usage": usage})
                self.checkpoint(run_id, reason="budget_exceeded")
                return self._end(run_id, "failed", error=f"budget exceeded: {exceeded}",
                                              data={"reason": "budget_exceeded", "budget": exceeded})

            if self._needs_approval(run, task):
                self._request_approval(run_id, task)
                return self.ledger.get_run(run_id)

            self._run_one_step(run, task, tasks, usage)

    def _free_form_loop(self, run_id: str) -> dict:
        """Free-form goal session: the model drives toward the goal across turns. The
        harness records each turn, enforces a turn budget, and runs the judge as a final
        check when the model declares done. No task graph, no deterministic verification."""
        segment_start = time.monotonic()
        while True:
            run = self.ledger.require_run(run_id)
            if run["status"] not in ACTIVE_STATES:
                return run
            usage = {**_USAGE_ZERO, **(run.get("usage") or {})}
            usage["runtime_s"] = round(usage["runtime_s"] + (time.monotonic() - segment_start), 3)
            segment_start = time.monotonic()
            self.ledger.update_run(run_id, usage=usage)
            run["usage"] = usage

            control = run.get("control") or ""
            if control == "pause":
                self.ledger.update_run(run_id, control="")
                self.checkpoint(run_id, reason="pause")
                return self.ledger.transition(run_id, "paused")
            if control == "cancel":
                return self._finish_cancel(run_id)

            # Planning phase (once): ask the model for a plan before it starts grinding.
            plan = run.get("plan") or {}
            if plan.get("source") != "self-plan":
                self._freeform_plan(run_id)
                continue  # re-read the run (now has a plan) and start execution
            plan_text = (plan.get("raw") or "").strip()

            budget = run.get("budget") or {}
            max_turns = budget.get("max_turns") or 20
            prior = [s for s in self.ledger.list_steps(run_id) if s.get("kind") == "freeform"]
            turn = len(prior) + 1
            if turn > max_turns:
                return self._end(run_id, "failed",
                                 error=f"reached max_turns ({max_turns}) without completing the goal",
                                 data={"reason": "max_turns"})

            transcript = [{"turn": i + 1, "content": s.get("output") or "",
                           "tool_summary": (s.get("meta") or {}).get("tool_summary") or "",
                           "repeated": bool((s.get("meta") or {}).get("repeated")),
                           "judge_feedback": (s.get("meta") or {}).get("judge_feedback") or ""}
                          for i, s in enumerate(prior)]

            note = ""
            plan = run.get("plan") or {}
            if plan.get("pending_note"):
                note = plan["pending_note"]
                self.ledger.update_run(run_id, plan={k: v for k, v in plan.items() if k != "pending_note"})

            step = self.ledger.start_step(run_id, kind="freeform", input={"turn": turn, "goal": run["goal"]})
            self.ledger.update_run(run_id, current_step_id=step["step_id"])

            stop = threading.Event()

            def watch():
                while not stop.wait(0.5):
                    try:
                        if (self.ledger.get_run(run_id) or {}).get("control"):
                            stop.set()
                            return
                    except Exception:
                        return
            watcher = threading.Thread(target=watch, name="mneme-ff-watch", daemon=True)
            watcher.start()

            try:
                if self.freeform_turn is None:
                    raise LedgerError("free-form run but no freeform_turn executor configured")
                r = self.freeform_turn(self, run, transcript, turn,
                                       {"max_turns": max(0, max_turns - turn + 1)},
                                       note, cancel_event=stop, plan_text=plan_text) or {}
            except Exception as e:
                self.ledger.finish_step(step["step_id"], "failed", error=f"{type(e).__name__}: {e}")
                self.ledger.emit(run_id, "engine_error", {"error": f"{type(e).__name__}: {e}",
                                                          "traceback": traceback.format_exc()[-1500:]})
                return self._end(run_id, "failed", error=f"turn crashed: {e}")
            finally:
                stop.set()

            content = (r.get("content") or "").strip()
            tool_trace = r.get("tool_trace") or []
            for tc in tool_trace:
                self.ledger.record_tool_call(run_id, str(tc.get("tool") or "?"), tc.get("args") or {},
                                             result=str(tc.get("result") or "")[:_RESULT_PREVIEW],
                                             status=tc.get("status") or "",
                                             elapsed_ms=int(tc.get("elapsed_ms") or 0),
                                             task_id="", step_id=step["step_id"])
            usage["model_calls"] = usage.get("model_calls", 0) + 1
            usage["tool_calls"] = usage.get("tool_calls", 0) + len(tool_trace)
            self.ledger.update_run(run_id, usage=usage)

            done = self._freeform_done(content)
            cur_summary = self._tool_summary(tool_trace)
            meta = {"turn": turn, "done": bool(done), "tool_summary": cur_summary}
            if cur_summary and prior and (prior[-1].get("meta") or {}).get("tool_summary") == cur_summary:
                meta["repeated"] = True
            if done and self.judge is not None:
                ok, why = self._freeform_judge(run, content, tool_trace)
                meta["judge_ok"] = bool(ok)
                meta["judge_feedback"] = why or ""
                if not ok:
                    done = False
            self.ledger.finish_step(step["step_id"], "completed", output=content, meta=meta)
            self.checkpoint(run_id, reason="turn")

            if done:
                for t in self.ledger.list_tasks(run_id):
                    if t["status"] in ("pending", "running"):
                        self.ledger.update_task(t["task_id"], status="completed", result=content or "",
                                                error="", finished_at=_now(), event="task_completed",
                                                data={"title": t["title"]})
                return self._end(run_id, "completed", result=content, data={"turns": turn})

    def _freeform_plan(self, run_id: str) -> str:
        """Run the one-shot planning turn and store the plan on the run. Returns the
        raw plan text ("" if the plan turn failed — execution proceeds unplanned)."""
        run = self.ledger.require_run(run_id)
        step = self.ledger.start_step(run_id, kind="plan", input={"goal": run["goal"]})
        self.ledger.update_run(run_id, current_step_id=step["step_id"])

        stop = threading.Event()

        def watch():
            while not stop.wait(0.5):
                try:
                    if (self.ledger.get_run(run_id) or {}).get("control"):
                        stop.set()
                        return
                except Exception:
                    return
        watcher = threading.Thread(target=watch, name="mneme-ff-plan-watch", daemon=True)
        watcher.start()

        plan_text = ""
        try:
            r = self.freeform_turn(self, run, [], 0, {}, "", cancel_event=stop, plan=True) or {}
            plan_text = (r.get("content") or "").strip()
            self.ledger.finish_step(step["step_id"], "completed", output=plan_text)
        except Exception as e:
            self.ledger.finish_step(step["step_id"], "failed", error=f"{type(e).__name__}: {e}")
            self.ledger.emit(run_id, "engine_error", {"error": f"plan turn crashed: {e}"})
            plan_text = ""
        finally:
            stop.set()
        tasks = _extract_plan_tasks(plan_text)
        if not tasks:
            # The model narrated ("let me look at X") instead of writing a plan. Fall
            # back to a generic plan so the run keeps structure instead of carrying a
            # useless narration as its "plan".
            plan_text = _DEFAULT_PLAN
            tasks = _extract_plan_tasks(plan_text)
        self.ledger.update_run(run_id, plan={"source": "self-plan", "version": 1,
                                             "raw": plan_text, "tasks": tasks})
        return plan_text

    def _freeform_done(self, content: str) -> bool:
        """The model declares completion by ending its reply with a lone DONE line."""
        if not content:
            return False
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if not lines:
            return False
        last = lines[-1].lower().rstrip(".! ")
        return last in ("done", "complete", "finished", "goal complete", "goal achieved")

    def _tool_summary(self, tool_trace: List[dict], max_chars: int = 700) -> str:
        """Compact record of what a turn actually DID (tools + commands), so the next
        turn can see the work even when the model's narration left it out."""
        parts = []
        for tc in (tool_trace or [])[-12:]:
            tool = tc.get("tool") or "?"
            args = tc.get("args") or {}
            a = args.get("command") if isinstance(args, dict) else ""
            if not a and isinstance(args, dict):
                a = args.get("path") or ""
            a = " ".join(str(a or "").split())[:100]
            parts.append(f"{tool}({a})" if a else tool)
        return "; ".join(parts)[:max_chars]

    def _freeform_judge(self, run: dict, content: str, tool_trace: List[dict]):
        """Final check: the judge decides whether the work is a correct bug fix or is
        aligned with the goal — never just 'it ran without error'."""
        if self.judge is None:
            return True, ""
        evidence = _verify._tool_evidence(tool_trace or [])
        try:
            ok, why = self.judge(run["goal"], content, evidence=evidence, failed="")
            return bool(ok), why or ""
        except Exception as e:
            self.ledger.emit(run["run_id"], "judge_error", {"error": f"{type(e).__name__}: {e}"})
            return True, f"judge errored ({type(e).__name__}) — accepting the model's DONE"

    def _run_one_step(self, run: dict, task: dict, tasks: List[dict], usage: dict) -> None:
        run_id = run["run_id"]
        if task["status"] == "pending":
            task = self.ledger.update_task(
                task["task_id"], status="running", attempts=task["attempts"] + 1,
                started_at=task.get("started_at") or _now(), event="task_started",
                data={"title": task["title"], "attempt": task["attempts"] + 1})
        step = self.ledger.start_step(run_id, task["task_id"], kind="model",
                                      input={"task": task["title"], "attempt": task["attempts"]})
        self.ledger.update_run(run_id, current_task_id=task["task_id"], current_step_id=step["step_id"])
        # A pending user note (from /note) is injected into this step's context and
        # then cleared, so it reaches the next step exactly once.
        _note = ""
        _plan = run.get("plan") or {}
        if _plan.get("pending_note"):
            _note = _plan["pending_note"]
            self.ledger.update_run(run_id, plan={k: v for k, v in _plan.items() if k != "pending_note"})
        ctx = StepContext(
            engine=self, run=run, task=task, step=step, workspace=self.workspace(run_id),
            observations=[{"task_id": t["task_id"], "title": t["title"], "result": t["result"]}
                          for t in tasks if t["status"] == "completed"],
            budget_remaining=self._remaining(run.get("budget") or {}, usage),
            attempt=task["attempts"],
            note=_note,
        )
        try:
            result = self.executor(ctx)
            if not isinstance(result, StepResult):
                raise TypeError(f"executor returned {type(result).__name__}, expected StepResult")
        except Exception as e:
            result = StepResult(ok=False, error=f"{type(e).__name__}: {e}", model_calls=0,
                                meta={"traceback": traceback.format_exc()[-2000:]})

        # Verification: the step says the task is done — the harness checks.
        if result.ok and result.done:
            checks = (task.get("meta") or {}).get("verify") or []
            if checks:
                passed, detail = self._verify(run_id, task, step, result.output or "", checks,
                                              tool_calls=result.tool_calls)
                result.meta = {**(result.meta or {}), "verification": detail}
                if not passed:
                    result.ok = False
                    result.error = "verification failed: " + _verify.summarize_failures(detail)
                    if self.diagnostics:
                        try:
                            extra = self.diagnostics()
                        except Exception:
                            extra = ""
                        if extra:
                            result.error += "\n\nRecent bash output (incl. background processes):\n" + extra
            elif self.step_judge is not None:
                # No deterministic checks: the step judge confirms the step is DONE
                # as planned, judging the EVIDENCE (tool trace), not the narration.
                ok, why = self._judge_step(run, task, step, result.output or "",
                                           result.tool_calls)
                result.meta = {**(result.meta or {}), "judge_verdict": why}
                if not ok:
                    result.ok = False
                    result.error = "judge rejected the step: " + (why or "no reason")

        if (result.meta or {}).get("interrupted"):
            # Stopped mid-step by pause/cancel: not a success, not a failure. The task
            # re-runs on resume; the loop top applies the pending control request.
            self._account(run_id, task, step, result)
            self.ledger.finish_step(step["step_id"], "interrupted", output=result.output or "",
                                    error=result.error or "", meta=result.meta)
            self.ledger.update_task(task["task_id"], status="pending", event="task_interrupted",
                                    data={"title": task["title"]})
            self.checkpoint(run_id, reason="interrupted")
            return

        usage = self._account(run_id, task, step, result)
        self.ledger.finish_step(step["step_id"], "completed" if result.ok else "failed",
                                output=result.output or "", error=result.error or "", meta=result.meta)

        if result.ok and result.done:
            self.ledger.update_task(task["task_id"], status="completed", result=result.output or "",
                                    error="", finished_at=_now(), event="task_completed",
                                    data={"title": task["title"]})
            if result.replan and self._can_replan(run_id):
                self.checkpoint(run_id, reason="step")
                self._plan(run_id, "replan", f"requested by task {task['title']!r}: {result.replan}")
                return
        elif result.ok:
            self.ledger.emit(run_id, "task_continued", {"title": task["title"]},
                             task_id=task["task_id"], step_id=step["step_id"])
        else:
            category = _classify_failure(result.error, result.meta)
            self.ledger.update_step_meta(step["step_id"], {"failure_category": category})
            max_f = (run.get("budget") or {}).get("max_failures")
            final = (not result.retryable) or (max_f is not None and usage["failures"] >= max_f)
            self.ledger.update_task(
                task["task_id"], status="failed" if final else "pending", error=result.error or "",
                finished_at=_now() if final else "", event="task_failed",
                data={"title": task["title"], "error": result.error, "attempt": task["attempts"],
                      "will_retry": not final, "category": category})
            if final:
                self.checkpoint(run_id, reason="task_failed")
                if self._can_replan(run_id):
                    self._plan(run_id, "replan",
                               f"task {task['title']!r} failed: {(result.error or '')[:300]}")
                    return
                self._end(run_id, "failed", error=result.error or "task failed",
                                       data={"reason": "task_failed", "task_id": task["task_id"]})
                return
        self.checkpoint(run_id, reason="step")

    def _account(self, run_id: str, task: dict, step: dict, result) -> dict:
        """Record a step's tool calls and add its cost to the run's usage."""
        for tc in result.tool_calls or []:
            self.ledger.record_tool_call(
                run_id, str(tc.get("tool") or "?"), tc.get("args") or {},
                result=str(tc.get("result") or "")[:_RESULT_PREVIEW], status=tc.get("status") or "",
                elapsed_ms=int(tc.get("elapsed_ms") or 0), task_id=task.get("task_id", ""),
                step_id=step["step_id"])
        usage = {**_USAGE_ZERO, **(self.ledger.require_run(run_id).get("usage") or {})}
        usage["steps"] += 1
        usage["model_calls"] += int(result.model_calls or 0)
        usage["tool_calls"] += len(result.tool_calls or [])
        usage["cost"] = round(usage["cost"] + float(getattr(result, "cost", 0.0) or 0.0), 6)
        if not result.ok and isinstance(result, StepResult) and not (result.meta or {}).get("interrupted"):
            usage["failures"] += 1
        self.ledger.update_run(run_id, usage=usage)
        return usage

    # ── verification ─────────────────────────────────────────────────────

    def _verify(self, run_id: str, task: dict, step: dict, output: str, checks: List[dict],
                tool_calls: Optional[List[dict]] = None):
        ws = self.workspace(run_id)
        base = ws.ensure().dir("workspace") if ws else None
        self.ledger.transition(run_id, "verifying", event="verification_started",
                               data={"task_id": task["task_id"], "checks": len(checks)})
        evidence = _verify._tool_evidence(tool_calls or [])
        criteria = (task.get("instructions") or "").strip() or (task.get("title") or "")
        passed, detail = _verify.run_checks(checks, output, base, judge=self.judge,
                                            evidence=evidence, criteria=criteria)
        self.ledger.emit(run_id, "verification_passed" if passed else "verification_failed",
                         {"results": detail}, task_id=task["task_id"], step_id=step["step_id"])
        self.ledger.transition(run_id, "running", event="verification_finished",
                               data={"passed": passed})
        return passed, detail

    def _judge_step(self, run: dict, task: dict, step: dict, output: str,
                    tool_calls: Optional[List[dict]] = None):
        """Step confirmation for a step with NO deterministic checks: the step judge
        decides whether the step is DONE as planned, judging the EVIDENCE (tool trace),
        not the model's narration."""
        criteria = (task.get("instructions") or "").strip() or (task.get("title") or "")
        evidence = _verify._tool_evidence(tool_calls or [])
        try:
            ok, why = self.step_judge(criteria, output or "", evidence=evidence, failed="")
            ok, why = bool(ok), (why or "")[:500]
        except Exception as e:
            self.ledger.emit(run["run_id"], "judge_error", {"error": f"{type(e).__name__}: {e}"})
            ok, why = True, f"judge errored ({type(e).__name__}) — accepting the step"
        self.ledger.emit(run["run_id"], "verification_passed" if ok else "verification_failed",
                         {"judge": why}, task_id=task["task_id"], step_id=step["step_id"])
        return ok, why

    def _judge_plan(self, run: dict, plan: dict):
        """Plan approval: the judge decides whether the plan will achieve the goal
        before any step runs. Returns (ok, why)."""
        if self.plan_judge is None:
            return True, ""
        plan_text = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(plan.get("tasks") or []))
        try:
            ok, why = self.plan_judge(run["goal"], plan_text)
            return bool(ok), (why or "")[:500]
        except Exception as e:
            self.ledger.emit(run["run_id"], "plan_judge_error", {"error": f"{type(e).__name__}: {e}"})
            return True, f"plan judge errored ({type(e).__name__}) — accepting the plan"

    # ── planning ─────────────────────────────────────────────────────────

    def _can_replan(self, run_id: str) -> bool:
        if self.planner is None:
            return False
        run = self.ledger.require_run(run_id)
        limit = (run.get("budget") or {}).get("max_replans")
        used = (run.get("usage") or {}).get("replans", 0)
        if limit is not None and used >= limit:
            self.ledger.emit(run_id, "replan_refused", {"reason": "max_replans", "used": used})
            return False
        return True

    def _plan(self, run_id: str, mode: str, reason: str) -> None:
        """Ask the planner for tasks. Initial planning falls back to one task = the
        goal if the planner fails; a replan that yields nothing fails the run."""
        run = self.ledger.require_run(run_id)
        if mode == "replan":
            self.ledger.emit(run_id, "replan_requested", {"reason": reason})
        self.ledger.transition(run_id, "planning", event="planning_started",
                               data={"mode": mode, "reason": reason})
        tasks = self.ledger.list_tasks(run_id)
        step = self.ledger.start_step(run_id, "", kind="plan", input={"mode": mode, "reason": reason})
        usage = {**_USAGE_ZERO, **(run.get("usage") or {})}
        pctx = PlanContext(engine=self, run=run, mode=mode, reason=reason, tasks=tasks,
                           budget_remaining=self._remaining(run.get("budget") or {}, usage),
                           workspace=self.workspace(run_id))
        try:
            res = self.planner(pctx)
            if not isinstance(res, PlanResult):
                raise TypeError(f"planner returned {type(res).__name__}, expected PlanResult")
        except Exception as e:
            res = PlanResult(ok=False, error=f"{type(e).__name__}: {e}", model_calls=0)
        try:
            specs = Ledger.normalize_task_specs(res.tasks) if (res.ok and res.tasks) else []
        except LedgerError as e:
            res.ok, res.error, specs = False, f"invalid plan: {e}", []
        self._account(run_id, {}, step, res)
        self.ledger.finish_step(step["step_id"], "completed" if specs else "failed",
                                output=res.output or "", error=res.error or ("" if specs else "no tasks in plan"),
                                meta=res.meta)
        source = "planner"
        if not specs:
            if mode == "replan":
                self._end(run_id, "failed", error=f"replan produced no tasks ({res.error or 'empty plan'})",
                                       data={"reason": "replan_failed"})
                return
            specs = [{"title": run["goal"][:200], "instructions": run["goal"]}]
            source = "fallback"
            self.ledger.emit(run_id, "plan_fallback", {"error": res.error or "no PLAN: lines"})
        if mode == "replan":
            for t in tasks:
                if t["status"] in ("pending", "running"):
                    self.ledger.update_task(t["task_id"], status="skipped", event="task_superseded",
                                            data={"reason": reason})
        for spec in specs:
            self.ledger.add_task(run_id, spec["title"], spec.get("instructions", ""), meta=spec.get("meta"))
        prev = run.get("plan") or {}
        plan = {"version": int(prev.get("version", 0)) + 1, "source": source, "mode": mode,
                "reason": reason, "tasks": [t["title"] for t in self.ledger.list_tasks(run_id)
                                            if t["status"] not in ("skipped", "cancelled")]}
        usage = {**_USAGE_ZERO, **(self.ledger.require_run(run_id).get("usage") or {})}
        if mode == "replan":
            usage["replans"] += 1
            usage["failures"] = 0  # the new plan gets a fresh failure budget; max_steps still bounds the run
        self.ledger.update_run(run_id, plan=plan, usage=usage)
        self.ledger.emit(run_id, "plan_created", plan)
        # Plan approval: the judge checks whether the plan will actually achieve the
        # goal before any step runs. Every fresh plan (initial + replan) is approved;
        # a rejected plan is sent back for a replan carrying the judge's reason (bounded
        # by max_replans).
        if self.plan_judge is not None and specs:
            ok, why = self._judge_plan(run, plan)
            if not ok:
                self.ledger.emit(run_id, "plan_rejected", {"reason": why, "mode": mode})
                self.ledger.update_run(run_id, plan={**plan, "pending_replan":
                                                     f"plan rejected by judge: {why}"})
        self.ledger.transition(run_id, "running", event="planning_finished",
                               data={"tasks": len(specs), "version": plan["version"]})
        self.checkpoint(run_id, reason="plan")

    def _finish_complete(self, run_id: str, tasks: List[dict]) -> dict:
        done = [t for t in tasks if t["status"] == "completed"]
        result = done[-1]["result"] if done else ""
        self.ledger.update_run(run_id, current_task_id="", current_step_id="")
        self.checkpoint(run_id, reason="complete")
        self._log(f"run {run_id} completed")
        return self._end(run_id, "completed", result=result, data={"tasks_completed": len(done)})

    def _end(self, run_id: str, status: str, **kw) -> dict:
        """Single choke point for completed/failed: transition, then run hooks
        (skill stats, reflection, artifact capture). A hook never breaks a run."""
        run = self.ledger.transition(run_id, status, **kw)
        for hook in list(self.on_finish):
            try:
                hook(self, run)
            except Exception as e:
                self.ledger.emit(run_id, "hook_error", {"hook": getattr(hook, "__name__", "?"),
                                                        "error": f"{type(e).__name__}: {e}"})
        return run

    def _finish_cancel(self, run_id: str) -> dict:
        for t in self.ledger.list_tasks(run_id):
            if t["status"] in ("pending", "running"):
                self.ledger.update_task(t["task_id"], status="cancelled", event="task_cancelled")
        self.ledger.update_run(run_id, control="")
        self.checkpoint(run_id, reason="cancel")
        return self.ledger.transition(run_id, "cancelled")

    # ── budgets ──────────────────────────────────────────────────────────

    @staticmethod
    def _budget_exceeded(budget: dict, usage: dict) -> Optional[str]:
        for key, ukey in _BUDGET_TO_USAGE.items():
            if key in ("max_failures", "max_replans"):
                continue  # enforced where they apply (retry-vs-fail, replan-vs-fail)
            limit = budget.get(key)
            if limit is not None and usage.get(ukey, 0) >= limit:
                return key
        return None

    @staticmethod
    def _remaining(budget: dict, usage: dict) -> dict:
        out = {}
        for key, ukey in _BUDGET_TO_USAGE.items():
            limit = budget.get(key)
            out[key] = None if limit is None else max(0, limit - usage.get(ukey, 0))
        return out

    # ── checkpoints ──────────────────────────────────────────────────────

    def snapshot(self, run_id: str) -> dict:
        run = self.ledger.require_run(run_id)
        tasks = self.ledger.list_tasks(run_id)
        return {
            "run_id": run_id,
            "status": run["status"],
            "goal": run["goal"],
            "attempt": run.get("attempt", 1),
            "plan": run.get("plan") or {},
            "current_task_id": run.get("current_task_id", ""),
            "current_step_id": run.get("current_step_id", ""),
            "tasks": [{"task_id": t["task_id"], "seq": t["seq"], "title": t["title"],
                       "status": t["status"], "attempts": t["attempts"],
                       "result": (t["result"] or "")[:_RESULT_PREVIEW], "error": t["error"]}
                      for t in tasks],
            "observations": [{"task_id": t["task_id"], "result": (t["result"] or "")[:_RESULT_PREVIEW]}
                             for t in tasks if t["status"] == "completed"],
            "artifacts": [{"artifact_id": a["artifact_id"], "path": a["path"], "sha256": a["sha256"]}
                          for a in self.ledger.list_artifacts(run_id)],
            "budget": run.get("budget") or {},
            "usage": run.get("usage") or {},
            "profile": run.get("profile", ""),
            "model": run.get("model", ""),
            "approval_state": run.get("approval_state", ""),
            "meta": run.get("meta") or {},
        }

    def checkpoint(self, run_id: str, reason: str = "manual") -> dict:
        state = self.snapshot(run_id)
        cp = self.ledger.create_checkpoint(run_id, state, reason=reason)
        ws = self.workspace(run_id)
        if ws is not None:
            try:
                ws.ensure().write_checkpoint_mirror(cp["seq"], state)
            except OSError as e:
                self.ledger.emit(run_id, "checkpoint_mirror_failed", {"error": str(e)})
        return cp

    def restore_checkpoint(self, run_id: str, checkpoint_id: str) -> dict:
        """Roll task state back to a checkpoint (tasks completed after it re-run).
        History is not rewritten: the restore itself is an event."""
        cp = self.ledger.get_checkpoint(checkpoint_id)
        if cp is None or cp["run_id"] != run_id:
            raise LedgerError(f"checkpoint {checkpoint_id} does not belong to run {run_id}")
        saved = {t["task_id"]: t for t in (cp["state"] or {}).get("tasks", [])}
        for t in self.ledger.list_tasks(run_id):
            s = saved.get(t["task_id"])
            if s is None:
                continue
            status = "pending" if s["status"] in ("running", "failed", "cancelled") else s["status"]
            self.ledger.update_task(t["task_id"], status=status, result=s.get("result", ""),
                                    error=s.get("error", ""))
        self.ledger.emit(run_id, "checkpoint_restored", {"checkpoint_id": checkpoint_id, "seq": cp["seq"]})
        return cp

    # ── approvals ────────────────────────────────────────────────────────

    @staticmethod
    def _needs_approval(run: dict, task: dict) -> bool:
        meta = task.get("meta") or {}
        if meta.get("approved"):
            return False
        return bool(meta.get("requires_approval") or (run.get("meta") or {}).get("approve_each_task"))

    def _request_approval(self, run_id: str, task: dict) -> None:
        import json as _json
        req = {"task_id": task["task_id"], "title": task["title"],
               "instructions": task.get("instructions", ""), "requested_at": _now()}
        self.checkpoint(run_id, reason="awaiting_approval")
        self.ledger.transition(run_id, "awaiting_approval", event="approval_requested",
                               data=req, approval_state=_json.dumps(req))
        self._log(f"run {run_id} awaiting approval for task {task['title']!r}")

    def _pending_approval(self, run_id: str) -> dict:
        import json as _json
        run = self.ledger.require_run(run_id)
        if run["status"] != "awaiting_approval":
            raise InvalidTransition(f"run {run_id} is {run['status']}, not awaiting_approval")
        try:
            return _json.loads(run.get("approval_state") or "{}")
        except ValueError:
            return {}

    def approve(self, run_id: str, actor: str = "user", note: str = "", background: bool = True) -> dict:
        req = self._pending_approval(run_id)
        task = self.ledger.get_task(req.get("task_id", ""))
        if task is None:
            raise LedgerError(f"run {run_id}: approval refers to an unknown task")
        self.ledger.update_task(task["task_id"], meta={**(task.get("meta") or {}), "approved": True,
                                                       "approved_by": actor},
                                event="approval_granted", data={"actor": actor, "note": note})
        self.ledger.update_run(run_id, approval_state="")
        return self.resume(run_id, background=background, actor=actor)

    def reject(self, run_id: str, actor: str = "user", reason: str = "", background: bool = True) -> dict:
        req = self._pending_approval(run_id)
        task = self.ledger.get_task(req.get("task_id", ""))
        why = f"rejected by {actor}" + (f": {reason}" if reason else "")
        if task is not None:
            self.ledger.update_task(task["task_id"], status="failed", error=why, finished_at=_now(),
                                    event="approval_rejected", data={"actor": actor, "reason": reason})
        run = self.ledger.require_run(run_id)
        plan = dict(run.get("plan") or {})
        plan["pending_replan"] = f"task {req.get('title', '?')!r} was {why}"
        self.ledger.update_run(run_id, approval_state="", plan=plan)
        return self.resume(run_id, background=background, actor=actor)

    # ── external runs (driven by an extension over HTTP, e.g. the swarm) ──

    @staticmethod
    def _refuse_external(run: dict) -> None:
        if (run.get("meta") or {}).get("external"):
            raise InvalidTransition(f"run {run['run_id']} is driven by {run['meta']['external']} — "
                                    "resume/retry it from that driver (e.g. swarm --resume-run)")

    def external_transition(self, run_id: str, status: str, *, result: str = "", error: str = "",
                            actor: str = "extension") -> dict:
        run = self.ledger.require_run(run_id)
        if not (run.get("meta") or {}).get("external"):
            raise InvalidTransition(f"run {run_id} is harness-driven; only external runs accept status updates")
        if status in ("completed", "failed"):
            return self._end(run_id, status, result=result, error=error, actor=actor)
        return self.ledger.transition(run_id, status, actor=actor)

    def request_replan(self, run_id: str, reason: str = "", actor: str = "user") -> dict:
        """Ask for a replan of the remaining work (applied at the next step boundary)."""
        run = self.ledger.require_run(run_id)
        if run["status"] in TERMINAL_STATES:
            raise InvalidTransition(f"run {run_id} is {run['status']} — nothing to replan")
        if self.planner is None:
            raise LedgerError("this harness has no planner")
        plan = dict(run.get("plan") or {})
        plan["pending_replan"] = f"requested by {actor}: {reason or 'no reason given'}"
        self.ledger.update_run(run_id, plan=plan)
        if self._executing_now(run) or run["status"] == "awaiting_approval":
            return self.ledger.get_run(run_id)
        return self.resume(run_id, actor=actor)

    def add_note(self, run_id: str, text: str = "", actor: str = "user") -> dict:
        """Inject a user note into the next task step's context (applied at the next
        step boundary). A lighter-weight alternative to /replan: the note is handed
        to the running model as a user message without restructuring the plan."""
        run = self.ledger.require_run(run_id)
        if run["status"] in TERMINAL_STATES:
            raise InvalidTransition(f"run {run_id} is {run['status']} — nothing to note")
        text = (text or "").strip()
        if not text:
            raise LedgerError("note text is required")
        plan = dict(run.get("plan") or {})
        plan["pending_note"] = text
        self.ledger.update_run(run_id, plan=plan)
        self.ledger.emit(run_id, "note_added", {"text": text}, actor=actor)
        if self._executing_now(run) or run["status"] == "awaiting_approval":
            return self.ledger.get_run(run_id)
        return self.resume(run_id, actor=actor)

    # ── control ──────────────────────────────────────────────────────────

    def _executing_now(self, run: dict) -> bool:
        """True when some live process (this one or another) is executing the run."""
        if run["status"] not in ACTIVE_STATES:
            return False
        if self.is_executing(run["run_id"]):
            return True
        orphans = {r["run_id"] for r in self.ledger.orphaned_runs(self.lease_seconds)}
        return run["run_id"] not in orphans

    def pause(self, run_id: str, actor: str = "user") -> dict:
        run = self.ledger.require_run(run_id)
        if run["status"] == "paused":
            return run
        if run["status"] in TERMINAL_STATES:
            raise InvalidTransition(f"run {run_id} is {run['status']} — nothing to pause")
        if self._executing_now(run):
            self.ledger.request_control(run_id, "pause", actor=actor)
            return self.ledger.get_run(run_id)
        self.checkpoint(run_id, reason="pause")
        return self.ledger.transition(run_id, "paused", actor=actor)

    def cancel(self, run_id: str, actor: str = "user") -> dict:
        run = self.ledger.require_run(run_id)
        if run["status"] == "cancelled":
            return run
        if run["status"] in TERMINAL_STATES:
            raise InvalidTransition(f"run {run_id} is {run['status']} — nothing to cancel")
        if self._executing_now(run):
            self.ledger.request_control(run_id, "cancel", actor=actor)
            return self.ledger.get_run(run_id)
        return self._finish_cancel(run_id)

    def resume(self, run_id: str, checkpoint_id: Optional[str] = None, background: bool = True,
               actor: str = "user") -> dict:
        run = self.ledger.require_run(run_id)
        self._refuse_external(run)
        if run["status"] in TERMINAL_STATES:
            raise InvalidTransition(f"run {run_id} is {run['status']} — use retry instead")
        if self._executing_now(run):
            return run  # already running
        self.ledger.update_run(run_id, control="")
        if checkpoint_id:
            self.restore_checkpoint(run_id, checkpoint_id)
        self.ledger.emit(run_id, "resume_requested", {"checkpoint_id": checkpoint_id or ""}, actor=actor)
        if background:
            self.start(run_id)
            return self.ledger.get_run(run_id)
        return self.execute(run_id)

    def retry(self, run_id: str, background: bool = True, actor: str = "user") -> dict:
        """Re-run a failed/cancelled run. Completed tasks are kept; the rest re-run."""
        run = self.ledger.require_run(run_id)
        self._refuse_external(run)
        if run["status"] not in ("failed", "cancelled"):
            raise InvalidTransition(f"run {run_id} is {run['status']} — only failed/cancelled runs can be retried")
        for t in self.ledger.list_tasks(run_id):
            if t["status"] in ("failed", "cancelled", "running"):
                self.ledger.update_task(t["task_id"], status="pending", error="", finished_at="")
        usage = {**_USAGE_ZERO, **(run.get("usage") or {})}
        usage["failures"] = 0
        attempt = int(run.get("attempt") or 1) + 1
        self.ledger.transition(run_id, "created", event="run_retry", actor=actor,
                               data={"attempt": attempt}, attempt=attempt, usage=usage,
                               error="", result="", control="")
        if background:
            self.start(run_id)
            return self.ledger.get_run(run_id)
        return self.execute(run_id)

    # ── recovery ─────────────────────────────────────────────────────────

    def recover(self, auto_resume: bool = False) -> List[str]:
        """Find runs left active by a dead process and make them resumable.

        The in-flight step is marked ``interrupted``; the run goes to ``paused``
        with a ``run_interrupted`` event. With auto_resume the run is restarted
        (its interrupted step re-executes — at-least-once semantics)."""
        recovered = []
        for run in self.ledger.orphaned_runs(self.lease_seconds, exclude_owner=self.owner):
            run_id = run["run_id"]
            if self.is_executing(run_id):
                continue
            prev_owner = run.get("owner") or ""
            if not self.ledger.claim(run_id, self.owner, self.lease_seconds):
                continue
            try:
                for s in self.ledger.list_steps(run_id):
                    if s["status"] == "running":
                        self.ledger.finish_step(s["step_id"], "interrupted",
                                                error="process stopped during this step")
                self.ledger.transition(run_id, "paused", event="run_interrupted",
                                       data={"previous_owner": prev_owner,
                                             "previous_status": run["status"]})
                self.checkpoint(run_id, reason="recovered")
                recovered.append(run_id)
                self._log(f"run {run_id} recovered after interruption (was {run['status']})")
            finally:
                self.ledger.release(run_id, self.owner)
            if auto_resume:
                self.resume(run_id, actor="harness")
        return recovered


def _now() -> str:
    from mneme.harness.ledger import now_iso
    return now_iso()


def _record_skill_outcomes(engine: "RunEngine", run: dict) -> None:
    used = set()
    for st in engine.ledger.list_steps(run["run_id"]):
        used.update((st.get("meta") or {}).get("skills") or [])
    if used:
        engine.skills.record_outcome(used, run["status"] == "completed")
        engine.ledger.emit(run["run_id"], "skills_recorded", {"skills": sorted(used),
                                                               "success": run["status"] == "completed"})


def _capture_artifacts(engine: "RunEngine", run: dict) -> None:
    """on_finish hook: register every file in the run's artifacts/ dir not yet recorded."""
    ws = engine.workspace(run["run_id"])
    if ws is None:
        return
    import os as _os
    base = ws.dir("artifacts")
    if not _os.path.isdir(base):
        return
    known = {a["path"] for a in engine.ledger.list_artifacts(run["run_id"])}
    for root, dirs, files in _os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            p = _os.path.abspath(_os.path.join(root, f))
            if f.startswith(".") or p in known:
                continue
            engine.ledger.add_artifact(run["run_id"], p, description="captured from artifacts/",
                                       provenance={"captured": "auto", "run_status": run["status"]})

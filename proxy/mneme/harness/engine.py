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
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from mneme.harness.ledger import (
    ACTIVE_STATES, TERMINAL_STATES, InvalidTransition, Ledger, LedgerError, process_owner,
)
from mneme.harness.workspace import RunWorkspace

DEFAULT_BUDGET = {
    "max_steps": 100,        # hard safety cap on steps per run
    "max_failures": 3,       # failed steps before the run fails
    "max_model_calls": None,
    "max_tool_calls": None,
    "max_replans": None,     # recorded now, enforced once planning exists (Phase 2)
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


class RunEngine:
    def __init__(self, ledger: Ledger, executor: Executor, *, runs_root: Optional[str] = None,
                 lease_seconds: float = 120.0, owner_tag: str = "engine",
                 log: Optional[Callable[[str], None]] = None):
        self.ledger = ledger
        self.executor = executor
        self.runs_root = runs_root
        self.lease_seconds = float(lease_seconds)
        self.owner = process_owner(owner_tag)
        self._log = log or (lambda msg: print(f"  [HARNESS] {msg}", flush=True))
        self._workers: Dict[str, threading.Thread] = {}
        self._workers_lock = threading.Lock()

    # ── creation ─────────────────────────────────────────────────────────

    def create(self, goal: str, tasks: Optional[List] = None, *, budget: Optional[dict] = None,
               start: bool = False, **kw) -> dict:
        run = self.ledger.create_run(goal, tasks, budget=merge_budget(budget), **kw)
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

            tasks = self.ledger.list_tasks(run_id)
            task = next((t for t in tasks if t["status"] in ("pending", "running")), None)
            if task is None:
                return self._finish_complete(run_id, tasks)

            exceeded = self._budget_exceeded(run.get("budget") or {}, usage)
            if exceeded:
                self.ledger.emit(run_id, "budget_exceeded", {"budget": exceeded, "usage": usage})
                self.checkpoint(run_id, reason="budget_exceeded")
                return self.ledger.transition(run_id, "failed", error=f"budget exceeded: {exceeded}",
                                              data={"reason": "budget_exceeded", "budget": exceeded})

            self._run_one_step(run, task, tasks, usage)

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
        ctx = StepContext(
            engine=self, run=run, task=task, step=step, workspace=self.workspace(run_id),
            observations=[{"task_id": t["task_id"], "title": t["title"], "result": t["result"]}
                          for t in tasks if t["status"] == "completed"],
            budget_remaining=self._remaining(run.get("budget") or {}, usage),
            attempt=task["attempts"],
        )
        try:
            result = self.executor(ctx)
            if not isinstance(result, StepResult):
                raise TypeError(f"executor returned {type(result).__name__}, expected StepResult")
        except Exception as e:
            result = StepResult(ok=False, error=f"{type(e).__name__}: {e}", model_calls=0,
                                meta={"traceback": traceback.format_exc()[-2000:]})

        for tc in result.tool_calls or []:
            self.ledger.record_tool_call(
                run_id, str(tc.get("tool") or "?"), tc.get("args") or {},
                result=str(tc.get("result") or "")[:_RESULT_PREVIEW], status=tc.get("status") or "",
                elapsed_ms=int(tc.get("elapsed_ms") or 0), task_id=task["task_id"], step_id=step["step_id"])

        usage = {**_USAGE_ZERO, **(self.ledger.require_run(run_id).get("usage") or {})}
        usage["steps"] += 1
        usage["model_calls"] += int(result.model_calls or 0)
        usage["tool_calls"] += len(result.tool_calls or [])
        usage["cost"] = round(usage["cost"] + float(result.cost or 0.0), 6)
        if not result.ok:
            usage["failures"] += 1
        self.ledger.update_run(run_id, usage=usage)
        self.ledger.finish_step(step["step_id"], "completed" if result.ok else "failed",
                                output=result.output or "", error=result.error or "", meta=result.meta)

        if result.ok and result.done:
            self.ledger.update_task(task["task_id"], status="completed", result=result.output or "",
                                    error="", finished_at=_now(), event="task_completed",
                                    data={"title": task["title"]})
        elif result.ok:
            self.ledger.emit(run_id, "task_continued", {"title": task["title"]},
                             task_id=task["task_id"], step_id=step["step_id"])
        else:
            max_f = (run.get("budget") or {}).get("max_failures")
            final = (not result.retryable) or (max_f is not None and usage["failures"] >= max_f)
            self.ledger.update_task(
                task["task_id"], status="failed" if final else "pending", error=result.error or "",
                finished_at=_now() if final else "", event="task_failed",
                data={"title": task["title"], "error": result.error, "attempt": task["attempts"],
                      "will_retry": not final})
            if final:
                self.checkpoint(run_id, reason="task_failed")
                self.ledger.transition(run_id, "failed", error=result.error or "task failed",
                                       data={"reason": "task_failed", "task_id": task["task_id"]})
                return
        self.checkpoint(run_id, reason="step")

    def _finish_complete(self, run_id: str, tasks: List[dict]) -> dict:
        done = [t for t in tasks if t["status"] == "completed"]
        result = done[-1]["result"] if done else ""
        self.ledger.update_run(run_id, current_task_id="", current_step_id="")
        self.checkpoint(run_id, reason="complete")
        self._log(f"run {run_id} completed")
        return self.ledger.transition(run_id, "completed", result=result,
                                      data={"tasks_completed": len(done)})

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
            if key == "max_failures":
                continue  # enforced at failure time (it decides retry vs fail)
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

"""Harness run engine: execution, checkpoints, budgets, pause/resume/cancel/retry,
failure handling, and recovery after a REAL process crash. Standalone — no proxy.

Run: python3 tests/test_harness_engine.py
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import textwrap
import unittest

_PROXY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY)

from mneme.harness import Ledger, RunEngine, StepResult, InvalidTransition  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402


class Script:
    """Executor that replays scripted StepResults and records what it saw."""

    def __init__(self, results=None, default=None):
        self.results = list(results or [])
        self.default = default or (lambda ctx: StepResult(output=f"did {ctx.task['title']}"))
        self.seen = []

    def __call__(self, ctx):
        self.seen.append((ctx.task["title"], ctx.attempt, [o["title"] for o in ctx.observations]))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r(ctx) if callable(r) else r
        return self.default(ctx)


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_engine_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.runs_root = os.path.join(self.tmp, "runs")

    def engine(self, executor, **kw):
        return RunEngine(self.led, executor, runs_root=self.runs_root, log=lambda m: None, **kw)

    def types(self, rid):
        return [e["type"] for e in self.led.events(rid)]


class TestExecution(EngineTestCase):
    def test_multi_task_completes_in_order_with_observations(self):
        ex = Script()
        eng = self.engine(ex)
        run = eng.create("build it", ["plan", "code", "test"])
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"], "did test")
        self.assertEqual([s[0] for s in ex.seen], ["plan", "code", "test"])
        self.assertEqual(ex.seen[2][2], ["plan", "code"])  # prior results were provided
        self.assertEqual(out["usage"]["steps"], 3)
        t = self.types(run["run_id"])
        for needed in ("run_created", "plan_created", "run_started", "task_started",
                       "step_started", "step_completed", "task_completed",
                       "checkpoint_created", "run_completed"):
            self.assertIn(needed, t)
        self.assertIsNone(out["owner"] or None)  # claim released

    def test_workspace_and_checkpoint_mirror(self):
        eng = self.engine(Script())
        run = eng.create("g")
        eng.execute(run["run_id"])
        ws = os.path.join(self.runs_root, run["run_id"])
        for sub in ("input", "workspace", "artifacts", "logs", "checkpoints"):
            self.assertTrue(os.path.isdir(os.path.join(ws, sub)))
        mirrors = sorted(os.listdir(os.path.join(ws, "checkpoints")))
        self.assertTrue(mirrors)
        with open(os.path.join(ws, "checkpoints", mirrors[-1])) as f:
            self.assertEqual(json.load(f)["status"], "running")  # snapshot taken before the final transition
        self.assertEqual(self.led.latest_checkpoint(run["run_id"])["reason"], "complete")

    def test_continue_same_task(self):
        ex = Script([StepResult(output="half", done=False), StepResult(output="all")])
        eng = self.engine(ex)
        run = eng.create("g")
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"], "all")
        self.assertEqual(len(self.led.list_steps(run["run_id"])), 2)
        self.assertIn("task_continued", self.types(run["run_id"]))

    def test_tool_calls_and_artifacts_recorded(self):
        def step(ctx):
            p = ctx.workspace.resolve("artifacts", "r.txt")
            with open(p, "w") as f:
                f.write("x")
            ctx.add_artifact(p, description="report")
            return StepResult(output="ok", model_calls=2, tool_calls=[
                {"tool": "bash", "args": {"command": "ls"}, "result": "a", "status": "success"},
                {"tool": "web_search", "args": {"query": "q"}, "result": "", "status": "failure"},
            ])
        eng = self.engine(Script([step]))
        run = eng.create("g")
        out = eng.execute(run["run_id"])
        self.assertEqual(out["usage"]["tool_calls"], 2)
        self.assertEqual(out["usage"]["model_calls"], 2)
        calls = self.led.list_tool_calls(run["run_id"])
        self.assertEqual([c["tool"] for c in calls], ["bash", "web_search"])
        arts = self.led.list_artifacts(run["run_id"])
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]["sha256"])


class TestFailures(EngineTestCase):
    def test_failure_then_retry_within_budget(self):
        ex = Script([StepResult(ok=False, error="boom"), StepResult(output="fixed")])
        eng = self.engine(ex)
        run = eng.create("g", budget={"max_failures": 3})
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual([s[1] for s in ex.seen], [1, 2])  # attempt counter advanced
        self.assertIn("task_failed", self.types(run["run_id"]))

    def test_executor_exception_is_a_failure_not_a_crash(self):
        ex = Script([RuntimeError("kaboom")], default=lambda c: StepResult(output="ok"))
        eng = self.engine(ex)
        run = eng.create("g")
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "completed")
        failed = [s for s in self.led.list_steps(run["run_id"]) if s["status"] == "failed"]
        self.assertIn("kaboom", failed[0]["error"])

    def test_max_failures_fails_run(self):
        eng = self.engine(Script(default=lambda c: StepResult(ok=False, error="nope")))
        run = eng.create("g", budget={"max_failures": 2})
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["usage"]["failures"], 2)
        self.assertEqual(self.led.list_tasks(run["run_id"])[0]["status"], "failed")

    def test_non_retryable_fails_immediately(self):
        eng = self.engine(Script([StepResult(ok=False, error="fatal", retryable=False)]))
        run = eng.create("g", budget={"max_failures": 10})
        self.assertEqual(eng.execute(run["run_id"])["status"], "failed")

    def test_retry_keeps_completed_tasks(self):
        calls = {"n": 0}

        def flaky(ctx):
            if ctx.task["title"] == "b" and calls["n"] == 0:
                calls["n"] += 1
                return StepResult(ok=False, error="bad", retryable=False)
            return StepResult(output=ctx.task["title"])
        ex = Script(default=flaky)
        eng = self.engine(ex)
        run = eng.create("g", ["a", "b"])
        self.assertEqual(eng.execute(run["run_id"])["status"], "failed")
        out = eng.retry(run["run_id"], background=False)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["attempt"], 2)
        self.assertEqual([s[0] for s in ex.seen], ["a", "b", "b"])  # "a" not redone
        self.assertIn("run_retry", self.types(run["run_id"]))

    def test_retry_rejects_non_terminal(self):
        eng = self.engine(Script())
        run = eng.create("g")
        with self.assertRaises(InvalidTransition):
            eng.retry(run["run_id"])


class TestBudgets(EngineTestCase):
    def test_max_steps(self):
        eng = self.engine(Script(default=lambda c: StepResult(output="more", done=False)))
        run = eng.create("g", budget={"max_steps": 3})
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("max_steps", out["error"])
        self.assertEqual(out["usage"]["steps"], 3)
        self.assertIn("budget_exceeded", self.types(run["run_id"]))

    def test_max_tool_calls(self):
        eng = self.engine(Script(default=lambda c: StepResult(
            output="x", tool_calls=[{"tool": "bash"}, {"tool": "bash"}])))
        run = eng.create("g", ["a", "b", "c"], budget={"max_tool_calls": 3})
        out = eng.execute(run["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("max_tool_calls", out["error"])

    def test_budget_remaining_given_to_executor(self):
        seen = {}

        def step(ctx):
            seen.update(ctx.budget_remaining)
            return StepResult(output="x")
        eng = self.engine(Script([step]))
        run = eng.create("g", budget={"max_model_calls": 5})
        eng.execute(run["run_id"])
        self.assertEqual(seen["max_model_calls"], 5)
        self.assertIsNone(seen["max_cost"])

    def test_unknown_budget_key_rejected(self):
        eng = self.engine(Script())
        with self.assertRaises(LedgerError):
            eng.create("g", budget={"max_bananas": 1})


class TestControl(EngineTestCase):
    def test_pause_between_steps_then_resume(self):
        gate = threading.Event()
        release = threading.Event()

        def step(ctx):
            if ctx.task["title"] == "a":
                gate.set()
                release.wait(5)
            return StepResult(output=ctx.task["title"])
        ex = Script(default=step)
        eng = self.engine(ex)
        run = eng.create("g", ["a", "b"])
        rid = run["run_id"]
        eng.start(rid)
        self.assertTrue(gate.wait(5))
        eng.pause(rid)                      # worker is mid-step: request, not force
        self.assertEqual(self.led.get_run(rid)["control"], "pause")
        release.set()
        out = eng.wait(rid, 5)
        self.assertEqual(out["status"], "paused")
        self.assertEqual([s[0] for s in ex.seen], ["a"])
        tasks = {t["title"]: t["status"] for t in self.led.list_tasks(rid)}
        self.assertEqual(tasks, {"a": "completed", "b": "pending"})
        out = eng.resume(rid, background=False)
        self.assertEqual(out["status"], "completed")
        self.assertEqual([s[0] for s in ex.seen], ["a", "b"])
        t = self.types(rid)
        self.assertIn("pause_requested", t)
        self.assertIn("run_paused", t)
        self.assertIn("run_resumed", t)

    def test_pause_idle_run_directly(self):
        eng = self.engine(Script())
        rid = eng.create("g")["run_id"]
        self.assertEqual(eng.pause(rid)["status"], "paused")
        self.assertEqual(eng.resume(rid, background=False)["status"], "completed")

    def test_cancel_running(self):
        gate, release = threading.Event(), threading.Event()

        def step(ctx):
            gate.set()
            release.wait(5)
            return StepResult(output="x")
        eng = self.engine(Script(default=step))
        rid = eng.create("g", ["a", "b"])["run_id"]
        eng.start(rid)
        gate.wait(5)
        eng.cancel(rid)
        release.set()
        out = eng.wait(rid, 5)
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual([t["status"] for t in self.led.list_tasks(rid)], ["completed", "cancelled"])
        with self.assertRaises(InvalidTransition):
            eng.resume(rid)

    def test_restore_checkpoint_guards(self):
        eng = self.engine(Script())
        rid = eng.create("g", ["a", "b"])["run_id"]
        eng.execute(rid)
        first = [c for c in self.led.list_checkpoints(rid) if c["reason"] == "step"][0]
        with self.assertRaises(InvalidTransition):  # completed runs don't resume
            eng.resume(rid, checkpoint_id=first["checkpoint_id"])
        rid2 = eng.create("g", ["a", "b"])["run_id"]
        with self.assertRaises(LedgerError):  # another run's checkpoint
            eng.restore_checkpoint(rid2, first["checkpoint_id"])

    def test_restore_checkpoint_on_paused_run(self):
        ex = Script()
        eng = self.engine(ex)
        rid = eng.create("g", ["a", "b", "c"])["run_id"]
        # pause after first two tasks via a stepping executor
        def step(ctx):
            if ctx.task["title"] == "b":
                self.led.request_control(rid, "pause")
            return StepResult(output=ctx.task["title"])
        eng.executor = step
        self.assertEqual(eng.execute(rid)["status"], "paused")
        after_a = [c for c in self.led.list_checkpoints(rid) if c["reason"] == "step"][0]
        eng.executor = ex
        out = eng.resume(rid, checkpoint_id=after_a["checkpoint_id"], background=False)
        self.assertEqual(out["status"], "completed")
        self.assertEqual([s[0] for s in ex.seen], ["b", "c"])  # b re-ran from the checkpoint
        self.assertIn("checkpoint_restored", self.types(rid))


_CRASH_CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {proxy!r})
    from mneme.harness import Ledger, RunEngine, StepResult
    led = Ledger({db!r})
    def step(ctx):
        with open({log!r}, "a") as f:
            f.write(ctx.task["title"] + "\\n")
        if ctx.task["title"] == "b":
            os._exit(9)            # hard crash mid-step: no cleanup, no finally
        return StepResult(output="result-" + ctx.task["title"])
    eng = RunEngine(led, step, runs_root={runs!r}, lease_seconds=60, log=lambda m: None)
    run = eng.create("crash test", ["a", "b", "c"])
    print(run["run_id"], flush=True)
    eng.execute(run["run_id"])
""")


class TestRestartRecovery(EngineTestCase):
    def test_real_crash_then_recover_and_resume(self):
        db = os.path.join(self.tmp, "harness.db")
        log = os.path.join(self.tmp, "exec.log")
        code = _CRASH_CHILD.format(proxy=_PROXY, db=db, log=log, runs=self.runs_root)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 9, p.stderr)
        rid = p.stdout.strip().splitlines()[0]

        # State as the dead process left it: still "running", owned by a dead pid.
        led = Ledger(db)
        run = led.get_run(rid)
        self.assertEqual(run["status"], "running")
        self.assertTrue(run["owner"])

        seen = []

        def step(ctx):
            seen.append((ctx.task["title"], [o["result"] for o in ctx.observations]))
            return StepResult(output="result-" + ctx.task["title"])
        eng = RunEngine(led, step, runs_root=self.runs_root, lease_seconds=60, log=lambda m: None)
        self.assertEqual(eng.recover(), [rid])
        run = led.get_run(rid)
        self.assertEqual(run["status"], "paused")
        self.assertEqual(run["owner"], "")
        steps = led.list_steps(rid)
        self.assertEqual([s["status"] for s in steps], ["completed", "interrupted"])
        self.assertIn("run_interrupted", [e["type"] for e in led.events(rid)])

        out = eng.resume(rid, background=False)
        self.assertEqual(out["status"], "completed")
        # "a" was not re-executed; "b" re-ran (at-least-once), and saw a's persisted result.
        self.assertEqual(seen, [("b", ["result-a"]), ("c", ["result-a", "result-b"])])
        with open(log) as f:
            self.assertEqual(f.read().split(), ["a", "b"])
        self.assertEqual(eng.recover(), [])  # nothing left to recover

    def test_recover_leaves_live_owner_alone(self):
        eng = self.engine(Script())
        rid = eng.create("g")["run_id"]
        self.led.transition(rid, "running")
        other = RunEngine(self.led, Script(), owner_tag="other", lease_seconds=60, log=lambda m: None)
        self.assertTrue(self.led.claim(rid, other.owner, 60))  # a live, fresh owner
        self.assertEqual(eng.recover(), [])
        self.assertEqual(self.led.get_run(rid)["status"], "running")
        with self.assertRaises(LedgerError):
            eng.execute(rid)  # cannot steal it either

    def test_auto_resume(self):
        ex = Script()
        eng = self.engine(ex)
        rid = eng.create("g")["run_id"]
        self.led.transition(rid, "running")  # orphan: active, no owner
        self.assertEqual(eng.recover(auto_resume=True), [rid])
        self.assertEqual(eng.wait(rid, 5)["status"], "completed")


if __name__ == "__main__":
    unittest.main()

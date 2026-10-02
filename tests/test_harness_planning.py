"""Phase 2: plan parsing, deterministic verification, initial planning, replanning,
verification-driven retry, and planner prompt construction. Standalone — no proxy.

Run: python3 tests/test_harness_planning.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.planning import PlanResult, parse_plan, find_replan  # noqa: E402
from mneme.harness import verify as V  # noqa: E402
from mneme.harness.chat_executor import make_chat_planner, make_chat_executor  # noqa: E402


class TestParsePlan(unittest.TestCase):
    def test_natural_language_with_tags(self):
        text = ("I'll check the docs first, then write the note.\n"
                "PLAN: Find the version\n"
                "  - plan 2: Write it to notes.txt\n"
                "VERIFY: `test -s notes.txt`\n"
                "**PLAN:** Report back\n"
                "VERIFY: none\n"
                "This plan: is not a tag because it doesn't start the line with PLAN")
        tasks = parse_plan(text)
        self.assertEqual([t["title"] for t in tasks], ["Find the version", "Write it to notes.txt", "Report back"])
        self.assertEqual(tasks[1]["verify"], [{"type": "command", "command": "test -s notes.txt"}])
        self.assertNotIn("verify", tasks[2])

    def test_verify_before_any_plan_ignored_and_cap(self):
        self.assertEqual(parse_plan("VERIFY: true"), [])
        many = "\n".join(f"PLAN: t{i}" for i in range(20))
        self.assertEqual(len(parse_plan(many)), 8)
        self.assertEqual(parse_plan("no tags here"), [])

    def test_find_replan(self):
        self.assertEqual(find_replan("done.\nREPLAN: the API moved"), "the API moved")
        self.assertEqual(find_replan("all good"), "")

    def test_structured_verify_specs(self):
        text = ("PLAN: write the file\n"
                "VERIFY: file_contains out.html :: <script>\n"
                "PLAN: run the test\n"
                "VERIFY: file_exists out.html\n"
                "VERIFY: output_contains PASS\n")
        tasks = parse_plan(text)
        self.assertEqual(tasks[0]["verify"], [{"type": "file_contains", "path": "out.html", "text": "<script>"}])
        self.assertEqual(tasks[1]["verify"], [
            {"type": "file_exists", "path": "out.html"},
            {"type": "output_contains", "text": "PASS"},
        ])
        # a bare shell command still falls back to a command check
        self.assertEqual(parse_plan("PLAN: x\nVERIFY: grep -q foo notes.txt")[0]["verify"],
                         [{"type": "command", "command": "grep -q foo notes.txt"}])


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mneme_verify_")
        with open(os.path.join(self.d, "notes.txt"), "w") as f:
            f.write("python 3.13\n")

    def test_normalize(self):
        self.assertEqual(V.normalize("true"), [{"type": "command", "command": "true"}])
        self.assertEqual(V.normalize(None), [])
        for bad in ({"type": "nope"}, {"type": "file_exists"}, {"type": "output_matches", "pattern": "("}, 5):
            with self.assertRaises(V.VerifySpecError):
                V.normalize(bad)

    def test_checks(self):
        checks = V.normalize([
            "grep -q 3.13 notes.txt",
            {"type": "file_exists", "path": "notes.txt"},
            {"type": "file_contains", "path": "notes.txt", "text": "3.13"},
            {"type": "output_contains", "text": "wrote"},
            {"type": "output_matches", "pattern": r"(?i)WROTE\s+it"},
            {"type": "command", "command": "exit 3", "expect_exit": 3},
        ])
        ok, res = V.run_checks(checks, "I wrote it", self.d)
        self.assertTrue(ok, res)

    def test_failures_reported(self):
        checks = V.normalize([{"type": "file_exists", "path": "missing.txt"}, "false"])
        ok, res = V.run_checks(checks, "", self.d)
        self.assertFalse(ok)
        msg = V.summarize_failures(res)
        self.assertIn("missing.txt", msg)
        self.assertIn("exit 1", msg)

    def test_timeout(self):
        ok, res = V.run_checks([{"type": "command", "command": "sleep 5", "timeout": 0.2}], "", self.d)
        self.assertFalse(ok)
        self.assertIn("timed out", res[0]["detail"])

    def test_malformed_command_is_inconclusive(self):
        # Unquoted parens -> shell parse error. The command never ran, so it is NOT
        # evidence the work failed; it must not fail the task.
        ok, res = V.run_checks([{"type": "command", "command": "grep -q (unclosed /etc/passwd"}], "", self.d)
        self.assertTrue(ok, res)
        self.assertIn("malformed", res[0]["detail"])
        # A genuine no-match still fails.
        ok2, _ = V.run_checks([{"type": "command", "command": "grep -q THIS_NEVER_EXISTS notes.txt"}], "", self.d)
        self.assertFalse(ok2)


class Planner:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def __call__(self, pctx):
        self.calls.append((pctx.mode, pctx.reason, [(t["title"], t["status"]) for t in pctx.tasks]))
        p = self.plans.pop(0) if self.plans else PlanResult(ok=False, error="out of plans")
        if isinstance(p, Exception):
            raise p
        return p


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_plan_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.runs = os.path.join(self.tmp, "runs")

    def engine(self, executor, planner):
        return RunEngine(self.led, executor, planner=planner, runs_root=self.runs, log=lambda m: None)

    def types(self, rid):
        return [e["type"] for e in self.led.events(rid)]


class TestPlanning(EngineCase):
    def test_initial_plan_creates_tasks(self):
        pl = Planner([PlanResult(tasks=[{"title": "a"}, {"title": "b"}], output="PLAN: a\nPLAN: b")])
        seen = []
        eng = self.engine(lambda c: (seen.append(c.task["title"]), StepResult(output=c.task["title"]))[1], pl)
        rid = eng.create("goal")["run_id"]
        self.assertEqual(self.led.list_tasks(rid), [])          # nothing until planned
        self.assertEqual(self.led.get_run(rid)["plan"]["source"], "pending")
        out = eng.execute(rid)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(out["plan"]["source"], "planner")
        self.assertEqual(out["plan"]["version"], 1)
        steps = self.led.list_steps(rid)
        self.assertEqual(steps[0]["kind"], "plan")
        t = self.types(rid)
        for e in ("planning_started", "plan_created", "planning_finished"):
            self.assertIn(e, t)

    def test_plan_false_and_explicit_tasks_skip_planner(self):
        pl = Planner([])
        eng = self.engine(lambda c: StepResult(output="x"), pl)
        eng.execute(eng.create("g", ["x"])["run_id"])
        eng.execute(eng.create("g", plan=False)["run_id"])
        self.assertEqual(pl.calls, [])
        with self.assertRaises(LedgerError):
            eng.create("g", ["x"], plan=True)
        with self.assertRaises(LedgerError):
            RunEngine(self.led, lambda c: None).create("g", plan=True)

    def test_initial_plan_failure_falls_back_to_goal(self):
        for bad in (PlanResult(ok=False, error="no PLAN lines"), RuntimeError("planner down")):
            pl = Planner([bad])
            eng = self.engine(lambda c: StepResult(output=c.task["title"]), pl)
            out = eng.execute(eng.create("the goal")["run_id"])
            self.assertEqual(out["status"], "completed")
            self.assertEqual(out["result"], "the goal")
            self.assertEqual(out["plan"]["source"], "fallback")
            self.assertIn("plan_fallback", self.types(out["run_id"]))

    def test_invalid_plan_spec_falls_back(self):
        pl = Planner([PlanResult(tasks=[{"title": "a", "verify": {"type": "bogus"}}])])
        eng = self.engine(lambda c: StepResult(output="x"), pl)
        out = eng.execute(eng.create("g")["run_id"])
        self.assertEqual(out["plan"]["source"], "fallback")

    def test_replan_after_task_failure(self):
        pl = Planner([PlanResult(tasks=[{"title": "try api"}, {"title": "summarize"}]),
                      PlanResult(tasks=[{"title": "scrape page"}, {"title": "summarize"}])])

        def ex(c):
            if c.task["title"] == "try api":
                return StepResult(ok=False, error="403 forbidden", retryable=False)
            return StepResult(output=c.task["title"])
        eng = self.engine(ex, pl)
        out = eng.execute(eng.create("get data")["run_id"])
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["usage"]["replans"], 1)
        self.assertEqual(out["plan"]["version"], 2)
        mode, reason, seen = pl.calls[1]
        self.assertEqual(mode, "replan")
        self.assertIn("403 forbidden", reason)
        self.assertIn(("try api", "failed"), seen)
        status = [(t["title"], t["status"]) for t in self.led.list_tasks(out["run_id"])]
        self.assertEqual(status, [("try api", "failed"), ("summarize", "skipped"),
                                  ("scrape page", "completed"), ("summarize", "completed")])
        for e in ("replan_requested", "task_superseded"):
            self.assertIn(e, self.types(out["run_id"]))

    def test_max_replans_enforced(self):
        pl = Planner([PlanResult(tasks=[{"title": "t"}]), PlanResult(tasks=[{"title": "t2"}])])
        eng = self.engine(lambda c: StepResult(ok=False, error="nope", retryable=False), pl)
        out = eng.execute(eng.create("g", budget={"max_replans": 1})["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["usage"]["replans"], 1)
        self.assertIn("replan_refused", self.types(out["run_id"]))

    def test_replan_with_no_tasks_fails_run(self):
        pl = Planner([PlanResult(tasks=[{"title": "t"}]), PlanResult(ok=False, error="stumped")])
        eng = self.engine(lambda c: StepResult(ok=False, error="x", retryable=False), pl)
        out = eng.execute(eng.create("g")["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("replan produced no tasks", out["error"])

    def test_step_requested_replan(self):
        pl = Planner([PlanResult(tasks=[{"title": "look"}, {"title": "old next"}]),
                      PlanResult(tasks=[{"title": "new next"}])])

        def ex(c):
            if c.task["title"] == "look":
                return StepResult(output="found X", replan="docs moved")
            return StepResult(output=c.task["title"])
        eng = self.engine(ex, pl)
        out = eng.execute(eng.create("g")["run_id"])
        self.assertEqual(out["status"], "completed")
        status = [(t["title"], t["status"]) for t in self.led.list_tasks(out["run_id"])]
        self.assertEqual(status, [("look", "completed"), ("old next", "skipped"), ("new next", "completed")])

    def test_crash_during_planning_resumes_planning(self):
        pl = Planner([PlanResult(tasks=[{"title": "a"}])])
        eng = self.engine(lambda c: StepResult(output="a"), pl)
        rid = eng.create("g")["run_id"]
        self.led.transition(rid, "planning")  # simulate a process that died mid-planning
        self.assertEqual(eng.recover(), [rid])
        self.assertEqual(eng.resume(rid, background=False)["status"], "completed")


class TestVerificationInEngine(EngineCase):
    def test_verification_failure_retries_then_passes(self):
        attempts = {"n": 0}

        def ex(c):
            attempts["n"] += 1
            if attempts["n"] == 2:  # only the 2nd attempt really writes the file
                with open(os.path.join(c.workspace.dir("workspace"), "out.txt"), "w") as f:
                    f.write("done")
            return StepResult(output="I wrote out.txt")  # always CLAIMS success
        eng = RunEngine(self.led, ex, runs_root=self.runs, log=lambda m: None)
        rid = eng.create("g", [{"title": "write", "verify": [{"type": "file_exists", "path": "out.txt"}]}],
                         budget={"max_failures": 3})["run_id"]
        out = eng.execute(rid)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(attempts["n"], 2)
        steps = self.led.list_steps(rid)
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("verification failed", steps[0]["error"])
        t = self.types(rid)
        self.assertIn("verification_failed", t)
        self.assertIn("verification_passed", t)
        self.assertIn("verification_started", t)

    def test_planner_verify_lines_are_enforced(self):
        pl = Planner([PlanResult(tasks=parse_plan("PLAN: make it\nVERIFY: test -f made.txt"))])
        eng = self.engine(lambda c: StepResult(output="made it (not really)"), pl)
        out = eng.execute(eng.create("g", budget={"max_failures": 2, "max_replans": 0})["run_id"])
        self.assertEqual(out["status"], "failed")
        self.assertIn("verification failed", out["error"])

    def test_bad_verify_spec_rejected_at_create(self):
        eng = RunEngine(self.led, lambda c: StepResult(), log=lambda m: None)
        with self.assertRaises(LedgerError):
            eng.create("g", [{"title": "x", "verify": [{"type": "file_exists"}]}])


class TestChatPlanner(EngineCase):
    def test_prompt_and_parse(self):
        captured = []

        def fake_chat(messages, session_id="default", tools=None, **kw):
            captured.append(messages)
            if "PLANNING" in messages[0]["content"]:
                return {"content": "Sure.\nPLAN: one\nVERIFY: true\nPLAN: two", "_grade": "B"}
            return {"content": "ok\nREPLAN: new info" if "one" in messages[-1]["content"] else "ok",
                    "_grade": "B", "done_reason": "stop"}
        replans = []
        planner = make_chat_planner(fake_chat)

        def counting_planner(pctx):
            replans.append(pctx.mode)
            return planner(pctx)
        eng = self.engine(make_chat_executor(fake_chat), counting_planner)
        out = eng.execute(eng.create("do the thing", budget={"max_replans": 1})["run_id"])
        self.assertEqual(out["status"], "completed")
        plan_sys = captured[0][0]["content"]
        self.assertIn("Goal: do the thing", plan_sys)
        self.assertIn(os.path.join(self.runs, out["run_id"], "workspace"), plan_sys)
        self.assertEqual(replans, ["initial", "replan"])  # task "one" emitted REPLAN:
        replan_sys = captured[2][0]["content"]
        self.assertIn("This is a REPLAN", replan_sys)
        self.assertIn("Completed (do NOT repeat)", replan_sys)
        task_sys = captured[1][0]["content"]
        self.assertIn("The harness will verify this task afterwards with: true", task_sys)


class TestMidStepInterrupt(EngineCase):
    def test_pause_interrupts_inflight_step_without_counting_a_failure(self):
        import threading
        started = threading.Event()
        calls = []

        def slow_chat(messages, session_id="default", tools=None, cancel_event=None, **kw):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                cancel_event.wait(5)          # a long model turn, stopped by the scope event
                return {"content": "", "done_reason": "cancelled", "_grade": "F"}
            return {"content": "finished", "_grade": "B", "done_reason": "stop"}
        eng = RunEngine(self.led, make_chat_executor(slow_chat), runs_root=self.runs, log=lambda m: None)
        rid = eng.create("g", ["long task"], budget={"max_failures": 1})["run_id"]
        eng.start(rid)
        self.assertTrue(started.wait(5))
        eng.pause(rid)
        out = eng.wait(rid, 10)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(out["usage"]["failures"], 0)           # max_failures=1 not consumed
        self.assertEqual(self.led.list_steps(rid)[0]["status"], "interrupted")
        self.assertIn("task_interrupted", self.types(rid))
        self.assertEqual(eng.resume(rid, background=False)["status"], "completed")


if __name__ == "__main__":
    unittest.main()

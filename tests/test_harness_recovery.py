"""Phase 5: failure classification, llm_judge verification, approvals.
Standalone — no proxy.

Run: python3 tests/test_harness_recovery.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult, InvalidTransition  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.failures import classify  # noqa: E402
from mneme.harness.planning import PlanResult  # noqa: E402
from mneme.harness import verify as V  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_categories(self):
        cases = {
            "verification failed: file_exists(x)": "verification",
            "model called tools the harness cannot execute: bash": "unexecutable",
            "empty model output": "empty",
            "turn graded F (failed/fabricated): x": "fabricated",
            "model turn ended with done_reason=timeout": "provider",
            "rejected by user: no": "rejected",
            "RuntimeError: boom": "executor_error",
            "fetch failed 403 forbidden": "tool",
            "something odd": "other",
        }
        for err, cat in cases.items():
            self.assertEqual(classify(err), cat, err)
        self.assertEqual(classify("x", {"interrupted": True}), "interrupted")


class TestJudge(unittest.TestCase):
    def test_judge_runs_after_deterministic_checks(self):
        d = tempfile.mkdtemp()
        calls = []

        def judge(criteria, output):
            calls.append(criteria)
            return ("good" in output), "looked at it"
        checks = V.normalize([{"type": "llm_judge", "criteria": "is it good"}, "true"])
        self.assertTrue(V.run_checks(checks, "good work", d, judge=judge)[0])
        self.assertFalse(V.run_checks(checks, "bad work", d, judge=judge)[0])
        bad = V.normalize([{"type": "llm_judge", "criteria": "c"}, "false"])
        ok, res = V.run_checks(bad, "good", d, judge=judge)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)                    # judge NOT consulted when a command failed
        self.assertIn("skipped", res[-1]["detail"])
        self.assertFalse(V.run_checks(V.normalize([{"type": "llm_judge", "criteria": "c"}]), "x", d)[0])


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_rec_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.runs = os.path.join(self.tmp, "runs")

    def eng(self, ex, **kw):
        return RunEngine(self.led, ex, runs_root=self.runs, log=lambda m: None, **kw)

    def types(self, rid):
        return [e["type"] for e in self.led.events(rid)]


class TestEngineRecovery(Case):
    def test_failure_category_recorded(self):
        e = self.eng(lambda c: StepResult(ok=False, error="empty model output", retryable=False))
        rid = e.create("g")["run_id"]
        e.execute(rid)
        ev = [x for x in self.led.events(rid) if x["type"] == "task_failed"][0]
        self.assertEqual(ev["data"]["category"], "empty")
        self.assertEqual(self.led.list_steps(rid)[0]["meta"]["failure_category"], "empty")

    def test_engine_judge(self):
        e = self.eng(lambda c: StepResult(output="summary: short"),
                     judge=lambda crit, out: ("short" in out, "ok"))
        rid = e.create("g", [{"title": "t", "verify": [{"type": "llm_judge", "criteria": "is short"}]}])["run_id"]
        self.assertEqual(e.execute(rid)["status"], "completed")
        self.assertIn("verification_passed", self.types(rid))

    def test_invalid_grant_rejected(self):
        e = self.eng(lambda c: StepResult())
        with self.assertRaises(LedgerError):
            e.create("g", permissions={"grant": ["root"]})
        rid = e.create("g", permissions={"grant": ["network", "read-only"]})["run_id"]
        self.assertEqual(self.led.get_run(rid)["permissions"]["grant"], ["network", "read-only"])


class TestApprovals(Case):
    def test_approve_flow(self):
        seen = []
        e = self.eng(lambda c: (seen.append(c.task["title"]), StepResult(output="x"))[1])
        rid = e.create("g", ["safe", {"title": "deploy", "requires_approval": True}])["run_id"]
        out = e.execute(rid)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertEqual(seen, ["safe"])
        self.assertIn('"deploy"', out["approval_state"])
        self.assertEqual(out["owner"], "")                   # not holding the run while waiting
        self.assertEqual(e.recover(), [])                    # waiting is not an orphan
        with self.assertRaises(InvalidTransition):
            e.approve(e.create("other")["run_id"])           # nothing pending there
        out = e.approve(rid, actor="sean", background=False)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(seen, ["safe", "deploy"])
        for t in ("approval_requested", "approval_granted"):
            self.assertIn(t, self.types(rid))

    def test_reject_replans(self):
        pl_calls = []

        def planner(p):
            pl_calls.append((p.mode, p.reason))
            if p.mode == "initial":
                return PlanResult(tasks=[{"title": "rm -rf build", "requires_approval": True}])
            return PlanResult(tasks=[{"title": "clean build safely"}])
        e = self.eng(lambda c: StepResult(output=c.task["title"]), planner=planner)
        rid = e.create("clean up")["run_id"]
        self.assertEqual(e.execute(rid)["status"], "awaiting_approval")
        out = e.reject(rid, reason="too destructive", background=False)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result"], "clean build safely")
        self.assertIn("too destructive", pl_calls[1][1])
        self.assertIn("approval_rejected", self.types(rid))

    def test_reject_without_planner_fails(self):
        e = self.eng(lambda c: StepResult(output="x"))
        rid = e.create("g", [{"title": "t", "requires_approval": True}])["run_id"]
        e.execute(rid)
        out = e.reject(rid, reason="no", background=False)
        self.assertEqual(out["status"], "failed")
        self.assertIn("rejected by user: no", out["error"])

    def test_approve_each_task_via_run_meta(self):
        e = self.eng(lambda c: StepResult(output="x"))
        rid = e.create("g", ["a", "b"], meta={"approve_each_task": True})["run_id"]
        self.assertEqual(e.execute(rid)["status"], "awaiting_approval")
        self.assertEqual(e.approve(rid, background=False)["status"], "awaiting_approval")  # b needs one too
        self.assertEqual(e.approve(rid, background=False)["status"], "completed")


if __name__ == "__main__":
    unittest.main()

"""Phase 8: harness control commands and metrics. Standalone — no proxy.

Run: python3 tests/test_harness_commands.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.commands import handle  # noqa: E402
from mneme.harness.metrics import compute  # noqa: E402
from mneme.harness.planning import PlanResult  # noqa: E402
from mneme.harness.skills import SkillRegistry  # noqa: E402
from mneme.harness.profiles import ProfileStore  # noqa: E402
from mneme.harness.evolution import Evolution  # noqa: E402


class TestCommands(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="mneme_cmd_")
        self.led = Ledger(os.path.join(tmp, "harness.db"))
        self.skills = SkillRegistry(self.led)
        self.skills.upsert("web-research", "research on the web")
        self.plans = []

        def planner(p):
            self.plans.append(p.mode)
            return PlanResult(tasks=[{"title": "only task"}])
        self.eng = RunEngine(self.led, lambda c: StepResult(output="done it",
                             tool_calls=[{"tool": "bash", "status": "success"}]),
                             planner=planner, skills=self.skills, profiles=ProfileStore(self.led),
                             evolution=Evolution(self.led), runs_root=os.path.join(tmp, "runs"), log=lambda m: None,
                             freeform_turn=lambda *a, **k: {"content": "done it\nDONE", "tool_trace": []})
        self.h = lambda t: handle(t, self.eng)

    def test_not_commands(self):
        self.assertIsNone(self.h("hello"))
        self.assertIsNone(self.h("/usr/bin is a path"))       # unknown word -> goes to the model
        self.assertIsNone(handle("/help", None))
        self.assertIn("/approve", self.h("/help"))

    def test_run_lifecycle_via_commands(self):
        out = self.h("/run write a haiku")
        self.assertIn("started run_", out)
        rid = self.led.list_runs(limit=1)[0]["run_id"]
        self.eng.wait(rid, 10)
        self.assertIn("[completed]", self.h("/status"))            # "last" by default
        self.assertIn("done it", self.h(f"/status {rid[-6:]}"))    # suffix reference
        self.assertIn("only task", self.h(f"/plan {rid}"))
        self.assertIn("[completed", self.h(f"/tasks {rid}"))
        self.assertIn("run_completed", self.h(f"/log {rid} 5"))
        self.assertIn(rid, self.h("/runs"))
        self.assertEqual(self.h("/runs failed"), "no runs")
        self.assertIn("no such run", self.h("/status zzzz"))
        self.assertIn("nothing to pause", self.h(f"/pause {rid}"))

    def test_run_defaults_to_structured(self):
        out = self.h("/run write a haiku")
        self.assertIn("structured plan", out)
        rid = self.led.list_runs(limit=1)[0]["run_id"]
        self.eng.wait(rid, 10)
        self.assertFalse((self.led.get_run(rid).get("meta") or {}).get("free_form"))
        self.assertIn("[completed]", self.h(f"/status {rid}"))

    def test_run_free_form_opt_in(self):
        out = self.h("/run --free write a haiku")
        self.assertIn("free-form session", out)
        rid = self.led.list_runs(limit=1)[0]["run_id"]
        self.eng.wait(rid, 10)
        self.assertTrue((self.led.get_run(rid).get("meta") or {}).get("free_form"))
        self.assertIn("[completed]", self.h(f"/status {rid}"))

    def test_approval_and_replan_commands(self):
        rid = self.eng.create("g", [{"title": "risky", "requires_approval": True}])["run_id"]
        self.eng.execute(rid)
        self.assertIn("WAITING FOR APPROVAL", self.h(f"/status {rid}"))
        self.h(f"/approve {rid} looks fine")
        self.assertEqual(self.eng.wait(rid, 10)["status"], "completed")
        rid2 = self.eng.create("g2", ["a"])["run_id"]
        self.eng.pause(rid2)
        self.assertIn("replan requested", self.h(f"/replan {rid2} scope changed"))
        self.assertEqual(self.eng.wait(rid2, 10)["status"], "completed")
        self.assertIn("replan", self.plans)

    def test_catalog_commands(self):
        self.assertIn("web-research", self.h("/skills"))
        self.assertIn("bash [shell", self.h("/tools"))
        self.assertIn("researcher", self.h("/profiles"))
        self.assertEqual(self.h("/evolution"), "no proposals")
        self.assertIn("not available", self.h("/strategies"))
        self.assertEqual(handle("/search x", self.eng, extras={"search": lambda q: f"found {q}"}), "found x")
        self.assertEqual(self.h("/jobs"), "jobs not configured")

    def test_metrics(self):
        ok = self.eng.create("g", ["a"])["run_id"]
        self.eng.execute(ok)
        self.eng.executor = lambda c: StepResult(ok=False, error="empty model output", retryable=False)
        bad = self.eng.create("g", ["a"], budget={"max_replans": 0})["run_id"]
        self.eng.execute(bad)
        m = compute(self.led, self.skills, self.eng.evolution)
        self.assertEqual(m["runs"]["completed"], 1)
        self.assertEqual(m["runs"]["failed"], 1)
        self.assertEqual(m["task_success_rate"], 0.5)
        self.assertEqual(m["failure_categories"], {"empty": 1})
        self.assertEqual(m["tools"]["bash"]["success_rate"], 1.0)
        self.assertEqual(m["self_improvement"]["proposals"], 1)   # the failed run's observation
        self.assertIn("\"task_success_rate\"", self.h("/metrics"))


if __name__ == "__main__":
    unittest.main()

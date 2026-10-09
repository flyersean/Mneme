"""Free-form goal session — the model drives toward the goal across turns, the
harness records turns + enforces a turn budget, and the judge is the final check.
No task graph, no deterministic verification."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness.ledger import Ledger
from mneme.harness.engine import RunEngine


def _wait(led, run_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = led.get_run(run_id)
        if r["status"] in ("completed", "failed", "cancelled"):
            return r
        time.sleep(0.05)
    return led.get_run(run_id)


class FreeFormCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_ff_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.runs = os.path.join(self.tmp, "runs")

    def eng(self, turn, judge=None, **kw):
        return RunEngine(self.led, lambda ctx: None, freeform_turn=turn, judge=judge,
                         runs_root=self.runs, log=lambda m: None, **kw)

    def test_freeform_completes_on_done(self):
        def turn(engine, run, transcript, turn_num, remaining, note="", cancel_event=None):
            return {"content": "Fixed and verified.\nDONE" if turn_num == 2 else "Working...",
                    "tool_trace": [{"tool": "bash", "args": {"command": "x"}, "result": "ok",
                                    "status": "success", "elapsed_ms": 1}]}
        e = self.eng(turn, judge=lambda c, o, evidence="", failed="": (True, "B: aligned"))
        run = e.create("Build a thing", free_form=True, start=True)
        r = _wait(self.led, run["run_id"])
        self.assertEqual(r["status"], "completed")
        steps = [s for s in self.led.list_steps(run["run_id"]) if s["kind"] == "freeform"]
        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[-1]["meta"]["judge_ok"])

    def test_judge_rejects_then_accepts(self):
        seen = {"n": 0}

        def turn(engine, run, transcript, turn_num, remaining, note="", cancel_event=None):
            had_feedback = any(t.get("judge_feedback") for t in transcript)
            return {"content": "Fixed the misalignment.\nDONE" if had_feedback else "Built it.\nDONE",
                    "tool_trace": []}

        def judge(criteria, output, evidence="", failed=""):
            seen["n"] += 1
            return (False, "B: runs but misses the goal") if seen["n"] == 1 else (True, "B: aligned")
        e = self.eng(turn, judge=judge)
        run = e.create("Goal", free_form=True, start=True)
        r = _wait(self.led, run["run_id"])
        self.assertEqual(r["status"], "completed")
        self.assertEqual(seen["n"], 2)

    def test_max_turns_fails(self):
        e = self.eng(lambda *a, **k: {"content": "still working...", "tool_trace": []},
                     judge=lambda c, o, evidence="", failed="": (True, ""))
        run = e.create("Goal", free_form=True, budget={"max_turns": 3}, start=True)
        r = _wait(self.led, run["run_id"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("max_turns", r.get("error") or "")


if __name__ == "__main__":
    unittest.main()

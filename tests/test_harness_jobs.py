"""Phase 9: jobs + scheduler (no double-fire across processes, no self-overlap).
Standalone — no proxy.

Run: python3 tests/test_harness_jobs.py
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.jobs import JobStore, Scheduler  # noqa: E402


class TestJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_jobs_")
        self.path = os.path.join(self.tmp, "harness.db")
        self.led = Ledger(self.path)
        self.jobs = JobStore(self.led)
        self.gate = threading.Event()
        self.eng = RunEngine(self.led, lambda c: (self.gate.wait(5), StepResult(output="tick"))[1],
                             log=lambda m: None)
        self.sched = Scheduler(self.eng, self.jobs)

    def test_validation(self):
        for kw in ({"goal": ""}, {"interval_s": 1}, {"interval_s": "x"}, {"tasks": [{"nope": 1}]}):
            args = {"name": "n", "goal": "g", "interval_s": 60, **kw}
            with self.assertRaises(LedgerError):
                self.jobs.create(args.pop("name"), args.pop("goal"), args.pop("interval_s"), **args)

    def test_due_job_starts_a_run_and_reschedules(self):
        j = self.jobs.create("check", "check the feed", 60)
        started = self.sched.tick()
        self.assertEqual(len(started), 1)
        run = self.led.get_run(started[0])
        self.assertEqual((run["meta"]["job_id"], run["created_by"]), (j["job_id"], f"job:{j['job_id']}"))
        j2 = self.jobs.get(j["job_id"])
        self.assertEqual((j2["runs_count"], j2["last_run_id"]), (1, started[0]))
        self.assertGreater(j2["next_run_at"], time.time() + 50)
        self.assertEqual(self.sched.tick(), [])                     # not due again yet
        self.gate.set()
        self.eng.wait(started[0], 5)

    def test_no_overlap_then_overlap(self):
        j = self.jobs.create("slow", "slow thing", 60)
        first = self.sched.tick()[0]
        self.jobs.trigger(j["job_id"])
        self.assertEqual(self.sched.tick(), [])                     # previous run still active -> skipped
        self.assertEqual(self.jobs.get(j["job_id"])["skipped"], 1)
        self.assertIn("skipped", [x["action"] for x in self.jobs.log(j["job_id"])])
        self.gate.set()
        self.eng.wait(first, 5)
        self.jobs.trigger(j["job_id"])
        self.assertEqual(len(self.sched.tick()), 1)

    def test_disabled_and_future_jobs_do_not_fire(self):
        a = self.jobs.create("a", "a", 60)
        self.jobs.set_enabled(a["job_id"], False)
        self.jobs.create("b", "b", 60, start_in_s=3600)
        self.assertEqual(self.sched.tick(), [])

    def test_two_processes_never_double_fire(self):
        other = JobStore(Ledger(self.path))                         # a second connection = second proxy
        self.jobs.create("x", "x", 60)
        now = time.time()
        a, b = self.jobs.claim_due(now), other.claim_due(now)
        self.assertEqual(len(a) + len(b), 1)


if __name__ == "__main__":
    unittest.main()

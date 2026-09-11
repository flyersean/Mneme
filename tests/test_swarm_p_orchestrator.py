"""Tests for the parallel swarm orchestrator's `parallel:` fan-out."""

import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extensions", "swarm"))
from swarm_p_orchestrator import ParallelOrchestrator  # noqa: E402


class TestParallel(unittest.TestCase):
    def setUp(self):
        # Skip __init__ (no config needed to exercise the fan-out directly).
        self.o = ParallelOrchestrator.__new__(ParallelOrchestrator)
        self.tmp = tempfile.mkdtemp()

    def test_parallel_block_runs_all_sub_steps_concurrently(self):
        block = [
            {"name": "a", "exec": f"sleep 0.4; echo A > {os.path.join(self.tmp, 'pa.txt')}"},
            {"name": "b", "exec": f"sleep 0.4; echo B > {os.path.join(self.tmp, 'pb.txt')}"},
            {"name": "c", "exec": f"sleep 0.4; echo C > {os.path.join(self.tmp, 'pc.txt')}"},
        ]
        t0 = time.time()
        self.o._run_parallel(block)
        elapsed = time.time() - t0
        for fname, expect in (("pa.txt", "A"), ("pb.txt", "B"), ("pc.txt", "C")):
            with open(os.path.join(self.tmp, fname)) as f:
                self.assertEqual(f.read().strip(), expect)
        # 3 × 0.4s serial ≈ 1.2s; parallel should finish well under that.
        self.assertLess(elapsed, 0.9)

    def test_exec_sub_step_runs_a_single_leaf_step(self):
        out = os.path.join(self.tmp, "out.txt")
        self.o._exec_sub_step({"name": "w", "exec": f"echo hi > {out}"})
        with open(out) as f:
            self.assertEqual(f.read().strip(), "hi")


if __name__ == "__main__":
    unittest.main()

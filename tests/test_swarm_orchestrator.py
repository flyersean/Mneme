"""Tests for the swarm orchestrator's folder-IO primitives (swap_dir / clear_dir).

These exercise the atomic inbox-swap semantics without any model or proxy:
the swap must freeze the current inbox into <dir>.active, recreate a fresh
<dir>, leave late-arriving writes in <dir> untouched by a later clear, and
clear a stale snapshot on the next swap.

Run:
    /home/sean/mneme/venv/bin/python tests/test_swarm_orchestrator.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extensions", "swarm"))
from swarm_orchestrator import Orchestrator  # noqa: E402


class TestFolderIO(unittest.TestCase):
    def setUp(self):
        # Skip __init__ (no config file needed to exercise these pure-IO methods).
        self.o = Orchestrator.__new__(Orchestrator)
        self.tmp = tempfile.mkdtemp()
        self.raw = os.path.join(self.tmp, "raw")

    def _write(self, name, content):
        p = os.path.join(self.raw, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)

    def test_swap_freezes_inbox_and_recreates(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self.assertEqual(sorted(os.listdir(self.raw + ".active")), ["a.txt"])
        self.assertEqual(os.listdir(self.raw), [])

    def test_late_write_survives_clear(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self._write("b.txt", "B")                       # arrives during the tick
        self.o.clear_dir(self.raw + ".active")          # consume the snapshot
        self.assertEqual(os.listdir(self.raw + ".active"), [])   # snapshot drained
        self.assertEqual(sorted(os.listdir(self.raw)), ["b.txt"])  # late write kept

    def test_next_swap_clears_stale_snapshot(self):
        self._write("a.txt", "A")
        self.o.swap_dir(self.raw)
        self._write("b.txt", "B")
        self.o.clear_dir(self.raw + ".active")
        self.o.swap_dir(self.raw)                       # next tick
        self.assertEqual(sorted(os.listdir(self.raw + ".active")), ["b.txt"])
        self.assertEqual(os.listdir(self.raw), [])

    def test_swap_missing_dir(self):
        self.o.swap_dir(self.raw)                       # raw does not exist yet
        self.assertTrue(os.path.isdir(self.raw))
        self.assertTrue(os.path.isdir(self.raw + ".active"))

    def test_clear_missing_dir_is_noop(self):
        self.o.clear_dir(os.path.join(self.tmp, "nope"))  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)

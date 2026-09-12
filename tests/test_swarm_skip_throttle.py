"""skip_if_empty and every (cycle throttle) — full run() with a stubbed model."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extensions", "swarm"))
import yaml  # noqa: E402
from swarm_orchestrator import Orchestrator  # noqa: E402


class TestSkipAndThrottle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []  # step names, in model-call order

    def _run(self, steps, **top):
        cfg = dict(steps=steps)
        cfg.update(top)
        path = os.path.join(self.tmp, "swarm.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f)
        o = Orchestrator(path)

        def fake_call(step, context, retries=0):
            self.calls.append(step.get("name"))
            return "MODEL-OUTPUT"

        o.call_mneme = fake_call
        o.call_ollama = fake_call
        o.run()
        return o

    def test_skip_if_empty_skips_model_call_when_dir_empty(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        out = os.path.join(self.tmp, "out")
        self._run([{"name": "read", "read_dir": empty, "skip_if_empty": True,
                    "write_dir": out, "port": 9999}])
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(out, "output.txt")))

    def test_skip_if_empty_calls_when_dir_nonempty(self):
        inbox = os.path.join(self.tmp, "inbox")
        os.makedirs(inbox)
        with open(os.path.join(inbox, "x.txt"), "w") as f:
            f.write("hello")
        out = os.path.join(self.tmp, "out")
        self._run([{"name": "read", "read_dir": inbox, "skip_if_empty": True,
                    "write_dir": out, "port": 9999}])
        self.assertEqual(self.calls, ["read"])

    def test_every_runs_only_every_nth_visit(self):
        # 2-step loop capped at 6 executions: b (every:3) runs only on its 3rd
        # visit; the other visits skip but still loop back via b's goto.
        self._run(
            [
                {"name": "a", "goto": "b"},
                {"name": "b", "every": 3, "write_dir": os.path.join(self.tmp, "out"),
                 "goto": "a", "port": 9999},
            ],
            max_steps=6,
        )
        self.assertEqual(self.calls, ["b"])

    def test_every_zero_runs_every_visit(self):
        self._run(
            [
                {"name": "a", "goto": "b"},
                {"name": "b", "every": 0, "write_dir": os.path.join(self.tmp, "out"),
                 "goto": "a", "port": 9999},
            ],
            max_steps=4,
        )
        self.assertEqual(self.calls, ["b", "b"])


if __name__ == "__main__":
    unittest.main()

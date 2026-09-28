"""Phase 7: agent profiles (versioned, merged under explicit run settings) and
automatic artifact capture. Standalone — no proxy.

Run: python3 tests/test_harness_profiles.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.profiles import ProfileStore, BUILTIN_PROFILES  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_prof_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.prof = ProfileStore(self.led)
        self.runs = os.path.join(self.tmp, "runs")

    def eng(self, ex=None):
        return RunEngine(self.led, ex or (lambda c: StepResult(output="x")), profiles=self.prof,
                         runs_root=self.runs, log=lambda m: None)


class TestProfiles(Case):
    def test_builtins_seeded_once(self):
        self.assertEqual({p["name"] for p in self.prof.list()}, set(BUILTIN_PROFILES))
        ProfileStore(self.led)  # re-open: no duplicate versions
        self.assertEqual(len(self.prof.history("coder")), 1)

    def test_versioning_and_validation(self):
        p = self.prof.upsert("mine", {"description": "d", "grant": ["read-only"], "budget": {"max_steps": 5}})
        self.assertEqual(p["version"], 1)
        self.assertEqual(self.prof.upsert("mine", {"description": "d", "grant": ["read-only"],
                                                   "budget": {"max_steps": 5}})["version"], 1)
        self.assertEqual(self.prof.upsert("mine", {"description": "d2"})["version"], 2)
        self.assertEqual(len(self.prof.history("mine")), 2)
        for bad in ({"nope": 1}, {"grant": ["root"]}, {"budget": {"max_x": 1}}, {"skills": "a"}):
            with self.assertRaises(LedgerError):
                self.prof.upsert("bad", bad)

    def test_profile_merged_under_explicit_settings(self):
        e = self.eng()
        self.prof.upsert("p", {"description": "d", "grant": ["read-only"], "budget": {"max_steps": 7, "max_failures": 1},
                               "skills": ["web-research"], "plan": False})
        run = e.create("g", profile="p", budget={"max_failures": 9})
        self.assertEqual(run["profile"], "p")
        self.assertEqual((run["budget"]["max_steps"], run["budget"]["max_failures"]), (7, 9))  # explicit wins
        self.assertEqual(run["permissions"]["grant"], ["read-only"])
        self.assertEqual(run["meta"]["skills"], ["web-research"])
        self.assertEqual(run["meta"]["profile_version"], 1)
        run2 = e.create("g", profile="p", permissions={"grant": ["network"]})
        self.assertEqual(run2["permissions"]["grant"], ["network"])
        with self.assertRaises(LedgerError):
            e.create("g", profile="missing")

    def test_cautious_profile_requires_approval(self):
        e = self.eng()
        rid = e.create("g", ["a"], profile="cautious")["run_id"]
        self.assertEqual(e.execute(rid)["status"], "awaiting_approval")


class TestArtifactCapture(Case):
    def test_files_in_artifacts_dir_registered_once(self):
        def step(ctx):
            with open(ctx.workspace.resolve("artifacts", "report.md"), "w") as f:
                f.write("# report")
            p = ctx.workspace.resolve("artifacts", "manual.txt")
            with open(p, "w") as f:
                f.write("m")
            ctx.add_artifact(p, description="registered by the step")
            return StepResult(output="done")
        e = self.eng(step)
        rid = e.create("g")["run_id"]
        e.execute(rid)
        arts = {os.path.basename(a["path"]): a for a in self.led.list_artifacts(rid)}
        self.assertEqual(set(arts), {"report.md", "manual.txt"})
        self.assertEqual(arts["report.md"]["provenance"]["captured"], "auto")
        self.assertEqual(arts["manual.txt"]["description"], "registered by the step")


if __name__ == "__main__":
    unittest.main()

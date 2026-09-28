"""Phase 6: controlled self-improvement — levels, versioned apply, post-apply
verification + rollback, L4 code on a git branch only, run observation and
reflection hooks. Standalone — no proxy.

Run: python3 tests/test_harness_evolution.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.skills import SkillRegistry  # noqa: E402
from mneme.harness import evolution as E  # noqa: E402
from mneme.harness.chat_executor import make_chat_reflector  # noqa: E402
from mneme.harness.planning import parse_reflection  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_evo_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.skills = SkillRegistry(self.led)
        self.notes = []
        self.store = {"greeting": "hello v1"}
        self.evo = E.Evolution(self.led, appliers={
            "knowledge": E.KnowledgeApplier(sink=lambda t, c: self.notes.append((t, c))),
            "skill": E.SkillApplier(self.skills),
            "instruction": E.CallableApplier(read=self.store.get,
                                             write=lambda n, c: self.store.__setitem__(n, c)),
        })


class TestLevels(Case):
    def test_knowledge_auto_applied_to_sink(self):
        p = self.evo.propose("knowledge", "fact:1", "API v2 moved to /v3", created_by="run:r1")
        self.assertEqual((p["level"], p["status"]), (1, "applied"))
        self.assertEqual(self.notes, [("fact:1", "API v2 moved to /v3")])

    def test_skill_auto_applied_versioned_and_rolled_back(self):
        self.skills.upsert("s", "desc", "v1 body")
        p = self.evo.propose("skill", "s", json.dumps({"description": "desc", "body": "v2 body"}))
        self.assertEqual(p["status"], "applied")
        self.assertEqual(self.skills.get("s")["body"], "v2 body")
        self.assertIn("v1 body", p["previous"])
        self.evo.rollback(p["proposal_id"])
        self.assertEqual(self.skills.get("s")["body"], "v1 body")
        self.assertEqual(self.skills.get("s")["version"], 3)          # history kept: v1, v2, restore
        new = self.evo.propose("skill", "brand-new", json.dumps({"description": "d", "body": "b"}))
        self.evo.rollback(new["proposal_id"])
        self.assertEqual(self.skills.get("brand-new")["active"], 0)   # didn't exist -> deactivated

    def test_instruction_needs_approval_and_tests(self):
        p = self.evo.propose("instruction", "greeting", "hello v2", tests=[{"type": "output_contains", "text": "v2"}])
        self.assertEqual((p["level"], p["status"]), (3, "proposed"))
        self.assertEqual(self.store["greeting"], "hello v1")         # nothing applied yet
        with self.assertRaises(LedgerError):
            self.evo.approve(p["proposal_id"], actor="auto")
        p = self.evo.approve(p["proposal_id"], actor="sean")
        self.assertEqual(p["status"], "applied")
        self.assertEqual((self.store["greeting"], p["previous"], p["decided_by"]), ("hello v2", "hello v1", "sean"))
        self.assertEqual([h["action"] for h in self.evo.history(p["proposal_id"])], ["proposed", "tested", "applied"])
        self.assertEqual(self.evo.changes_to("instruction", "greeting")[0]["proposal_id"], p["proposal_id"])

    def test_failing_pre_test_blocks_apply(self):
        p = self.evo.propose("instruction", "greeting", "hello v2", tests=["false"])
        p = self.evo.approve(p["proposal_id"])
        self.assertEqual(p["status"], "test_failed")
        self.assertEqual(self.store["greeting"], "hello v1")

    def test_post_apply_failure_rolls_back(self):
        marker = os.path.join(self.tmp, "ok")
        # tests pass before apply (marker exists), the apply removes the marker -> post-apply fails
        open(marker, "w").close()
        self.evo.appliers["instruction"] = E.CallableApplier(
            read=self.store.get, write=lambda n, c: (self.store.__setitem__(n, c),
                                                     os.path.exists(marker) and c == "bad" and os.remove(marker)))
        p = self.evo.propose("instruction", "greeting", "bad", tests=[f"test -f {marker}"])
        p = self.evo.approve(p["proposal_id"])
        self.assertEqual(p["status"], "rolled_back")
        self.assertEqual(self.store["greeting"], "hello v1")          # restored
        self.assertIn("post_apply_verification_failed", [h["action"] for h in self.evo.history(p["proposal_id"])])

    def test_level_can_be_raised_not_lowered_and_bad_input(self):
        self.assertEqual(self.evo.propose("knowledge", "t", "x", level=3)["status"], "proposed")
        self.assertEqual(self.evo.propose("instruction", "greeting", "x", level=1)["level"], 3)
        for bad in (("nope", "t", "c"), ("profile", "t", "c"), ("knowledge", "", "c")):
            with self.assertRaises(LedgerError):
                self.evo.propose(*bad)

    def test_log_append_only(self):
        self.evo.propose("knowledge", "t", "x")
        raw = sqlite3.connect(self.led.path)
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM evolution_log")


class TestCodeLevel(Case):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        g("init", "-q")
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("VALUE = 1\n")
        g("add", "-A")
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.evo.appliers["code"] = E.CodeApplier(self.repo, os.path.join(self.tmp, "wt"))
        self.diff = ("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n")

    def test_code_goes_to_branch_only(self):
        p = self.evo.propose("code", "app.py", self.diff, tests=["grep -q 'VALUE = 2' app.py"])
        self.assertEqual((p["level"], p["status"]), (4, "proposed"))
        p = self.evo.approve(p["proposal_id"], actor="sean")
        self.assertEqual(p["status"], "branch_ready")
        with open(os.path.join(self.repo, "app.py")) as f:
            self.assertEqual(f.read(), "VALUE = 1\n")                # main tree untouched
        branches = subprocess.run(["git", "branch"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertIn(f"evolve/{p['proposal_id']}", branches)
        self.evo.reject(p["proposal_id"], reason="not now")
        branches = subprocess.run(["git", "branch"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn("evolve/", branches)

    def test_failing_tests_and_bad_patch(self):
        p = self.evo.approve(self.evo.propose("code", "app.py", self.diff, tests=["false"])["proposal_id"])
        self.assertEqual(p["status"], "test_failed")
        p = self.evo.test(self.evo.propose("code", "app.py", "--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-x\n+y\n",
                                           tests=["true"])["proposal_id"])
        self.assertEqual(p["status"], "test_failed")
        self.assertIn("does not apply", p["result"]["error"])
        p = self.evo.approve(self.evo.propose("code", "app.py", self.diff)["proposal_id"])
        self.assertEqual(p["status"], "test_failed")                  # code without tests is refused


class TestRunHooks(Case):
    def test_failed_run_becomes_knowledge(self):
        eng = RunEngine(self.led, lambda c: StepResult(ok=False, error="fetch failed 403", retryable=False),
                        evolution=self.evo, log=lambda m: None)
        rid = eng.create("get prices")["run_id"]
        eng.execute(rid)
        props = self.evo.list(kind="knowledge")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["evidence"], [rid])
        self.assertIn("[tool]", props[0]["content"])
        self.assertEqual(len(self.notes), 1)
        ok = eng.create("easy")["run_id"]
        eng.executor = lambda c: StepResult(output="x")
        eng.execute(ok)
        self.assertEqual(len(self.evo.list(kind="knowledge")), 1)     # clean successes add nothing

    def test_reflection(self):
        self.assertEqual(parse_reflection("LESSON: use the JSON API\nSKILL: Price Lookup :: find prices :: 1. api"),
                         (["use the JSON API"], [{"name": "price-lookup", "description": "find prices",
                                                   "body": "1. api"}]))
        chat = lambda m, **kw: {"content": "LESSON: prefer the JSON endpoint\n"
                                           "SKILL: price-lookup :: look up prices :: call the JSON API first"}
        eng = RunEngine(self.led, lambda c: StepResult(ok=False, error="blocked", retryable=False),
                        evolution=self.evo, skills=self.skills, log=lambda m: None)
        eng.on_finish.append(make_chat_reflector(chat))
        rid = eng.create("prices")["run_id"]
        eng.execute(rid)
        self.assertEqual(self.skills.get("price-lookup")["body"], "call the JSON API first")
        self.assertIn("reflected", [e["type"] for e in self.led.events(rid)])
        self.assertEqual(len(self.evo.list(kind="knowledge")), 2)     # observation + lesson


if __name__ == "__main__":
    unittest.main()

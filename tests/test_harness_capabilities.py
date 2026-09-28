"""Phases 3-4: strategy version history, skills registry, tool metadata/permissions,
capability-context selection, and skill outcome feedback. Standalone — no proxy.

Run: python3 tests/test_harness_capabilities.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

import mneme.strategy_history as SH  # noqa: E402
from mneme.harness import Ledger, RunEngine, StepResult  # noqa: E402
from mneme.harness.ledger import LedgerError  # noqa: E402
from mneme.harness.skills import SkillRegistry, parse_skill_md  # noqa: E402
from mneme.harness import capabilities as C  # noqa: E402
from mneme.harness.context import CapabilityContext  # noqa: E402
from mneme.harness.chat_executor import make_chat_executor  # noqa: E402


class TestStrategyHistory(unittest.TestCase):
    def test_versions_survive_insert_or_replace(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE strategies (strategy_id TEXT PRIMARY KEY, problem_type TEXT, strategy_text TEXT, "
                   "source_chunk TEXT, grade TEXT, created_at TEXT, version INTEGER, parent_id TEXT, "
                   "effective_grade REAL, use_count INTEGER, success_count INTEGER, retired INTEGER, "
                   "superseded_by TEXT, cost INTEGER, outcome TEXT)")
        SH.ensure_schema(db)
        SH.ensure_schema(db)  # idempotent

        def save(text, v):
            SH.snapshot(db, "s1", "superseded", actor="model")
            db.execute("INSERT OR REPLACE INTO strategies (strategy_id, problem_type, strategy_text, source_chunk, "
                       "grade, created_at, version, parent_id, effective_grade, use_count, success_count, retired, "
                       "superseded_by, cost, outcome) VALUES ('s1','code',?,'','A','t',?,'',0,0,0,0,'',0,'SUCCESS')",
                       (text, v))
            SH.set_provenance(db, "s1", created_by="run:r1")
            SH.snapshot(db, "s1", "saved", actor="run:r1", reason="learned")
        save("v1 text", 1)
        save("v2 text", 2)
        h = SH.history(db, "s1")
        self.assertEqual([(r["event"], r["strategy_text"]) for r in h],
                         [("saved", "v1 text"), ("superseded", "v1 text"), ("saved", "v2 text")])
        self.assertEqual(db.execute("SELECT created_by FROM strategies").fetchone()[0], "run:r1")


class RegCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_caps_")
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.reg = SkillRegistry(self.led)


class TestSkills(RegCase):
    def test_versioning_noop_and_restore(self):
        a = self.reg.upsert("python-debugging", "debug failing python tests", "1. run pytest", tags=["pytest"])
        self.assertEqual(a["version"], 1)
        self.assertEqual(self.reg.upsert("python-debugging", "debug failing python tests", "1. run pytest",
                                         tags=["pytest"])["version"], 1)  # unchanged -> no new version
        b = self.reg.upsert("python-debugging", "debug failing python tests", "1. run pytest -x", actor="run:r9",
                            reason="found -x faster", tags=["pytest"])
        self.assertEqual(b["version"], 2)
        h = self.reg.history("python-debugging")
        self.assertEqual([(x["version"], x["actor"]) for x in h], [(1, "user"), (2, "run:r9")])
        c = self.reg.restore_version("python-debugging", 1)
        self.assertEqual((c["version"], c["body"]), (3, "1. run pytest"))  # restore = new version, history kept
        with self.assertRaises(LedgerError):
            self.reg.upsert("Bad Name!", "x")
        with self.assertRaises(LedgerError):
            self.reg.upsert("ok", "")

    def test_load_shipped_skills_dir(self):
        names = self.reg.load_dir(os.path.join(_ROOT, "skills"))
        self.assertIn("swarm-creation", names)
        sk = self.reg.get("swarm-creation")
        self.assertTrue(sk["source"].startswith("file:"))
        self.assertIn("parallel", sk["body"])
        self.assertEqual(self.reg.load_dir(os.path.join(_ROOT, "skills")), names)
        self.assertEqual(self.reg.get("swarm-creation")["version"], 1)  # reload of unchanged file

    def test_parse_frontmatter(self):
        m = parse_skill_md("---\nname: x\ndescription: does x\ntools: [bash]\n---\nbody here")
        self.assertEqual((m["name"], m["tools"], m["body"]), ("x", ["bash"], "body here"))

    def test_select_and_requires(self):
        self.reg.upsert("git-ops", "commit branch and merge with git", tags=["git"])
        self.reg.upsert("repo-refactor", "refactor a repository safely across many files",
                        requires=["git-ops"], tags=["refactor"])
        self.reg.upsert("web-research", "research a question on the web and cite pages", tags=["search"])
        picked = [s["name"] for s in self.reg.select("refactor the repository module", k=1)]
        self.assertEqual(picked, ["repo-refactor", "git-ops"])  # composition expanded
        self.assertEqual(self.reg.select("bake a cake"), [])
        self.reg.set_active("web-research", False)
        self.assertEqual(self.reg.select("research the web"), [])

    def test_record_outcome(self):
        self.reg.upsert("s", "desc")
        self.reg.record_outcome(["s"], True)
        self.reg.record_outcome(["s"], False)
        s = self.reg.get("s")
        self.assertEqual((s["uses"], s["successes"], s["failures"]), (2, 1, 1))


class TestToolCapabilities(unittest.TestCase):
    def test_meta_and_grants(self):
        self.assertEqual(C.tool_meta("bash")["permission"], "shell")
        self.assertEqual(C.tool_meta("some_mcp_tool")["permission"], "system")  # conservative
        g = C.normalize_grant(["read-only", "network"])
        self.assertTrue(C.allowed("web_search", g))
        self.assertFalse(C.allowed("bash", g))
        self.assertEqual(C.normalize_grant(["*"]), set(C.PERMISSION_LEVELS))
        with self.assertRaises(ValueError):
            C.normalize_grant(["root"])

    def test_select_tools(self):
        names = list(C.BUILTIN_TOOL_META)
        top = [m["name"] for m in C.select_tools("search the web for the latest release", names, k=2)]
        self.assertIn("web_search", top)
        self.assertEqual(C.select_tools("zzz qqq", names), [])


class TestCapabilityContext(RegCase):
    def test_build_small_focused_context(self):
        self.reg.upsert("web-research", "research a question on the web and cite pages",
                        "1. web_search\n2. fetch_url the primary source", failure_modes=["answering from snippets"])
        self.reg.upsert("git-ops", "commit and branch with git")
        cc = CapabilityContext(self.reg)
        text, chosen = cc.build("research the latest python release on the web")
        self.assertEqual(chosen, ["web-research"])
        self.assertIn("fetch_url the primary source", text)
        self.assertIn("answering from snippets", text)
        self.assertNotIn("git-ops", text)
        self.assertIn("web_search [network]", text)
        brief, _ = cc.build("research the web", brief=True)
        self.assertNotIn("fetch_url the primary source", brief)
        limited, _ = cc.build("run a shell command to build", grant={"read-only"})
        self.assertNotIn("bash [", limited)
        self.assertIn("unavailable", limited)


class TestSkillFeedbackLoop(RegCase):
    def test_skill_injected_and_outcome_recorded(self):
        self.reg.upsert("web-research", "research a question on the web", "PROCEDURE-XYZ")
        seen = {}

        def chat(messages, **kw):
            seen["sys"] = messages[0]["content"]
            seen["grant"] = kw.get("tool_grant")
            return {"content": "answer", "_grade": "B", "done_reason": "stop"}
        eng = RunEngine(self.led, make_chat_executor(chat), capabilities=CapabilityContext(self.reg),
                        skills=self.reg, runs_root=os.path.join(self.tmp, "runs"), log=lambda m: None)
        rid = eng.create("research the web for X", permissions={"grant": ["read-only", "network"]})["run_id"]
        self.assertEqual(eng.execute(rid)["status"], "completed")
        self.assertIn("PROCEDURE-XYZ", seen["sys"])
        self.assertEqual(seen["grant"], {"read-only", "network"})
        self.assertEqual(self.reg.get("web-research")["successes"], 1)
        self.assertIn("skills_recorded", [e["type"] for e in self.led.events(rid)])


if __name__ == "__main__":
    unittest.main()

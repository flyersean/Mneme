"""Skill hot-reload — file-based skills (SKILL.md) are re-scanned when they change,
so a model that writes or edits a skill file sees it on the next turn with no restart.

Run: python3 tests/test_skills_reload.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness import Ledger  # noqa: E402
from mneme.harness.skills import SkillRegistry, parse_skill_md  # noqa: E402


def _write_skill(skills_dir, name, description, body):
    d = os.path.join(skills_dir, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")


class TestSkillsHotReload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_skill_reload_")
        self.skills_dir = os.path.join(self.tmp, "skills")
        os.makedirs(self.skills_dir)
        self.led = Ledger(os.path.join(self.tmp, "harness.db"))
        self.reg = SkillRegistry(self.led, dirs=[self.skills_dir])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_skill_file_is_seen_without_restart(self):
        self.assertEqual(self.reg.list(), [])
        _write_skill(self.skills_dir, "hello", "Say hello", "print hello")
        time.sleep(0.02)
        names = [s["name"] for s in self.reg.list()]
        self.assertIn("hello", names)

    def test_edited_skill_file_bumps_version_without_restart(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.reg.list()  # load v1
        self.assertEqual(self.reg.get("hello")["version"], 1)
        time.sleep(0.02)
        _write_skill(self.skills_dir, "hello", "Say hello", "v2 body")
        got = {s["name"]: s for s in self.reg.list()}
        self.assertEqual(got["hello"]["version"], 2)
        self.assertEqual(got["hello"]["body"], "v2 body")

    def test_edited_skill_seen_via_get_without_restart(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.assertEqual(self.reg.get("hello")["version"], 1)
        time.sleep(0.02)
        _write_skill(self.skills_dir, "hello", "Say hello", "v2 body")
        s = self.reg.get("hello")  # get() also reloads
        self.assertEqual(s["version"], 2)
        self.assertEqual(s["body"], "v2 body")

    def test_deleted_skill_file_is_removed_without_restart(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.assertIn("hello", [s["name"] for s in self.reg.list()])
        shutil.rmtree(os.path.join(self.skills_dir, "hello"))
        time.sleep(0.02)
        self.assertNotIn("hello", [s["name"] for s in self.reg.list()])

    def test_recreated_skill_file_reappears(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.reg.list()
        shutil.rmtree(os.path.join(self.skills_dir, "hello"))
        time.sleep(0.02)
        self.assertNotIn("hello", [s["name"] for s in self.reg.list()])
        _write_skill(self.skills_dir, "hello", "Say hello", "v2 body")
        time.sleep(0.02)
        got = {s["name"]: s for s in self.reg.list()}
        self.assertIn("hello", got)
        self.assertEqual(got["hello"]["body"], "v2 body")

    def test_soft_delete_keeps_row_but_inactive(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.reg.list()
        self.assertTrue(self.reg.delete("hello"))  # soft: deactivate
        inactive = {s["name"]: s for s in self.reg.list(include_inactive=True)}
        self.assertIn("hello", inactive)
        self.assertEqual(inactive["hello"]["active"], 0)
        self.assertNotIn("hello", [s["name"] for s in self.reg.list()])  # gone from active list

    def test_purge_removes_row_entirely(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.reg.list()
        self.assertTrue(self.reg.purge("hello"))
        self.assertNotIn("hello", [s["name"] for s in self.reg.list(include_inactive=True)])

    def test_edit_preserves_active_flag(self):
        self.reg.upsert("hello", "Say hello", "v1", actor="test", source="user")
        self.assertTrue(self.reg.get("hello")["active"])
        self.reg.set_active("hello", False)
        self.reg.upsert("hello", "Say hello edited", "v2", actor="test", source="user")
        s = self.reg.get("hello")
        self.assertEqual(s["active"], 0)  # edit must NOT re-activate a deactivated skill
        self.assertEqual(s["description"], "Say hello edited")
        self.assertEqual(s["version"], 2)

    def test_export_round_trips(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "do the thing")
        self.reg.list()
        md = self.reg.export("hello")
        self.assertIsNotNone(md)
        meta = parse_skill_md(md)
        self.assertEqual(meta["name"], "hello")
        self.assertEqual(meta["description"], "Say hello")
        self.assertIn("do the thing", meta["body"])

    def test_file_removed_then_recreated_reactivates(self):
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        self.reg.list()
        shutil.rmtree(os.path.join(self.skills_dir, "hello"))
        time.sleep(0.02)
        self.assertNotIn("hello", [s["name"] for s in self.reg.list()])
        # still exists (soft), source marked file-removed
        inactive = {s["name"]: s for s in self.reg.list(include_inactive=True)}
        self.assertIn("hello", inactive)
        self.assertTrue(inactive["hello"]["source"].startswith("file-removed:"))
        _write_skill(self.skills_dir, "hello", "Say hello", "v1 body")
        time.sleep(0.02)
        s = self.reg.get("hello")
        self.assertEqual(s["active"], 1)  # re-created -> available again


    def test_soft_delete_then_restore(self):
        self.reg.upsert("hello", "Say hello", "v1", actor="test", source="user")
        self.reg.delete("hello")  # soft
        self.assertNotIn("hello", [s["name"] for s in self.reg.list()])
        self.assertIn("hello", [s["name"] for s in self.reg.list(include_inactive=True)])
        self.reg.set_active("hello", True)  # restore
        self.assertIn("hello", [s["name"] for s in self.reg.list()])

    def test_select_include_inactive(self):
        self.reg.upsert("hello", "Say hello", "v1", actor="test", source="user")
        self.reg.delete("hello")  # soft
        self.assertEqual(self.reg.select("hello", k=5), [])
        got = [s["name"] for s in self.reg.select("hello", k=5, include_inactive=True)]
        self.assertIn("hello", got)


if __name__ == "__main__":
    unittest.main()

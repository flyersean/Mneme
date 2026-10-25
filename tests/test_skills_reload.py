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
from mneme.harness.skills import SkillRegistry  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

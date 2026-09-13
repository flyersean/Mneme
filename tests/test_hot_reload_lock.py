"""Tests for the hot-reload lock (runtime.hot_reload / MNEME_HOT_RELOAD=0).

When locked, prompt files are frozen at first read — a later edit to an override
file does NOT change the injected prompt until restart. Unlocked (default) keeps
live re-read. This is the guard against a coding/studying agent editing its own
prompts on a running proxy."""

import os
import shutil
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_lock_")
os.environ["MNEME_CHUNK_DIR"] = _TMP

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
from mneme import instructions  # noqa: E402

_OVERRIDE_DIR = os.path.join(_TMP, "instructions", "default")


def _write_override(name, text):
    os.makedirs(_OVERRIDE_DIR, exist_ok=True)
    with open(os.path.join(_OVERRIDE_DIR, name + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)


def _clear():
    instructions._PROMPT_CACHE.clear()
    shutil.rmtree(os.path.join(_TMP, "instructions"), ignore_errors=True)


class TestHotReloadLock(unittest.TestCase):
    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()
        os.environ.pop("MNEME_HOT_RELOAD", None)

    def test_locked_prompts_are_frozen(self):
        os.environ["MNEME_HOT_RELOAD"] = "0"
        name = "empty_answer_retry"
        first = instructions._load_instruction(name)
        self.assertIn("CONTINUE", first)  # shipped default
        _write_override(name, "CUSTOM PROMPT TEXT")
        second = instructions._load_instruction(name)
        self.assertEqual(second, first)  # frozen — the new override is ignored
        self.assertNotIn("CUSTOM PROMPT TEXT", second)

    def test_unlocked_prompts_are_live(self):
        os.environ["MNEME_HOT_RELOAD"] = "1"
        name = "empty_answer_retry"
        first = instructions._load_instruction(name)
        _write_override(name, "CUSTOM PROMPT TEXT")
        second = instructions._load_instruction(name)
        self.assertIn("CUSTOM PROMPT TEXT", second)
        self.assertNotEqual(second, first)

    def test_lock_freezes_each_name_independently(self):
        os.environ["MNEME_HOT_RELOAD"] = "0"
        a = instructions._load_instruction("empty_answer_retry")  # reads + caches
        _write_override("empty_answer_retry", "SHOULD NOT APPEAR")
        _write_override("tool_failure_nudge", "NEW NUDGE")
        # empty_answer_retry frozen (already cached); tool_failure_nudge not yet
        # cached, so its first read picks up the override and freezes it there.
        self.assertNotIn("SHOULD NOT APPEAR", instructions._load_instruction("empty_answer_retry"))
        self.assertIn("NEW NUDGE", instructions._load_instruction("tool_failure_nudge"))


if __name__ == "__main__":
    unittest.main()

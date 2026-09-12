"""Test the inject_enabled flag — save-only mode: injection off, classification kept."""

import os
import sys
import tempfile
import unittest

# ── Isolated environment BEFORE importing the proxy ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="mneme_test_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_ASK_REUSABLE"] = "0"
os.environ["MNEME_MEMORY_ONLY"] = "0"
os.environ["MNEME_TOPIC_SWITCH_GRACE"] = "0"
os.environ["MNEME_MAX_PER_TOPIC"] = "0"

_PROXY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY_DIR)
import mneme_proxy as mp  # noqa: E402


class TestInjectFlag(unittest.TestCase):
    def setUp(self):
        # Stub classification so the early-return path is tested deterministically
        # (the real classifier may hit the dead backend).
        self._orig_classify = mp._classify_problem_type
        mp._classify_problem_type = lambda q: "test_ptype"
        self._orig_inject = mp.INJECT_ENABLED

    def tearDown(self):
        mp._classify_problem_type = self._orig_classify
        mp.INJECT_ENABLED = self._orig_inject

    def test_inject_disabled_returns_empty_context_but_classifies(self):
        mp.INJECT_ENABLED = False
        ctx, ptype = mp.build_context("hello world")
        self.assertEqual(ctx, "")
        self.assertEqual(ptype, "test_ptype")

    def test_inject_disabled_skips_embed(self):
        # When injection is off, build_context must NOT embed/retrieve — so a dead
        # embed (None) can't matter.
        calls = []
        mp.INJECT_ENABLED = False
        mp._embed_query = lambda q: calls.append(q) or None
        mp.build_context("hello world")
        self.assertEqual(calls, [])  # no embed performed


if __name__ == "__main__":
    unittest.main()

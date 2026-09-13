"""Tests for the trust tier (per-chunk ingest + injection) and the [source: input]
provenance tag. Together they close the self-reinforcement loop: model-generated
chunks are 'unverified' and file-derived facts are cited honestly-but-unverified."""

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

_PROXY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY_DIR)
import mneme_proxy as mp  # noqa: E402


class TestComputeTrust(unittest.TestCase):
    def test_observed_sources_are_verified(self):
        self.assertEqual(mp._compute_trust("user", []), "verified")
        self.assertEqual(mp._compute_trust("page:example.com", []), "verified")
        self.assertEqual(mp._compute_trust("tool:terminal", []), "verified")

    def test_generated_sources_are_unverified(self):
        self.assertEqual(mp._compute_trust("model", []), "unverified")
        self.assertEqual(mp._compute_trust("unknown", []), "unverified")
        self.assertEqual(mp._compute_trust("document:foo", []), "unverified")

    def test_observed_source_with_model_content_is_unverified(self):
        # A merged group (user turn + model reply) must not pass as verified.
        msgs = [
            {"role": "user", "source": "user", "content": "question"},
            {"role": "assistant", "source": "model", "content": "answer"},
        ]
        self.assertEqual(mp._compute_trust("user", msgs), "unverified")

    def test_pure_observed_is_verified(self):
        msgs = [{"role": "user", "source": "user", "content": "question"}]
        self.assertEqual(mp._compute_trust("user", msgs), "verified")

    def test_input_source_is_unverified(self):
        # swarm read_dir content is staged as source='input', not 'user'.
        self.assertEqual(mp._compute_trust("input", []), "unverified")


class TestReadDirDetection(unittest.TestCase):
    def test_read_dir_content_detected(self):
        self.assertTrue(mp._looks_like_read_dir("--- story.txt ---\nOnce upon a time..."))

    def test_multi_file_read_dir_detected(self):
        self.assertTrue(mp._looks_like_read_dir("--- story.txt ---\nx\n\n--- critique.md ---\ny"))

    def test_normal_question_not_detected(self):
        self.assertFalse(mp._looks_like_read_dir("what is the capital of france?"))

    def test_plain_text_not_detected(self):
        self.assertFalse(mp._looks_like_read_dir("The sky is blue and the grass is green."))

    def test_empty_not_detected(self):
        self.assertFalse(mp._looks_like_read_dir(""))


class TestInputTag(unittest.TestCase):
    def _parsed(self, text):
        return mp._parse_inline_provenance(text)

    def test_bare_input_ok_when_input_present(self):
        p = self._parsed("The report says green. [source: input]")
        self.assertFalse(mp._has_fake_source(p, set(), set(), input_text="the report says green"))

    def test_bare_input_fake_when_no_input(self):
        p = self._parsed("The report says green. [source: input]")
        self.assertTrue(mp._has_fake_source(p, set(), set(), input_text=""))

    def test_named_input_ok_when_name_present(self):
        p = self._parsed("Green. [source: input:report.txt]")
        self.assertFalse(mp._has_fake_source(p, set(), set(), input_text="--- report.txt ---\ngreen"))

    def test_named_input_fake_when_name_missing(self):
        p = self._parsed("Green. [source: input:report.txt]")
        self.assertTrue(mp._has_fake_source(p, set(), set(), input_text="--- other.txt ---\ngreen"))

    def test_mem_and_url_sources_still_checked(self):
        # Regression guard: the existing mem/URL cross-check is unchanged.
        p = self._parsed("X is at 255 Main St. [source: mem_999999]")
        self.assertTrue(mp._has_fake_source(p, trace_chunks=set(), trace_urls=set(), input_text=""))
        p2 = self._parsed("X is at 255 Main St. [source: https://example.com/a]")
        self.assertTrue(mp._has_fake_source(p2, trace_chunks=set(), trace_urls=set(), input_text=""))


if __name__ == "__main__":
    unittest.main()

"""Tests for in-chat control commands (<<SETTINGS>>, <<RETRIEVAL ...>>).

These commands let the user view and change retrieval settings from the chat
instead of editing files. The risky part is the config rewrite: it must change
only the targeted key and leave comments, ordering, and every other setting
untouched (a naive yaml round-trip would strip the file's documentation).
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "proxy"))

from mneme import chatcmd as C  # noqa: E402

SAMPLE = """# Mneme proxy config
# Precedence: environment variable > this file > built-in default.

backend:
  type: ollama          # comment on type
  ollama_url: http://localhost:11434

# Retrieval: what gets injected into the prompt each turn.
retrieval:
  max_injected_tokens: 6000  # token budget for memory
  inject_min_similarity: 0.45  # THE main knob — absolute cosine floor
  keyword_fallback: false    # junk-prone; off
  topic_switch_sim: 0.45

timeouts:
  chat_timeout: 300
"""


class TestParse(unittest.TestCase):
    def test_parse_single_assignment(self):
        got = C.parse_set_assignments("inject_min_similarity=0.6")
        self.assertEqual(got, {"inject_min_similarity": 0.6})

    def test_parse_multiple_assignments(self):
        got = C.parse_set_assignments("inject_min_similarity=0.6 max_injected_tokens=4000")
        self.assertEqual(got, {"inject_min_similarity": 0.6, "max_injected_tokens": 4000})

    def test_int_key_parses_as_int(self):
        got = C.parse_set_assignments("max_per_topic=5")
        self.assertIsInstance(got["max_per_topic"], int)

    def test_unknown_key_rejected(self):
        with self.assertRaises(C.CommandError) as ctx:
            C.parse_set_assignments("temperature=0.9")
        self.assertIn("unknown retrieval key", str(ctx.exception))

    def test_malformed_token_rejected(self):
        with self.assertRaises(C.CommandError):
            C.parse_set_assignments("inject_min_similarity")

    def test_empty_rejected(self):
        with self.assertRaises(C.CommandError):
            C.parse_set_assignments("")

    def test_out_of_range_rejected(self):
        """A similarity above 1.0 is meaningless — catch it rather than inject nothing."""
        with self.assertRaises(C.CommandError) as ctx:
            C.parse_set_assignments("inject_min_similarity=1.5")
        self.assertIn("out of range", str(ctx.exception))

    def test_negative_rejected(self):
        with self.assertRaises(C.CommandError):
            C.parse_set_assignments("inject_min_similarity=-0.2")

    def test_non_numeric_rejected(self):
        with self.assertRaises(C.CommandError):
            C.parse_set_assignments("inject_min_similarity=abc")


class TestCommandRegex(unittest.TestCase):
    def test_settings_command_matches(self):
        self.assertTrue(C.SETTINGS_CMD_RE.search("<<SETTINGS>>"))
        self.assertTrue(C.SETTINGS_CMD_RE.search("please show <<settings>>"))

    def test_retrieval_bare_matches_with_no_args(self):
        m = C.RETRIEVAL_CMD_RE.search("<<RETRIEVAL>>")
        self.assertIsNotNone(m)
        self.assertFalse((m.group(1) or "").strip())

    def test_retrieval_with_args_matches(self):
        m = C.RETRIEVAL_CMD_RE.search("<<RETRIEVAL inject_min_similarity=0.6>>")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "inject_min_similarity=0.6")


class TestConfigRewrite(unittest.TestCase):
    """The rewrite must be surgical — comments and other keys survive."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "mneme.yaml")
        with open(self.p, "w") as f:
            f.write(SAMPLE)

    def _text(self):
        with open(self.p) as f:
            return f.read()

    def test_value_changed(self):
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        self.assertIn("inject_min_similarity: 0.6", self._text())

    def test_comments_preserved(self):
        """The config is self-documenting; a rewrite must not strip it."""
        before = self._text()
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        after = self._text()
        for comment in ("# Mneme proxy config", "# Precedence:",
                        "# THE main knob", "# junk-prone; off",
                        "# Retrieval: what gets injected", "# comment on type"):
            self.assertIn(comment, after, f"lost comment: {comment}")
        # Nothing outside retrieval changed
        self.assertIn("chat_timeout: 300", after)
        self.assertIn("type: ollama", after)

    def test_inline_comment_on_changed_line_survives(self):
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        line = [l for l in self._text().splitlines()
                if l.strip().startswith("inject_min_similarity")][0]
        self.assertIn("#", line, f"inline comment lost: {line!r}")
        self.assertIn("0.6", line)

    def test_other_keys_untouched(self):
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        t = self._text()
        self.assertIn("max_injected_tokens: 6000", t)
        self.assertIn("keyword_fallback: false", t)
        self.assertIn("topic_switch_sim: 0.45", t)

    def test_multiple_keys_at_once(self):
        C.update_config_file(self.p, "retrieval",
                             {"inject_min_similarity": 0.7, "max_injected_tokens": 4000})
        t = self._text()
        self.assertIn("inject_min_similarity: 0.7", t)
        self.assertIn("max_injected_tokens: 4000", t)

    def test_result_is_valid_yaml(self):
        import yaml
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        d = yaml.safe_load(self._text())
        self.assertEqual(d["retrieval"]["inject_min_similarity"], 0.6)
        self.assertEqual(d["retrieval"]["max_injected_tokens"], 6000)

    def test_float_formatting_is_clean(self):
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        self.assertNotIn("0.600000", self._text())
        self.assertIn("0.6", self._text())

    def test_new_key_inserted_into_existing_section(self):
        C.update_config_file(self.p, "retrieval", {"max_siblings": 5})
        import yaml
        d = yaml.safe_load(self._text())
        self.assertEqual(d["retrieval"]["max_siblings"], 5)

    def test_missing_section_is_appended(self):
        C.update_config_file(self.p, "newsection", {"thing": 1})
        import yaml
        d = yaml.safe_load(self._text())
        self.assertEqual(d["newsection"]["thing"], 1)

    def test_missing_file_raises(self):
        with self.assertRaises(C.CommandError):
            C.update_config_file("/nonexistent/nope.yaml", "retrieval", {"inject_min_similarity": 0.5})

    def test_unsafe_key_rejected(self):
        with self.assertRaises(C.CommandError):
            C.update_config_file(self.p, "retrieval", {"a: b\nc": 1})

    def test_write_is_atomic_no_tmp_left(self):
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        self.assertFalse(os.path.exists(self.p + ".tmp"), "temp file not cleaned up")

    def test_section_boundary_respected(self):
        """Changing retrieval must not spill into the next section."""
        C.update_config_file(self.p, "retrieval", {"inject_min_similarity": 0.6})
        import yaml
        d = yaml.safe_load(self._text())
        self.assertEqual(d["timeouts"]["chat_timeout"], 300)


class TestFormatSettings(unittest.TestCase):
    def test_report_contains_sections_and_values(self):
        snap = {
            "model": {"model": "hf.co/x/y:Q4", "backend": "ollama"},
            "sampling": {"temperature": 1.0, "top_k": 64},
            "retrieval": {"inject_min_similarity": 0.45},
            "overrides": {"inject_min_similarity": 0.6},
            "template": "gemma4-repeat",
        }
        out = C.format_settings(snap)
        self.assertIn("CURRENT MNEME SETTINGS", out)
        self.assertIn("hf.co/x/y:Q4", out)
        self.assertIn("inject_min_similarity", out)
        self.assertIn("gemma4-repeat", out)
        self.assertIn("<<RETRIEVAL", out)

    def test_report_handles_empty(self):
        out = C.format_settings({})
        self.assertIn("CURRENT MNEME SETTINGS", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

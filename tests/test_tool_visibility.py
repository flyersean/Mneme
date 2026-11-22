"""Tool-call visibility helpers: glyph/verb taxonomy + secret redaction.

These back the chat's Hermes-style tool-activity lines. The redaction is the
"must-have" acceptance criterion — a secret that leaks into a tool-args preview
reaches the client verbatim, so it is pinned here by test.
"""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_tv_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_EMBED_TIMEOUT"] = "1"
os.environ["MNEME_CHAT_TIMEOUT"] = "5"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402


class TestToolGlyph(unittest.TestCase):
    def test_known_tools_map(self):
        self.assertEqual(mp._tool_glyph("bash"), ("💻", "$"))
        self.assertEqual(mp._tool_glyph("read_file"), ("📖", "Read"))
        self.assertEqual(mp._tool_glyph("write"), ("✍️", "Write"))
        self.assertEqual(mp._tool_glyph("web_search"), ("🔎", "Search"))
        self.assertEqual(mp._tool_glyph("search_memory"), ("🧠", "Memory"))
        self.assertEqual(mp._tool_glyph("read_chunk"), ("🧠", "Memory"))
        self.assertEqual(mp._tool_glyph("start_run"), ("⚙️", "Run"))

    def test_unknown_tool_has_fallback(self):
        g, v = mp._tool_glyph("some_custom_tool")
        self.assertEqual(g, "🔧")
        self.assertEqual(v, "Some Custom Tool")


class TestSecretRedaction(unittest.TestCase):
    def test_secret_named_keys_redacted(self):
        out = mp._sanitize_args({"api_key": "sk-abc123", "command": "echo hi"})
        self.assertEqual(out["api_key"], "***")
        self.assertEqual(out["command"], "echo hi")

    def test_bearer_token_redacted_inline(self):
        out = mp._sanitize_args({"command": 'curl -H "Authorization: Bearer abcdefgh12345678"'})
        self.assertNotIn("abcdefgh12345678", out["command"])
        self.assertIn("Bearer ***", out["command"])

    def test_key_value_env_redacted_inline(self):
        out = mp._sanitize_args({"command": "export OPENROUTER_API_KEY=sk-xyz789 && echo done"})
        self.assertNotIn("sk-xyz789", out["command"])
        self.assertIn("OPENROUTER_API_KEY=***", out["command"])

    def test_nested_dict_redacted(self):
        out = mp._sanitize_args({"headers": {"authorization": "Bearer secret12345678"}})
        self.assertEqual(out["headers"]["authorization"], "***")

    def test_plain_value_untouched(self):
        self.assertEqual(mp._sanitize_args({"command": "ls -la"}), {"command": "ls -la"})


if __name__ == "__main__":
    unittest.main()

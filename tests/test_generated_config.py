"""The setup wizard's generated mneme.yaml must use only valid config keys — the
proxy rejects unknown keys at startup. This guards against adding a knob to the
template without registering it in _CONFIG_ENV_MAP."""

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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import yaml  # noqa: E402
import mneme_proxy as mp  # noqa: E402
import mneme_setup as setup  # noqa: E402


def _generated():
    y = setup._common_yaml("/tmp/inst", "/tmp/inst/mneme.db", 8080, "true", 64000, "false")
    y = (y.replace("@@BTYPE@@", "openai").replace("@@BPROV@@", "openrouter")
          .replace("@@MAIN@@", "test-model").replace("@@EMBED@@", "test-embed")
          .replace("@@LABEL@@", "test-label"))
    return yaml.safe_load(y)


class TestGeneratedConfig(unittest.TestCase):
    def test_all_sections_present(self):
        data = _generated()
        for sec in ("backend", "providers", "sampling", "timeouts", "storage",
                    "retrieval", "caps", "tools", "models"):
            self.assertIn(sec, data)

    def test_tools_section_complete(self):
        data = _generated()
        for t in ("native", "search_memory", "list_tools", "read_tool",
                  "read_file", "fetch_url", "web_search"):
            self.assertIn(t, data["tools"])

    def test_every_key_is_a_valid_config_key(self):
        data = _generated()
        for section, val in data.items():
            if section in mp._STRUCTURAL_SECTIONS:
                continue  # providers/models are free-form (read by provider resolution)
            if section in mp._CONFIG_ENV_MAP:
                continue  # top-level scalar key
            self.assertIsInstance(val, dict, f"section '{section}' must be a mapping")
            for key in val:
                flat = f"{section}.{key}"
                self.assertIn(flat, mp._CONFIG_ENV_MAP, f"unknown config key: {flat}")


if __name__ == "__main__":
    unittest.main()

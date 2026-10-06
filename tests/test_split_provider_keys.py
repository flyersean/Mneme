"""Regression: split chat/embed providers must each use their OWN key.

Chat on Routeway (ROUTEWAY_API_KEY), embed/label on OpenRouter (OPENROUTER_API_KEY).
The chat request must carry the Routeway key; embed/label must carry the OpenRouter
key. Previously `_resolve_provider` only mapped the chat key into OPENROUTER_API_KEY
when that env var was None, so a split setup kept the OpenRouter key and sent it to
the chat provider — a silent 401 "Invalid API key".
"""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_splitkey_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "openrouter"
os.environ["OPENROUTER_API_KEY"] = "sk-or-key"
os.environ["ROUTEWAY_API_KEY"] = "sk-routeway-key"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402


class TestSplitProviderKeys(unittest.TestCase):
    """The chat key (OPENROUTER_API_KEY carrier) and the OpenRouter aux key must
    not be conflated when the chat provider is a different vendor."""

    def tearDown(self):
        mp._AUX_OR_KEY = ""
        os.environ["OPENROUTER_API_KEY"] = "sk-or-key"

    def test_aux_key_returns_preserved_openrouter_key(self):
        # After _resolve_provider repoints OPENROUTER_API_KEY at the chat key,
        # embed/label on OpenRouter must still get the ORIGINAL OpenRouter key.
        mp._AUX_OR_KEY = "sk-or-key"
        os.environ["OPENROUTER_API_KEY"] = "sk-routeway-key"
        self.assertEqual(mp._aux_key("OPENROUTER_API_KEY"), "sk-or-key")

    def test_aux_key_other_providers_read_env_directly(self):
        mp._AUX_OR_KEY = "sk-or-key"
        os.environ["OPENROUTER_API_KEY"] = "sk-routeway-key"
        self.assertEqual(mp._aux_key("ROUTEWAY_API_KEY"), "sk-routeway-key")
        self.assertEqual(mp._aux_key("XAI_API_KEY"), "")  # unset

    def test_aux_key_empty_env_returns_empty(self):
        self.assertEqual(mp._aux_key(""), "")
        self.assertEqual(mp._aux_key("SOMETHING_UNSET"), "")

    def test_aux_key_no_preserve_falls_back_to_env(self):
        # When there's nothing preserved (chat IS OpenRouter), read env directly.
        mp._AUX_OR_KEY = ""
        os.environ["OPENROUTER_API_KEY"] = "sk-or-key"
        self.assertEqual(mp._aux_key("OPENROUTER_API_KEY"), "sk-or-key")

    def test_resolve_provider_preserves_openrouter_key_for_split_setup(self):
        # Drive the real resolver with a split config (chat=routeway, and an
        # OpenRouter key present for embed/label) and confirm the OpenRouter key
        # is preserved while the chat carrier is repointed.
        os.environ["OPENROUTER_API_KEY"] = "sk-or-key"
        os.environ["ROUTEWAY_API_KEY"] = "sk-routeway-key"
        os.environ["MNEME_PROVIDER"] = "routeway"
        mp.CONFIG_DATA["providers"] = {
            "routeway": {"base_url": "https://api.routeway.ai/v1",
                         "api_key_env": "ROUTEWAY_API_KEY", "model": "glm-5.3-flash"},
        }
        mp._resolve_provider()
        # Chat carrier now points at the Routeway key …
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-routeway-key")
        # … and the OpenRouter key was preserved for embed/label.
        self.assertEqual(mp._AUX_OR_KEY, "sk-or-key")
        self.assertEqual(mp._aux_key("OPENROUTER_API_KEY"), "sk-or-key")


if __name__ == "__main__":
    unittest.main()

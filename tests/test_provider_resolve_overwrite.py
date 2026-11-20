"""Regression test for the provider-switch base-URL staleness bug.

A provider switch (or a self-restart that inherits a stale OPENROUTER_BASE_URL
from the previous provider) used to leave the chat carrier pointing at the OLD
vendor: `_resolve_provider` set OPENROUTER_BASE_URL with set-if-absent semantics,
so a non-empty (stale) value was never overwritten, and the chat 401'd against
the wrong endpoint. The base URL is DERIVED from the active provider and must be
repointed on every resolve; the original OpenRouter carrier for embed/label is
preserved separately in _AUX_OR_BASE/_AUX_OR_KEY.

Run: python3 tests/test_provider_resolve_overwrite.py
"""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_resolve_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "openai"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402

_ENV_PRIOR = {k: os.environ.get(k) for k in ("OPENROUTER_BASE_URL", "OPENROUTER_API_KEY",
                                             "MNEME_PROVIDER", "MNEME_BACKEND")}


def tearDownModule():
    for k, v in _ENV_PRIOR.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestProviderResolveOverwritesBaseUrl(unittest.TestCase):
    def _reset(self):
        mp.CONFIG_DATA = {"providers": {}}
        mp._AUX_OR_CAPTURED = False
        mp._AUX_OR_KEY = ""
        mp._AUX_OR_BASE = ""
        mp._PROVIDER_HEADERS = {}

    def test_stale_base_url_is_overwritten_on_resolve(self):
        # Simulate a self-restart that inherited a stale base URL from routeway.
        self._reset()
        os.environ["MNEME_PROVIDER"] = "deepseek"
        os.environ["OPENROUTER_BASE_URL"] = "https://api.routeway.ai/v1"  # stale
        mp._resolve_provider()
        self.assertEqual(os.environ["OPENROUTER_BASE_URL"], "https://api.deepseek.com")

    def test_switching_provider_repoints_base_url(self):
        # A second resolve to a different provider must also overwrite (no stale).
        self._reset()
        os.environ["MNEME_PROVIDER"] = "routeway"
        os.environ["OPENROUTER_BASE_URL"] = "https://api.deepseek.com"  # now stale
        mp._resolve_provider()
        self.assertEqual(os.environ["OPENROUTER_BASE_URL"], "https://api.routeway.ai/v1")

    def test_openrouter_provider_uses_openrouter_base_url(self):
        self._reset()
        os.environ["MNEME_PROVIDER"] = "openrouter"
        os.environ["OPENROUTER_BASE_URL"] = "https://api.deepseek.com"  # stale
        mp._resolve_provider()
        self.assertEqual(os.environ["OPENROUTER_BASE_URL"], "https://openrouter.ai/api/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)

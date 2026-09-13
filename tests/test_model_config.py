"""Tests for per-model config in the Ollama payload: reasoning (think),
reasoning_effort, sampling overrides (presence_penalty / min_p / repetition_penalty),
and num_ctx. Verifies the payload Mneme actually sends to Ollama."""

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

# ── Isolated environment BEFORE importing the proxy ──
_TMP = tempfile.mkdtemp(prefix="mneme_modelcfg_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_MEMORY_ENABLED"] = "0"

import mneme_proxy as mp  # noqa: E402


class _FakeResp:
    encoding = "utf-8"

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


class TestPerModelConfig(unittest.TestCase):
    def _call(self, model_cfg, model="test-model"):
        """Run _query_model_impl with a per-model config, return the Ollama payload."""
        mp.CONFIG_DATA["models"] = {model: model_cfg}
        mp.MODEL = model
        captured = {}

        def fake_post(url, **kwargs):
            captured["payload"] = kwargs["json"]
            return _FakeResp([json.dumps({
                "message": {"role": "assistant", "content": "hi"},
                "done": True, "done_reason": "stop",
            })])

        orig = mp.requests.post
        mp.requests.post = fake_post
        try:
            mp._query_model_impl([{"role": "user", "content": "hello"}])
        finally:
            mp.requests.post = orig
        return captured["payload"]

    def test_default_reasoning_off_sends_think_false(self):
        p = self._call({})
        self.assertIs(p["think"], False)

    def test_reasoning_true_omits_think_false(self):
        p = self._call({"reasoning": True})
        self.assertNotIn("think", p)

    def test_reasoning_false_sends_think_false(self):
        p = self._call({"reasoning": False})
        self.assertIs(p["think"], False)

    def test_reasoning_effort_passes_through_and_implies_thinking(self):
        p = self._call({"reasoning_effort": "low"})
        self.assertEqual(p.get("reasoning_effort"), "low")
        self.assertNotIn("think", p)  # effort implies thinking on

    def test_reasoning_effort_and_reasoning_true(self):
        p = self._call({"reasoning": True, "reasoning_effort": "medium"})
        self.assertEqual(p.get("reasoning_effort"), "medium")
        self.assertNotIn("think", p)

    def test_sampling_overrides(self):
        p = self._call({"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                        "presence_penalty": 1.5, "min_p": 0.05, "repetition_penalty": 1.1})
        o = p["options"]
        self.assertEqual(o["temperature"], 0.7)
        self.assertEqual(o["top_p"], 0.8)
        self.assertEqual(o["top_k"], 20)
        self.assertEqual(o["presence_penalty"], 1.5)
        self.assertEqual(o["min_p"], 0.05)
        self.assertEqual(o["repetition_penalty"], 1.1)

    def test_num_ctx_override(self):
        p = self._call({"num_ctx": 32768})
        self.assertEqual(p["options"]["num_ctx"], 32768)

    def test_string_reasoning_value(self):
        p = self._call({"reasoning": "false"})
        self.assertIs(p["think"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

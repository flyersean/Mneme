"""Characterization tests for process_chat behaviour the harness relies on
(docs/harness/00-architecture-audit.md §8): the static/dynamic injection split and
the cancel flag. These pin CURRENT behaviour so later refactors (per-call turn
context, Phase 2+) cannot silently change it.

Run: python3 tests/test_process_chat_characterization.py
"""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_char_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_EMBED_TIMEOUT"] = "1"
os.environ["MNEME_CHAT_TIMEOUT"] = "5"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_ASK_REUSABLE"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402

MEM = "--- MEMORY: [mem_test] characterization ---\nuser: x\nassistant: y\n---"


def search_call():
    return {"content": "", "done_reason": "stop", "eval_count": 5,
            "tool_calls": [{"id": "c1", "function": {"name": "search_memory",
                                                     "arguments": {"query": "x"}}}]}


def answer(text="The answer. [guess]"):
    return {"content": text, "done_reason": "stop", "eval_count": 5, "tool_calls": []}


class Recorder:
    def __init__(self, replies, on_call=None):
        self.replies = list(replies)
        self.calls = []
        self.on_call = on_call

    def __call__(self, messages, *a, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.on_call:
            self.on_call(len(self.calls))
        return self.replies.pop(0) if self.replies else answer()


class TestProcessChatCharacterization(unittest.TestCase):
    def setUp(self):
        self._orig = (mp.query_model, mp.build_context, mp._execute_search_tool_calls)
        mp.build_context = lambda q: (MEM, "other")
        mp._execute_search_tool_calls = lambda calls: ("search results: nothing", set())
        mp._cancel_event.clear()

    def tearDown(self):
        mp.query_model, mp.build_context, mp._execute_search_tool_calls = self._orig
        mp._cancel_event.clear()

    def _msgs(self):
        return [{"role": "system", "content": "CLIENT SYSTEM"},
                {"role": "user", "content": "what is x?"}]

    def test_static_then_dynamic_injection_split(self):
        rec = Recorder([answer()])
        mp.query_model = rec
        mp.process_chat(self._msgs())
        sent = rec.calls[0]
        systems = [m for m in sent if m["role"] == "system"]
        self.assertEqual(systems[0]["content"], "CLIENT SYSTEM")          # client's stays first
        self.assertIn("=== MNEME MEMORY SYSTEM ===", systems[1]["content"])  # fixed block second
        self.assertNotIn("--- MEMORY:", systems[1]["content"])           # fixed block is static
        self.assertEqual(sent[-1]["role"], "user")                        # tail right before user
        self.assertEqual(sent[-2]["role"], "system")
        self.assertIn("--- MEMORY:", sent[-2]["content"])
        self.assertEqual(sum("=== MNEME MEMORY SYSTEM ===" in (m.get("content") or "") for m in sent), 1)

    def test_no_double_injection_across_tool_rounds(self):
        rec = Recorder([search_call(), answer()])
        mp.query_model = rec
        out = mp.process_chat(self._msgs())
        self.assertEqual(len(rec.calls), 2)
        for sent in rec.calls:
            texts = [m.get("content") or "" for m in sent if isinstance(m.get("content"), str)]
            self.assertEqual(sum("--- MEMORY:" in t for t in texts), 1)
            self.assertEqual(sum("=== MNEME MEMORY SYSTEM ===" in t for t in texts), 1)
        self.assertEqual(out["content"], "The answer. [guess]")
        self.assertEqual([t["tool"] for t in out["tool_trace"]], ["search_memory"])

    def test_cancel_between_rounds_stops_the_turn(self):
        rec = Recorder([search_call(), answer()], on_call=lambda n: mp._cancel_event.set())
        mp.query_model = rec
        out = mp.process_chat(self._msgs())
        self.assertEqual(len(rec.calls), 1)          # no re-query after Stop
        self.assertEqual(out["done_reason"], "cancelled")
        self.assertEqual(out["content"], "[Stopped by user.]")


class TestCancelScopes(unittest.TestCase):
    """Harness steps run process_chat in a private cancel scope."""

    def setUp(self):
        self._orig = (mp.query_model, mp.build_context, mp._execute_search_tool_calls)
        mp.build_context = lambda q: ("", "other")
        mp._execute_search_tool_calls = lambda calls: ("nothing", set())

    def tearDown(self):
        mp.query_model, mp.build_context, mp._execute_search_tool_calls = self._orig
        mp._cancel_event.clear()

    def test_chat_stop_does_not_stop_a_harness_step(self):
        mp.query_model = Recorder([search_call(), answer("kept going")])
        mp._cancel_event.set()                      # the chat UI's Stop button
        out = mp._scoped_process_chat([{"role": "user", "content": "q"}])
        self.assertEqual(out["content"], "kept going")
        self.assertIsNone(getattr(mp._cancel_local, "event", None))   # scope cleared

    def test_scope_event_stops_only_its_step(self):
        import threading
        ev = threading.Event()
        mp.query_model = Recorder([search_call(), answer()], on_call=lambda n: ev.set())
        out = mp._scoped_process_chat([{"role": "user", "content": "q"}], cancel_event=ev)
        self.assertEqual(out["done_reason"], "cancelled")
        self.assertFalse(mp._cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()

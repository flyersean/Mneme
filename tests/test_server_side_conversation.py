"""Server-side conversation persistence: a turn's reply must be written to the
conversation DB by the streaming worker, independent of the client. Guards the
"navigate away mid-turn loses the reply" bug."""

import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="mneme_ssc_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import mneme_proxy as mp  # noqa: E402


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestServerSideConversation(unittest.TestCase):

    def _stream_chat(self, client, cid, messages):
        """Post a streaming chat request and drain the SSE response to completion."""
        r = client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": messages,
            "stream": True,
            "conversation_id": cid,
        })
        # drain the stream so the worker finishes + persists
        r.get_data(as_text=True)
        return r

    def test_reply_persisted_server_side(self):
        with mock.patch.object(mp, "process_chat",
                               return_value={"content": "hello world", "thinking": "",
                                             "tool_calls": [], "_grade": "C"}):
            client = mp.app.test_client()
            conv = client.post("/conversations", json={}).get_json()["conversation"]
            cid = conv["id"]

            self._stream_chat(client, cid, [{"role": "user", "content": "hi"}])

            msgs = client.get(f"/conversations/{cid}").get_json()["conversation"]["messages"]
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertEqual(msgs[1]["content"], "hello world")

    def test_no_conversation_id_no_write(self):
        """Generic clients that don't pass conversation_id must not write conversations."""
        with mock.patch.object(mp, "process_chat",
                               return_value={"content": "hi", "thinking": "",
                                             "tool_calls": [], "_grade": "C"}):
            client = mp.app.test_client()
            # no conversation_id in the request -> nothing persisted, no crash
            r = client.post("/v1/chat/completions", json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            })
            r.get_data(as_text=True)
            # list should be empty (or not contain our turn)
            convs = client.get("/conversations").get_json()["conversations"]
            self.assertEqual(convs, [])


if __name__ == "__main__":
    unittest.main()

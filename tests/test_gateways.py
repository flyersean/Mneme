"""Phase 9: gateway interface — routing, auth, run watching, Telegram transport.
HTTP is faked; no network.

Run: python3 tests/test_gateways.py
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "extensions", "gateways"))

from base import Gateway, HarnessClient, Message  # noqa: E402
from cli import CLIGateway  # noqa: E402
from telegram import TelegramGateway  # noqa: E402


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._b


class FakeHTTP:
    def __init__(self):
        self.posts, self.gets = [], []
        self.run_status = "running"
        self.updates = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/harness/command"):
            if json["text"].startswith("/run "):
                return Resp(200, {"reply": "started run_1a2b3c4d5e6f7a8b — /status 5e6f7a8b"})
            return Resp(200, {"reply": f"did {json['text']} as {json['actor']}"})
        if url.endswith("/v1/chat/completions"):
            return Resp(200, {"choices": [{"message": {"content": f"echo {json['messages'][-1]['content']}"}}]})
        return Resp(200, {"ok": True})

    def get(self, url, params=None, timeout=None):
        self.gets.append(url)
        if "/runs/" in url:
            return Resp(200, {"run": {"status": self.run_status, "result": "the answer", "error": ""}})
        if url.endswith("/getUpdates"):
            u, self.updates = self.updates, []
            return Resp(200, {"result": u})
        return Resp(404, {})


class Collect(Gateway):
    name = "test"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent = []

    def receive(self):
        return []

    def send(self, msg, text):
        self.sent.append(text)


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.client = HarnessClient("http://x", session=self.http)

    def test_routing_and_identity(self):
        gw = Collect(self.client)
        self.assertEqual(gw.handle(Message("u1", "/status")), "did /status as test:u1")
        self.assertEqual(gw.handle(Message("u1", "hello")), "echo hello")
        self.assertEqual(gw.handle(Message("u1", "again")), "echo again")
        msgs = self.http.posts[-1][1]["messages"]
        self.assertEqual([m["content"] for m in msgs], ["hello", "echo hello", "again"])  # per-user history
        gw_run = Collect(self.client, plain_text="run")
        self.assertIn("started run_", gw_run.handle(Message("u1", "research X")))
        self.assertEqual(self.http.posts[-1][1]["text"], "/run research X")

    def test_authorization(self):
        gw = Collect(self.client, allowed_users=["ok"])
        self.assertIn("Not authorized", gw.handle(Message("intruder", "/runs")))
        self.assertTrue(gw.handle(Message("ok", "/runs")).startswith("did"))

    def test_started_runs_are_watched_and_notified(self):
        gw = Collect(self.client)
        m = Message("u1", "/run find the thing")
        gw.handle(m)
        self.assertIn("run_1a2b3c4d5e6f7a8b", gw.watched)
        gw.poll_watched()
        self.assertEqual(gw.sent, [])                                # still running: silent
        self.http.run_status = "awaiting_approval"
        gw.poll_watched()
        gw.poll_watched()                                            # notified once per state
        self.assertEqual(len(gw.sent), 1)
        self.assertIn("/approve", gw.sent[0])
        self.http.run_status = "completed"
        gw.poll_watched()
        self.assertIn("the answer", gw.sent[-1])
        self.assertEqual(gw.watched, {})

    def test_cli(self):
        out = io.StringIO()
        gw = CLIGateway(self.client, stream=io.StringIO("/runs\n/quit\n"), out=out)
        gw.serve(poll_every=999, stop=lambda: gw.done)
        self.assertIn("did /runs", out.getvalue())

    def test_telegram(self):
        with self.assertRaises(SystemExit):
            TelegramGateway(self.client, "tok", [], session=self.http)
        gw = TelegramGateway(self.client, "tok", ["42"], session=self.http, poll_timeout=0)
        self.http.updates = [
            {"update_id": 7, "message": {"text": "/status", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}},
            {"update_id": 8, "message": {"text": "/runs", "from": {"id": 42}, "chat": {"id": -5, "type": "group"}}},
        ]
        msgs = gw.receive()
        self.assertEqual(gw.offset, 9)
        self.assertTrue(gw.handle(msgs[0]).startswith("did /status as telegram:42"))
        self.assertEqual(gw.handle(msgs[1]), "Not authenticated.")      # groups refused
        gw.send(msgs[0], "x" * 9000)
        sends = [p for p in self.http.posts if p[0].endswith("/sendMessage")]
        self.assertEqual([len(p[1]["text"]) for p in sends], [4000, 4000, 1000])


class TestHarnessClientAuth(unittest.TestCase):
    def test_token_adds_auth_header(self):
        import requests as _r
        s = _r.Session()
        HarnessClient("http://x", session=s, token="tok-1")
        self.assertEqual(s.headers.get("Authorization"), "Bearer tok-1")

    def test_no_token_no_header(self):
        import requests as _r
        s = _r.Session()
        HarnessClient("http://x", session=s)
        self.assertNotIn("Authorization", s.headers)

    def test_command_401_is_clear_not_json_error(self):
        class RawResp:
            status_code = 401
            content = b"unauthorized"
            text = "unauthorized"
            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        class RawHTTP:
            def post(self, url, json=None, timeout=None):
                return RawResp()

        c = HarnessClient("http://x", session=RawHTTP(), token="tok-1")
        reply = c.command("/status", "u1")
        self.assertIn("unauthorized", reply)
        self.assertNotIn("JSONDecodeError", reply)


if __name__ == "__main__":
    unittest.main()

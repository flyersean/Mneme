"""Harness wired into the real proxy: /runs API end-to-end (Flask test client, stubbed
process_chat), the chat-executor success/failure rules, and the /api/chat/completions
return-value regression.

Run: python3 tests/test_harness_proxy.py
"""

import os
import sys
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_harness_proxy_")
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))
import mneme_proxy as mp  # noqa: E402
from mneme.harness.chat_executor import make_chat_executor  # noqa: E402


class FakeChat:
    def __init__(self):
        self.replies = []
        self.calls = []

    def __call__(self, messages, session_id="default", tools=None, **kw):
        self.calls.append((messages, session_id))
        if self.replies:
            return self.replies.pop(0)
        user = messages[-1]["content"]
        return {"content": f"done: {user}", "_grade": "B", "done_reason": "stop",
                "tool_trace": [{"tool": "bash", "args": {"command": "ls"},
                                "result": "x" * 120, "elapsed_ms": 3}]}


def wait_status(client, rid, want, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/runs/{rid}").get_json()["run"]
        if run["status"] in want:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {rid} never reached {want}")


class TestHarnessProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert mp.HARNESS is not None, "harness failed to initialize in the proxy"
        cls.fake = FakeChat()
        mp.HARNESS.executor = make_chat_executor(cls.fake)
        cls.c = mp.app.test_client()

    def setUp(self):
        self.fake.replies.clear()
        self.fake.calls.clear()

    def test_ledger_beside_shared_db(self):
        self.assertEqual(os.path.dirname(mp.HARNESS.ledger.path), os.path.abspath(mp.DB_DIR))

    def test_create_run_executes_and_records(self):
        r = self.c.post("/runs", json={"goal": "ship it", "tasks": ["one", "two"]})
        self.assertEqual(r.status_code, 201)
        rid = r.get_json()["run"]["run_id"]
        run = wait_status(self.c, rid, {"completed", "failed"})
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"], "done: two")
        d = self.c.get(f"/runs/{rid}?events=1").get_json()
        self.assertEqual([t["status"] for t in d["tasks"]], ["completed", "completed"])
        self.assertEqual(len(d["tool_calls"]), 2)
        self.assertEqual(d["tool_calls"][0]["status"], "success")
        self.assertIn("run_completed", [e["type"] for e in d["events"]])
        # step 2 saw step 1's result through the harness context, not model memory
        sys_msg = self.fake.calls[1][0][0]["content"]
        self.assertIn("MNEME HARNESS", sys_msg)
        self.assertIn("done: one", sys_msg)
        self.assertEqual(self.fake.calls[0][1], f"run:{rid}")
        ev = self.c.get(f"/runs/{rid}/events?types=run_completed").get_json()
        self.assertEqual(len(ev["events"]), 1)
        self.assertIn(rid, [x["run_id"] for x in self.c.get("/runs").get_json()["runs"]])

    def test_pause_resume_cancel_via_http(self):
        rid = self.c.post("/runs", json={"goal": "g", "start": False}).get_json()["run"]["run_id"]
        self.assertEqual(self.c.post(f"/runs/{rid}/pause").get_json()["run"]["status"], "paused")
        self.c.post(f"/runs/{rid}/resume")
        self.assertEqual(wait_status(self.c, rid, {"completed"})["status"], "completed")
        self.assertEqual(self.c.post(f"/runs/{rid}/cancel").status_code, 409)
        self.assertEqual(self.c.post(f"/runs/{rid}/checkpoint").status_code, 200)

    def test_failure_rules_then_retry(self):
        self.fake.replies += [
            {"content": "", "_grade": "F", "done_reason": "stop"},                  # empty
            {"content": "made up [source: x]", "_grade": "F", "done_reason": "stop"},  # graded F
            {"content": "x", "_grade": "B", "done_reason": "stop",
             "tool_calls": [{"function": {"name": "client_tool"}}]},               # unexecutable
        ]
        rid = self.c.post("/runs", json={"goal": "g", "budget": {"max_failures": 3}}).get_json()["run"]["run_id"]
        run = wait_status(self.c, rid, {"failed", "completed"})
        self.assertEqual(run["status"], "failed")
        errs = [s["error"] for s in self.c.get(f"/runs/{rid}").get_json()["steps"]]
        self.assertIn("empty model output", errs[0])
        self.assertIn("graded F", errs[1])
        self.assertIn("client_tool", errs[2])
        self.c.post(f"/runs/{rid}/retry")
        self.assertEqual(wait_status(self.c, rid, {"completed"})["attempt"], 2)

    def test_errors(self):
        self.assertEqual(self.c.get("/runs/run_nope").status_code, 404)
        self.assertEqual(self.c.post("/runs/run_nope/pause").status_code, 404)
        self.assertEqual(self.c.post("/runs", json={"goal": ""}).status_code, 400)
        self.assertEqual(self.c.post("/runs", json={"goal": "g", "budget": {"x": 1}}).status_code, 400)

    def test_artifact_registration(self):
        rid = self.c.post("/runs", json={"goal": "g", "start": False}).get_json()["run"]["run_id"]
        p = os.path.join(_TMP, "a.txt")
        with open(p, "w") as f:
            f.write("hi")
        r = self.c.post(f"/runs/{rid}/artifacts", json={"path": p, "description": "d"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.get_json()["artifact"]["sha256"]), 64)

    def test_api_chat_completions_returns_response(self):
        # Regression: the non-/v1 non-stream branch built its response and never returned it.
        orig = mp.process_chat
        mp.process_chat = lambda messages, **kw: {"content": "hello", "_grade": "B", "tool_calls": []}
        try:
            r = self.c.post("/api/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        finally:
            mp.process_chat = orig
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["message"]["content"], "hello")


if __name__ == "__main__":
    unittest.main()

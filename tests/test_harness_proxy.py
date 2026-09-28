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
from mneme.harness.chat_executor import make_chat_executor, make_chat_planner  # noqa: E402


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
        assert mp.HARNESS.planner is not None, "proxy should wire a planner"
        mp.HARNESS.executor = make_chat_executor(cls.fake)
        mp.HARNESS.planner = make_chat_planner(cls.fake)
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
        rid = self.c.post("/runs", json={"goal": "g", "plan": False,
                                         "budget": {"max_failures": 3}}).get_json()["run"]["run_id"]
        run = wait_status(self.c, rid, {"failed", "completed"})
        self.assertEqual(run["status"], "failed")
        errs = [s["error"] for s in self.c.get(f"/runs/{rid}").get_json()["steps"]]
        self.assertIn("empty model output", errs[0])
        self.assertIn("graded F", errs[1])
        self.assertIn("client_tool", errs[2])
        self.c.post(f"/runs/{rid}/retry")
        self.assertEqual(wait_status(self.c, rid, {"completed"})["attempt"], 2)

    def test_planned_run_via_http(self):
        # Proxy wiring: no tasks -> the model plans (PLAN:/VERIFY: lines), harness executes + verifies.
        ws_holder = {}

        def plan_reply(messages, session_id="default", tools=None, **kw):
            sys_txt = messages[0]["content"]
            ws = sys_txt.split("run workspace: ")[1].split(")")[0]
            ws_holder["ws"] = ws
            return {"content": f"Two steps.\nPLAN: write the file\nVERIFY: test -f {ws}/out.txt\nPLAN: report",
                    "_grade": "B", "done_reason": "stop"}

        def write_reply(messages, **kw):
            open(os.path.join(ws_holder["ws"], "out.txt"), "w").write("ok")
            return {"content": "wrote it", "_grade": "B", "done_reason": "stop"}
        orig_call = FakeChat.__call__
        seq = [plan_reply, write_reply]
        FakeChat.__call__ = lambda s, m, **kw: (seq.pop(0)(m, **kw) if seq else orig_call(s, m, **kw))
        try:
            rid = self.c.post("/runs", json={"goal": "make out.txt"}).get_json()["run"]["run_id"]
            run = wait_status(self.c, rid, {"completed", "failed"})
        finally:
            FakeChat.__call__ = orig_call
        self.assertEqual(run["status"], "completed", run.get("error"))
        self.assertEqual(run["plan"]["source"], "planner", self.c.get(f"/runs/{rid}?events=1").get_json()["steps"][0])
        d = self.c.get(f"/runs/{rid}?events=1").get_json()
        self.assertEqual([t["title"] for t in d["tasks"]], ["write the file", "report"])
        types = [e["type"] for e in d["events"]]
        self.assertIn("verification_passed", types)
        self.assertEqual(d["steps"][0]["kind"], "plan")

    def test_strategy_history_endpoint(self):
        mp._save_strategy("ALWAYS check the exit code of a build step.", "A", abstract=False,
                          problem_type="code", created_by="run:test")
        sid = mp.db.execute("SELECT strategy_id FROM strategies ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        h = self.c.get(f"/strategies/{sid}/history").get_json()
        self.assertEqual(h["current"]["created_by"], "run:test")
        self.assertEqual([x["event"] for x in h["history"]], ["saved"])

    def test_skills_api_and_shipped_skill(self):
        self.assertIn("swarm-creation", [s["name"] for s in self.c.get("/skills").get_json()["skills"]])
        r = self.c.post("/skills", json={"name": "t-skill", "description": "test skill", "body": "v1"})
        self.assertEqual(r.status_code, 201)
        self.c.post("/skills", json={"name": "t-skill", "description": "test skill", "body": "v2"})
        d = self.c.get("/skills/t-skill").get_json()
        self.assertEqual((d["skill"]["version"], len(d["history"])), (2, 2))
        self.assertEqual(self.c.post("/skills/t-skill/restore", json={"version": 1}).get_json()["skill"]["body"], "v1")
        self.assertEqual(self.c.post("/skills", json={"name": "BAD NAME", "description": "x"}).status_code, 400)

    def test_tool_grant_hides_and_blocks_tools(self):
        seen = []

        def qm(messages, tools=None, **kw):
            seen.append({(t.get("function") or {}).get("name") for t in (tools or [])})
            if len(seen) == 1:
                return {"content": "", "done_reason": "stop", "eval_count": 1,
                        "tool_calls": [{"id": "x", "function": {"name": "bash", "arguments": {"command": "echo hi"}}}]}
            return {"content": "done [guess]", "done_reason": "stop", "eval_count": 1, "tool_calls": []}
        orig = mp.query_model
        mp.query_model = qm
        try:
            out = mp._scoped_process_chat([{"role": "user", "content": "run echo"}], tool_grant={"read-only"})
        finally:
            mp.query_model = orig
        self.assertNotIn("bash", seen[0])
        self.assertIn("read_file", seen[0])
        self.assertEqual(out["tool_trace"], [])                      # bash was NOT executed
        self.assertEqual([tc["function"]["name"] for tc in out["tool_calls"]], ["bash"])  # -> step failure

    def test_evolution_api_instruction_roundtrip(self):
        from mneme.instructions import list_instructions
        cur = lambda: next(i["content"] for i in list_instructions() if i["name"] == "harness_judge")
        before = cur()
        r = self.c.post("/evolution", json={"kind": "instruction", "target": "harness_judge",
                                            "content": before + "\nBe terse.", "reason": "shorter verdicts"})
        pid = r.get_json()["proposal"]["proposal_id"]
        self.assertEqual(r.get_json()["proposal"]["status"], "proposed")    # L3: waits for approval
        self.assertEqual(cur(), before)
        self.assertEqual(self.c.post(f"/evolution/{pid}/approve").get_json()["proposal"]["status"], "applied")
        self.assertTrue(cur().endswith("Be terse."))
        self.assertEqual(self.c.post(f"/evolution/{pid}/rollback").get_json()["proposal"]["status"], "rolled_back")
        self.assertEqual(cur(), before)
        k = self.c.post("/evolution", json={"kind": "knowledge", "target": "t", "content": "note"}).get_json()
        self.assertEqual(k["proposal"]["status"], "applied")
        self.assertEqual(self.c.post("/evolution", json={"kind": "bogus", "target": "t", "content": "x"}).status_code, 400)

    def test_control_plane(self):
        # /commands typed in chat are answered by the harness without calling the model
        orig = mp.query_model
        mp.query_model = lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be called"))
        try:
            out = mp.process_chat([{"role": "user", "content": "/help"}])
        finally:
            mp.query_model = orig
        self.assertEqual(out["done_reason"], "command")
        self.assertIn("/approve", out["content"])
        r = self.c.post("/harness/command", json={"text": "/profiles"})
        self.assertIn("researcher", r.get_json()["reply"])
        self.assertIn("model=", self.c.post("/harness/command", json={"text": "models"}).get_json()["reply"])
        self.assertEqual(self.c.post("/harness/command", json={"text": "/nope"}).status_code, 400)
        self.assertIn("runs", self.c.get("/harness/metrics").get_json())
        ui = self.c.get("/runs/ui")
        self.assertEqual(ui.status_code, 200)
        self.assertIn(b"System evolution", ui.data)
        # inside a harness step, a task that starts with "/" is NOT treated as a command
        import threading
        mp._cancel_local.event = threading.Event()
        try:
            self.assertIsNone(mp._harness_command("/help"))
        finally:
            mp._cancel_local.event = None

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

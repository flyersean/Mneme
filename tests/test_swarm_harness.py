"""Phase 10 end-to-end: the swarm records itself as an external harness run over
real HTTP (the proxy app on a local port), fails, resumes at the right step with
--resume-run, and records parallel sub-steps as child runs. The harness never
executes an external run, and extensions can only write to external runs.

Run: python3 tests/test_swarm_harness.py
"""

import os
import sys
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_swarm_h_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ.update({"MNEME_BACKEND": "ollama", "MNEME_OLLAMA_URL": "http://127.0.0.1:1",
                   "MNEME_EMBED_TIMEOUT": "1", "MNEME_CHAT_TIMEOUT": "5", "MNEME_MODEL": "test-model",
                   "EMBED_MODEL": "test-embed", "LABEL_MODEL": "test-label", "MNEME_ASK_REUSABLE": "0"})

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
sys.path.insert(0, os.path.join(_ROOT, "extensions", "swarm"))
import mneme_proxy as mp  # noqa: E402
from swarm_orchestrator import Orchestrator  # noqa: E402
from swarm_p_orchestrator import ParallelOrchestrator  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

MODEL_CALLS = []


def fake_query_model(messages, *a, **kw):
    MODEL_CALLS.append(messages[-1].get("content"))
    return {"content": "A draft. [guess]", "done_reason": "stop", "eval_count": 3, "tool_calls": []}


class TestSwarmOnHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mp.query_model = fake_query_model
        mp.build_context = lambda q: ("", "other")
        cls.srv = make_server("127.0.0.1", 0, mp.app, threaded=True)
        cls.port = cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.c = mp.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="swarm_work_", dir=_TMP)
        os.makedirs(os.path.join(self.work, "input"))
        with open(os.path.join(self.work, "input", "brief.txt"), "w") as f:
            f.write("write about tides")
        self._cwd = os.getcwd()
        os.chdir(self.work)
        MODEL_CALLS.clear()

    def tearDown(self):
        os.chdir(self._cwd)

    def write_cfg(self, body):
        p = os.path.join(self.work, "swarm.yaml")
        with open(p, "w") as f:
            f.write(body.replace("PORT", str(self.port)))
        return p

    def events(self, rid, types=None):
        q = f"?types={types}" if types else ""
        return self.c.get(f"/runs/{rid}/events{q}").get_json()["events"]

    def test_record_fail_and_resume(self):
        cfg = self.write_cfg("""
harness: {port: PORT, goal: "tides swarm"}
steps:
  - name: draft
    port: PORT
    read_dir: input
    write_dir: out/draft.txt
  - name: check
    exec: "test -f ok.flag"
  - name: finish
    goto: END
""")
        o = Orchestrator(cfg)
        with self.assertRaises(SystemExit):
            o.run()                                   # exec fails: ok.flag missing
        rid = o.recorder.run_id
        run = self.c.get(f"/runs/{rid}").get_json()
        self.assertEqual(run["run"]["status"], "failed")
        self.assertEqual(run["run"]["meta"]["external"], "swarm")
        done = self.events(rid, "swarm_step_completed")
        self.assertEqual([(e["data"]["step"], e["data"]["next"]) for e in done], [("draft", "check")])
        self.assertTrue(run["artifacts"][0]["path"].endswith("out/draft.txt"))
        self.assertEqual(len(MODEL_CALLS), 1)
        # the harness never executes or resumes an external run itself
        self.assertEqual(self.c.post(f"/runs/{rid}/resume").status_code, 409)
        self.assertEqual(self.c.post(f"/runs/{rid}/retry").status_code, 409)

        open("ok.flag", "w").close()
        o2 = Orchestrator(cfg, resume_run_id=rid)
        o2.run()
        run = self.c.get(f"/runs/{rid}").get_json()["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(MODEL_CALLS), 1)          # 'draft' NOT redone — resumed at 'check'
        steps = [e["data"]["step"] for e in self.events(rid, "swarm_step_completed")]
        self.assertEqual(steps, ["draft", "check", "finish"])
        self.assertIn("swarm_resumed", [e["type"] for e in self.events(rid)])

    def test_parallel_children_and_guards(self):
        cfg = self.write_cfg("""
harness: {port: PORT}
steps:
  - parallel:
      - {name: a, port: PORT, read_dir: input, write_dir: pass/a.txt}
      - {name: b, port: PORT, read_dir: input, write_dir: pass/b.txt}
  - {name: end, goto: END}
""")
        o = ParallelOrchestrator(cfg)
        o.run()
        rid = o.recorder.run_id
        self.assertEqual(self.c.get(f"/runs/{rid}").get_json()["run"]["status"], "completed")
        kids = self.c.get(f"/runs?parent={rid}").get_json()["runs"]
        self.assertEqual(sorted(k["goal"] for k in kids), ["parallel step a", "parallel step b"])
        self.assertTrue(all(k["status"] == "completed" for k in kids))
        # extensions cannot write events into a harness-driven run
        own = self.c.post("/runs", json={"goal": "g", "start": False, "plan": False}).get_json()["run"]["run_id"]
        self.assertEqual(self.c.post(f"/runs/{own}/events", json={"type": "x"}).status_code, 409)
        self.assertEqual(self.c.post(f"/runs/{own}/status", json={"status": "completed"}).status_code, 409)
        self.assertEqual(self.c.post(f"/runs/{rid}/events", json={"type": "Bad Type"}).status_code, 400)

    def test_unreachable_harness_does_not_break_the_swarm(self):
        cfg = self.write_cfg("""
harness: {port: 1}
steps:
  - {name: only, exec: "true"}
""")
        o = Orchestrator(cfg)
        o.run()                                       # warns and continues
        self.assertFalse(o.recorder.enabled)
        cfg = self.write_cfg("""
harness: {port: 1, required: true}
steps:
  - {name: only, exec: "true"}
""")
        with self.assertRaises(SystemExit):
            Orchestrator(cfg).run()


if __name__ == "__main__":
    unittest.main()

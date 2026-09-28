"""Harness ledger: schema, state machine, append-only events, artifacts, checkpoints,
and cross-process ownership. Standalone — no proxy import.

Run: python3 tests/test_harness_ledger.py
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy"))

from mneme.harness.ledger import (  # noqa: E402
    Ledger, LedgerError, InvalidTransition, RUN_TRANSITIONS, SCHEMA_VERSION, owner_is_dead, process_owner,
)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mneme_ledger_")
        self.path = os.path.join(self.tmp, "harness.db")
        self.led = Ledger(self.path)

    def tearDown(self):
        self.led.close()


class TestSchema(LedgerTestCase):
    def test_schema_idempotent_and_versioned(self):
        self.assertEqual(self.led.schema_version(), SCHEMA_VERSION)
        again = Ledger(self.path)  # re-open: CREATE IF NOT EXISTS must not fail
        self.assertEqual(again.schema_version(), SCHEMA_VERSION)
        again.close()

    def test_memory_db_untouched(self):
        # The ledger is its own file — it never creates memory tables.
        names = {r[0] for r in sqlite3.connect(self.path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("chunks", names)
        self.assertTrue({"runs", "tasks", "steps", "tool_calls", "events",
                         "artifacts", "checkpoints"} <= names)


class TestRuns(LedgerTestCase):
    def test_create_run_default_single_task(self):
        run = self.led.create_run("Summarize the repo")
        self.assertEqual(run["status"], "created")
        tasks = self.led.list_tasks(run["run_id"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["instructions"], "Summarize the repo")
        self.assertEqual(run["plan"]["source"], "default")
        types = [e["type"] for e in self.led.events(run["run_id"])]
        self.assertEqual(types[0], "run_created")
        self.assertIn("task_created", types)
        self.assertIn("plan_created", types)

    def test_create_run_with_tasks(self):
        run = self.led.create_run("g", ["a", {"title": "b", "instructions": "do b"}])
        tasks = self.led.list_tasks(run["run_id"])
        self.assertEqual([t["title"] for t in tasks], ["a", "b"])
        self.assertEqual(tasks[1]["instructions"], "do b")
        self.assertEqual([t["seq"] for t in tasks], [0, 1])

    def test_create_run_validation(self):
        with self.assertRaises(LedgerError):
            self.led.create_run("   ")
        with self.assertRaises(LedgerError):
            self.led.create_run("g", [{"nope": 1}])

    def test_transitions(self):
        rid = self.led.create_run("g")["run_id"]
        self.led.transition(rid, "running")
        self.led.transition(rid, "paused")
        self.led.transition(rid, "running")
        self.led.transition(rid, "completed", result="done")
        self.assertEqual(self.led.get_run(rid)["result"], "done")
        with self.assertRaises(InvalidTransition):
            self.led.transition(rid, "running")  # terminal
        ev = [e for e in self.led.events(rid) if e["type"] == "run_completed"]
        self.assertEqual(ev[0]["data"], {"from": "running", "to": "completed"})

    def test_terminal_only_leaves_via_retry(self):
        self.assertEqual(RUN_TRANSITIONS["completed"], frozenset())
        self.assertEqual(RUN_TRANSITIONS["failed"], frozenset({"created"}))
        rid = self.led.create_run("g")["run_id"]
        self.led.transition(rid, "failed")
        with self.assertRaises(InvalidTransition):
            self.led.transition(rid, "running")
        self.led.transition(rid, "created", event="run_retry")

    def test_expect_guard(self):
        rid = self.led.create_run("g")["run_id"]
        with self.assertRaises(InvalidTransition):
            self.led.transition(rid, "running", expect=["paused"])

    def test_update_run_rejects_status_and_unknown(self):
        rid = self.led.create_run("g")["run_id"]
        with self.assertRaises(LedgerError):
            self.led.update_run(rid, status="running")
        with self.assertRaises(LedgerError):
            self.led.update_run(rid, bogus=1)

    def test_json_roundtrip(self):
        rid = self.led.create_run("g", budget={"max_steps": 4}, meta={"k": [1, 2]})["run_id"]
        run = self.led.get_run(rid)
        self.assertEqual(run["budget"], {"max_steps": 4})
        self.assertEqual(run["meta"], {"k": [1, 2]})

    def test_list_runs_filter(self):
        a = self.led.create_run("a")["run_id"]
        b = self.led.create_run("b", parent_run_id=a)["run_id"]
        self.led.transition(b, "running")
        self.assertEqual([r["run_id"] for r in self.led.list_runs(status="running")], [b])
        self.assertEqual([r["run_id"] for r in self.led.list_runs(parent_run_id=a)], [b])
        self.assertEqual(self.led.run_detail(a)["children"], [b])


class TestEventsAppendOnly(LedgerTestCase):
    def test_update_and_delete_rejected(self):
        rid = self.led.create_run("g")["run_id"]
        raw = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE events SET type='x' WHERE run_id=?", (rid,))
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM events WHERE run_id=?", (rid,))
        raw.close()

    def test_events_after_and_types(self):
        rid = self.led.create_run("g")["run_id"]
        last = self.led.events(rid)[-1]["event_id"]
        self.led.emit(rid, "custom", {"x": 1})
        after = self.led.events(rid, after_id=last)
        self.assertEqual([e["type"] for e in after], ["custom"])
        self.assertEqual(after[0]["data"], {"x": 1})
        self.assertEqual(len(self.led.events(rid, types=["custom"])), 1)


class TestStepsToolsArtifactsCheckpoints(LedgerTestCase):
    def test_step_lifecycle_and_tool_calls(self):
        rid = self.led.create_run("g")["run_id"]
        tid = self.led.list_tasks(rid)[0]["task_id"]
        s = self.led.start_step(rid, tid, input={"q": 1})
        self.assertEqual(s["status"], "running")
        self.led.record_tool_call(rid, "bash", {"command": "ls"}, result="x", status="success",
                                  task_id=tid, step_id=s["step_id"])
        self.led.record_tool_call(rid, "fetch_url", {"url": "u"}, result="403", status="failure",
                                  task_id=tid, step_id=s["step_id"])
        s = self.led.finish_step(s["step_id"], "completed", output="ok")
        self.assertEqual(s["output"], "ok")
        self.assertEqual(len(self.led.list_tool_calls(rid)), 2)
        types = [e["type"] for e in self.led.events(rid)]
        self.assertIn("tool_completed", types)
        self.assertIn("tool_failed", types)
        self.assertIn("step_completed", types)
        with self.assertRaises(LedgerError):
            self.led.finish_step(s["step_id"], "weird")

    def test_artifact_checksum(self):
        rid = self.led.create_run("g")["run_id"]
        p = os.path.join(self.tmp, "out.txt")
        with open(p, "w") as f:
            f.write("hello")
        a = self.led.add_artifact(rid, p, description="greeting")
        self.assertEqual(a["sha256"], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        self.assertEqual(a["size"], 5)
        self.assertEqual(a["provenance"]["run_id"], rid)
        self.assertEqual(len(self.led.list_artifacts(rid)), 1)

    def test_checkpoints_ordered(self):
        rid = self.led.create_run("g")["run_id"]
        self.led.create_checkpoint(rid, {"n": 1}, reason="a")
        cp2 = self.led.create_checkpoint(rid, {"n": 2}, reason="b")
        latest = self.led.latest_checkpoint(rid)
        self.assertEqual(latest["checkpoint_id"], cp2["checkpoint_id"])
        self.assertEqual(latest["state"], {"n": 2})
        self.assertEqual([c["seq"] for c in self.led.list_checkpoints(rid)], [0, 1])


class TestOwnership(LedgerTestCase):
    def test_claim_is_exclusive_while_alive(self):
        rid = self.led.create_run("g")["run_id"]
        me = process_owner("a")
        other = process_owner("b")  # same live pid, different engine
        self.assertTrue(self.led.claim(rid, me, lease_seconds=60))
        self.assertTrue(self.led.claim(rid, me, lease_seconds=60))   # re-entrant
        self.assertFalse(self.led.claim(rid, other, lease_seconds=60))
        self.led.release(rid, me)
        self.assertTrue(self.led.claim(rid, other, lease_seconds=60))

    def test_stale_heartbeat_can_be_taken(self):
        rid = self.led.create_run("g")["run_id"]
        self.assertTrue(self.led.claim(rid, process_owner("a"), lease_seconds=60))
        time.sleep(0.05)
        self.assertTrue(self.led.claim(rid, process_owner("b"), lease_seconds=0.01))

    def test_dead_owner_detection(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        import socket
        self.assertTrue(owner_is_dead(f"{socket.gethostname()}:{p.pid}:x"))
        self.assertFalse(owner_is_dead(process_owner("x")))
        self.assertFalse(owner_is_dead("some-other-host:1:x"))  # can't prove -> not dead

    def test_orphaned_runs(self):
        rid = self.led.create_run("g")["run_id"]
        self.led.transition(rid, "running")
        # active, no owner -> orphan
        self.assertEqual([r["run_id"] for r in self.led.orphaned_runs(60)], [rid])
        self.led.claim(rid, process_owner("live"), 60)
        self.assertEqual(self.led.orphaned_runs(60), [])


if __name__ == "__main__":
    unittest.main()

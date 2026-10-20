"""Insert-loss hardening tests for save_chunk (Patches 1-3).

Patch 1 (durability):  synchronous=FULL so a kill/crash cannot drop a committed chunk.
Patch 2 (shutdown):    SIGTERM/SIGINT checkpoint the WAL and close the connection.
Patch 3 (retry):       save_chunk re-verifies the row landed and retries on loss.

Mirrors tests/test_db_write_retry.py: isolate the environment BEFORE importing
the proxy so the module builds its DB in a temp dir.
"""

import os
import sys
import sqlite3
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

_TMP = tempfile.mkdtemp(prefix="mneme_durab_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty_config.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_ASK_REUSABLE"] = "0"
os.environ["MNEME_MEMORY_ONLY"] = "0"
os.environ["MNEME_TOPIC_SWITCH_GRACE"] = "0"
os.environ["MNEME_MAX_PER_TOPIC"] = "0"

import mneme_proxy as mp  # noqa: E402


def _msg(content="hello, this is a durable test chunk"):
    return [{"role": "user", "content": content}]


class TestSynchronousIsFull(unittest.TestCase):
    """Patch 1: the shared connection must use synchronous=FULL (fsync per commit)."""

    def test_synchronous_is_full(self):
        val = mp.db.execute("PRAGMA synchronous").fetchone()[0]
        # SQLite: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        self.assertEqual(val, 2, f"expected synchronous=FULL(2), got {val}")


class TestInsertVerifiedHelper(unittest.TestCase):
    """Patch 3: _insert_chunk_verified retries on insert-loss, returns bool."""

    def setUp(self):
        self.old_db = mp.db
        self.tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(os.path.join(self.tmp, "t.db"), check_same_thread=False)
        conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY)")
        conn.commit()
        mp.db = conn

    def tearDown(self):
        mp.db.close()
        mp.db = self.old_db

    def test_retries_when_insert_lost_once(self):
        calls = {"n": 0}
        def _insert():
            calls["n"] += 1
            if calls["n"] == 1:
                return  # first attempt: row never lands (insert-loss)
            mp.db.execute("INSERT INTO chunks VALUES ('mem_x')")
            mp.db.commit()
        ok = mp._insert_chunk_verified("mem_x", _insert, retries=3, backoff=0.01)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 2, "expected one retry after the lost insert")

    def test_returns_false_when_insert_always_lost(self):
        calls = {"n": 0}
        def _insert():
            calls["n"] += 1  # never inserts
        ok = mp._insert_chunk_verified("mem_x", _insert, retries=2, backoff=0.01)
        self.assertFalse(ok)
        self.assertEqual(calls["n"], 3, "expected 1 initial + 2 retries")


class TestSaveChunkRetriesOnLoss(unittest.TestCase):
    """Patch 3 (integration): save_chunk recovers a lost first insert."""

    def tearDown(self):
        if hasattr(self, "_orig_retry"):
            mp._db_write_retry = self._orig_retry

    def test_save_chunk_retries_when_insert_lost(self):
        self._orig_retry = mp._db_write_retry
        calls = {"n": 0}
        real_db = mp.db
        def fake_retry(fn):
            calls["n"] += 1
            fn()
            if calls["n"] == 1:
                real_db.rollback()  # simulate a lost commit
            else:
                real_db.commit()
        mp._db_write_retry = fake_retry
        try:
            mp.save_chunk("mem_test_retry", "topic", _msg(), None)
        finally:
            mp._db_write_retry = self._orig_retry
        row = real_db.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id='mem_test_retry'").fetchone()
        self.assertIsNotNone(row, "chunk should be present after retry")
        self.assertGreaterEqual(calls["n"], 2, "expected save_chunk to retry the insert")


class TestCleanShutdown(unittest.TestCase):
    """Patch 2: _checkpoint_and_close checkpoints WAL and closes the connection."""

    def test_checkpoint_and_close(self):
        self.assertTrue(hasattr(mp, "_checkpoint_and_close"),
                        "missing _checkpoint_and_close (Patch 2 not applied)")
        tmp = tempfile.mkdtemp()
        old_db = mp.db
        conn = sqlite3.connect(os.path.join(tmp, "t.db"), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO chunks VALUES ('x')")
        conn.commit()
        mp.db = conn
        try:
            mp._checkpoint_and_close()
        finally:
            mp.db = old_db
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        wal = os.path.join(tmp, "t.db-wal")
        self.assertFalse(os.path.exists(wal) and os.path.getsize(wal) > 0,
                         "WAL was not checkpointed by _checkpoint_and_close")


if __name__ == "__main__":
    unittest.main()

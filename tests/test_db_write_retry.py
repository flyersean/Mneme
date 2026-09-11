"""Test that _db_write_retry survives a transient SQLite write lock.

This is the 'shared DB, many proxies' cross-process contention case: process A
holds the write lock briefly, process B's write gets 'database is locked', and
the retry must re-run the write instead of dropping it.
"""

import os
import sys
import time
import sqlite3
import tempfile
import threading
import unittest

# ── Isolated environment BEFORE importing the proxy (mirrors test_tool_loop) ──
_TMP = tempfile.mkdtemp(prefix="mneme_test_")
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

_PROXY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY_DIR)
import mneme_proxy as mp  # noqa: E402


class TestDbWriteRetry(unittest.TestCase):
    def setUp(self):
        # Swap the module's db handle onto a fresh temp DB with a SHORT busy
        # timeout so we can trigger 'locked' fast (the real connection uses 60s).
        self.tmp = tempfile.mkdtemp()
        self.old_db = mp.db
        mp.db = sqlite3.connect(os.path.join(self.tmp, "test.db"),
                                check_same_thread=False, timeout=0.05)
        mp.db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)")
        mp.db.commit()

    def tearDown(self):
        mp.db.close()
        mp.db = self.old_db

    def test_retries_on_locked_and_eventually_succeeds(self):
        # A second connection holds the write lock briefly, then releases it.
        blocker = sqlite3.connect(os.path.join(self.tmp, "test.db"), timeout=0.05,
                                  check_same_thread=False)
        blocker.execute("CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)")
        blocker.commit()
        blocker.execute("BEGIN IMMEDIATE")  # acquire the write lock now

        def _release():
            time.sleep(0.5)
            blocker.commit()  # release the lock

        t = threading.Thread(target=_release)
        t.start()

        def _write():
            mp.db.execute("INSERT INTO t VALUES ('a', 'b')")

        mp._db_write_retry(_write, retries=3, backoff=0.1)
        t.join()
        row = mp.db.execute("SELECT v FROM t WHERE k='a'").fetchone()
        self.assertEqual(row, ("b",))
        blocker.close()

    def test_raises_after_retries_exhausted(self):
        # Hold the lock forever; the retry must exhaust and re-raise.
        blocker = sqlite3.connect(os.path.join(self.tmp, "test.db"), timeout=0.05)
        blocker.execute("CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)")
        blocker.commit()
        blocker.execute("BEGIN IMMEDIATE")

        def _write():
            mp.db.execute("INSERT INTO t VALUES ('a', 'b')")

        with self.assertRaises(sqlite3.OperationalError):
            mp._db_write_retry(_write, retries=1, backoff=0.05)
        blocker.close()


if __name__ == "__main__":
    unittest.main()

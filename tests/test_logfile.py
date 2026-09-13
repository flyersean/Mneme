"""Tests for the proxy's own logging (mneme/logfile.py) — the append-only,
size-capped log the proxy tees stdout/stderr into."""

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme.logfile import LogFile, read_max_entries, setup_logging  # noqa: E402


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


class TestLogFile(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MNEME_MAX_LOG_ENTRIES", None)

    def test_unlimited_grows_unbounded(self):
        p = os.path.join(tempfile.mkdtemp(), "log.txt")
        lf = LogFile(p, None)
        for i in range(50):
            lf.write(f"line {i}\n")
        lf.flush()
        self.assertEqual(len(_read(p)), 50)

    def test_cap_trims_to_newest(self):
        p = os.path.join(tempfile.mkdtemp(), "log.txt")
        lf = LogFile(p, 5)
        for i in range(10):
            lf.write(f"line {i}\n")
        lf.flush()
        self.assertEqual(_read(p), ["line 5", "line 6", "line 7", "line 8", "line 9"])

    def test_cap_keeps_appending_after_trim(self):
        p = os.path.join(tempfile.mkdtemp(), "log.txt")
        lf = LogFile(p, 3)
        for i in range(3):
            lf.write(f"line {i}\n")
        lf.write("extra\n")  # exceeds 3 -> trims to newest 3, then appends
        lf.flush()
        self.assertEqual(_read(p), ["line 1", "line 2", "extra"])

    def test_existing_overlong_file_trimmed_on_open(self):
        p = os.path.join(tempfile.mkdtemp(), "log.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("old1\nold2\nold3\nold4\nold5\n")
        lf = LogFile(p, 3)
        lf.flush()
        self.assertEqual(_read(p), ["old3", "old4", "old5"])

    def test_read_max_entries_parsing(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "200"
        self.assertEqual(read_max_entries(), 200)
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "0"
        self.assertEqual(read_max_entries(), 0)
        os.environ.pop("MNEME_MAX_LOG_ENTRIES", None)
        self.assertIsNone(read_max_entries())  # unset = unlimited
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "abc"
        self.assertIsNone(read_max_entries())  # bad value = unlimited

    def test_setup_logging_off_returns_none(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "0"
        self.assertIsNone(setup_logging(tempfile.mkdtemp()))

    def test_setup_logging_creates_file_and_tees(self):
        d = tempfile.mkdtemp()
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "5"
        _out, _err = sys.stdout, sys.stderr
        try:
            lf = setup_logging(d)
            self.assertIsNotNone(lf)
            self.assertTrue(os.path.exists(os.path.join(d, "proxy.log")))
            sys.stdout.write("tee_test\n")  # routed to the log via the tee
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = _out, _err  # restore for the test runner
        self.assertIn("tee_test", open(os.path.join(d, "proxy.log"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()

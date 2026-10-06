"""Tests for the proxy's own logging (mneme/logfile.py) — the append-only,
size-capped log the proxy tees stdout/stderr into."""

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme.logfile import LogFile, capped_append, read_max_entries, setup_logging  # noqa: E402


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
        self.assertIsNone(setup_logging(os.path.join(tempfile.mkdtemp(), "proxy.log")))

    def test_setup_logging_creates_file_and_tees(self):
        d = tempfile.mkdtemp()
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "5"
        _out, _err = sys.stdout, sys.stderr
        try:
            lf = setup_logging(os.path.join(d, "proxy.log"))
            self.assertIsNotNone(lf)
            self.assertTrue(os.path.exists(os.path.join(d, "proxy.log")))
            sys.stdout.write("tee_test\n")  # routed to the log via the tee
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = _out, _err  # restore for the test runner
        self.assertIn("tee_test", open(os.path.join(d, "proxy.log"), encoding="utf-8").read())


class TestCappedAppend(unittest.TestCase):
    """capped_append is the shared cap for errors.log / thinking.log / extension
    logs, so NONE of the proxy's logs grow without bound."""

    def setUp(self):
        os.environ.pop("MNEME_MAX_LOG_ENTRIES", None)

    def tearDown(self):
        os.environ.pop("MNEME_MAX_LOG_ENTRIES", None)

    def test_caps_to_newest(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "5"
        p = os.path.join(tempfile.mkdtemp(), "e.log")
        for i in range(20):
            capped_append(p, f"line {i}")
        self.assertEqual(_read(p), [f"line {i}" for i in range(15, 20)])

    def test_off_writes_nothing(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "0"
        p = os.path.join(tempfile.mkdtemp(), "e.log")
        capped_append(p, "should not be written")
        self.assertFalse(os.path.exists(p))

    def test_unset_is_unlimited(self):
        p = os.path.join(tempfile.mkdtemp(), "e.log")
        for i in range(300):
            capped_append(p, f"line {i}")
        self.assertEqual(len(_read(p)), 300)

    def test_preexisting_overlong_file_trimmed_on_first_append(self):
        # mirrors the real case: a 62k-line thinking.log collapsing to 200 the
        # first time record() runs in a fresh process.
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "200"
        p = os.path.join(tempfile.mkdtemp(), "big.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(f"old{i}\n" for i in range(62618)))
        capped_append(p, "newest")
        lines = _read(p)
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[-1], "newest")

    def test_appends_without_trailing_newline_for_single_line(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "10"
        p = os.path.join(tempfile.mkdtemp(), "e.log")
        capped_append(p, "no newline here")
        capped_append(p, "second")
        self.assertEqual(_read(p), ["no newline here", "second"])

    def test_creates_parent_dirs(self):
        os.environ["MNEME_MAX_LOG_ENTRIES"] = "10"
        p = os.path.join(tempfile.mkdtemp(), "nested", "deep", "e.log")
        capped_append(p, "hello")
        self.assertTrue(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()

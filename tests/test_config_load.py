import os
import sys
import tempfile
import unittest
from unittest import mock

# Isolate from any real config on the host before importing the proxy (the proxy
# runs load_config() at import time).
_TMP = tempfile.mkdtemp(prefix="mneme_cfgtest_")
os.environ["MNEME_CHUNK_DIR"] = _TMP

_PROXY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "proxy")
sys.path.insert(0, _PROXY_DIR)
import mneme_proxy as mp  # noqa: E402


class TestConfigLoadRetry(unittest.TestCase):
    """The setup wizard writes mneme.yaml and launches the proxy back-to-back, so
    at import time the file can be absent or half-written. load_config() must
    retry briefly and recover instead of running on stale/partial config (which
    left reasoning ON and made thinking models runaway-timeout)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("MNEME_REASONING_ENABLED",)}
        os.environ.pop("MNEME_REASONING_ENABLED", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_load_config_waits_for_late_config(self):
        tmpdir = tempfile.mkdtemp()
        cfg = os.path.join(tmpdir, "mneme.yaml")
        with open(cfg, "w") as f:
            f.write("sampling:\n  reasoning_enabled: 0\n")
        state = {"n": 0}

        def fake_find():
            state["n"] += 1
            return cfg if state["n"] >= 3 else None  # config not on disk yet

        with mock.patch.object(mp, "_find_config_path", side_effect=fake_find):
            mp.load_config()
        self.assertEqual(state["n"], 3)
        self.assertEqual(os.environ.get("MNEME_REASONING_ENABLED"), "0")

    def test_load_config_retries_on_partial_write(self):
        tmpdir = tempfile.mkdtemp()
        cfg = os.path.join(tmpdir, "mneme.yaml")
        state = {"n": 0}

        def fake_parse(path):
            state["n"] += 1
            if state["n"] < 2:
                raise Exception("truncated yaml")  # half-written file
            return {"sampling": {"reasoning_enabled": 0}}

        with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
             mock.patch.object(mp, "_parse_config_file", side_effect=fake_parse):
            mp.load_config()
        self.assertEqual(state["n"], 2)
        self.assertEqual(os.environ.get("MNEME_REASONING_ENABLED"), "0")


    def test_load_config_warns_on_port_mismatch(self):
        """A wrong $MNEME_CHUNK_DIR (pointing at another instance's dir) must be
        flagged loudly, not silently misdirect the log into that instance's
        proxy.log (the "everything lands in 8080's log" symptom)."""
        import io
        from contextlib import redirect_stdout
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        saved = {k: os.environ.get(k) for k in ("MNEME_PORT", "MNEME_CHUNK_DIR")}
        os.environ["MNEME_PORT"] = "8082"
        os.environ["MNEME_CHUNK_DIR"] = "/workspace/mneme_chunks/instances/8080"
        data = {"storage": {"port": 8080, "chunk_dir": "/workspace/mneme_chunks/instances/8080"}}
        try:
            buf = io.StringIO()
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data), \
                 redirect_stdout(buf):
                mp.load_config()
            out = buf.getvalue()
            self.assertIn("PORT MISMATCH", out)
            self.assertIn("8082", out)
            self.assertIn("8080", out)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_load_config_no_warning_when_consistent(self):
        import io
        from contextlib import redirect_stdout
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        saved = {k: os.environ.get(k) for k in ("MNEME_PORT", "MNEME_CHUNK_DIR")}
        os.environ["MNEME_PORT"] = "8082"
        os.environ["MNEME_CHUNK_DIR"] = "/workspace/mneme_chunks/instances/8082"
        data = {"storage": {"port": 8082, "chunk_dir": "/workspace/mneme_chunks/instances/8082"}}
        try:
            buf = io.StringIO()
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data), \
                 redirect_stdout(buf):
                mp.load_config()
            self.assertNotIn("MISMATCH", buf.getvalue())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()

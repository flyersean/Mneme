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

_MISSING = object()


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


    def test_chunk_large_messages_is_context_aware(self):
        """Only a message that EXCEEDS the context input budget is chunked. A
        message that fits passes through whole (regression for the "large input
        gets chunked away and the model grinds search -> empty" bug)."""
        small = {"role": "user", "content": "a small message"}
        big = {"role": "user", "content": "x" * 8000}  # ~2000 tokens by chars/4
        with mock.patch.object(mp, "_context_input_budget", return_value=100000):
            out = mp._chunk_large_messages([small, big])
        self.assertEqual(out[0]["content"], small["content"])
        self.assertEqual(out[1]["content"], big["content"])
        with mock.patch.object(mp, "_context_input_budget", return_value=100), \
             mock.patch.object(mp, "save_chunk"):
            out2 = mp._chunk_large_messages([{"role": "user", "content": "x" * 8000}])
        self.assertIn("AUTO-CHUNKED", out2[0]["content"])


    def test_load_config_with_model_template_no_nameerror(self):
        """Regression: USER_TEMPLATES_PATH was defined BELOW load_config()'s call
        site, so a first import with a model template selected crashed with a
        NameError at startup. load_config() must resolve the user-template path
        from the parsed config instead of the not-yet-defined module global."""
        import io
        from contextlib import redirect_stdout
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        data = {"model_template": "gemma4-repeat",
                "storage": {"db_path": os.path.join(tempfile.mkdtemp(), "mneme.db")},
                "sampling": {}}
        saved = mp.__dict__.get("USER_TEMPLATES_PATH", _MISSING)
        saved_db = os.environ.get("MNEME_DB_PATH")
        mp.__dict__.pop("USER_TEMPLATES_PATH", None)  # simulate pre-definition state
        try:
            buf = io.StringIO()
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data), \
                 redirect_stdout(buf):
                mp.load_config()  # must not raise NameError
            self.assertIn("[TEMPLATE] applied", buf.getvalue())
        finally:
            if saved is _MISSING:
                mp.__dict__.pop("USER_TEMPLATES_PATH", None)
            else:
                mp.USER_TEMPLATES_PATH = saved
            if saved_db is None:
                os.environ.pop("MNEME_DB_PATH", None)
            else:
                os.environ["MNEME_DB_PATH"] = saved_db


    def test_load_config_sets_max_server_rounds_from_config(self):
        """caps.max_server_rounds must flow to MNEME_MAX_SERVER_ROUNDS so the
        request-time read in process_chat picks it up (the override lives in
        overcome.py, which is read at import time — before the config loads)."""
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        data = {"caps": {"max_server_rounds": 60}}
        saved = os.environ.get("MNEME_MAX_SERVER_ROUNDS")
        try:
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data):
                mp.load_config()
            self.assertEqual(os.environ.get("MNEME_MAX_SERVER_ROUNDS"), "60")
        finally:
            if saved is None:
                os.environ.pop("MNEME_MAX_SERVER_ROUNDS", None)
            else:
                os.environ["MNEME_MAX_SERVER_ROUNDS"] = saved

    def test_load_config_sets_model_from_top_level_key(self):
        """The top-level `model:` key must flow to MNEME_MODEL (and embed/label to
        their env vars) when the environment is clean — so the config, not a stale
        start-script export, is authoritative for model identity."""
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        data = {"model": "new-model", "embed_model": "new-embed", "label_model": "new-label"}
        saved = {k: os.environ.get(k) for k in ("MNEME_MODEL", "EMBED_MODEL", "LABEL_MODEL")}
        for k in saved:
            os.environ.pop(k, None)  # start script now unsets these; simulate it
        try:
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data):
                mp.load_config()
            self.assertEqual(os.environ.get("MNEME_MODEL"), "new-model")
            self.assertEqual(os.environ.get("EMBED_MODEL"), "new-embed")
            self.assertEqual(os.environ.get("LABEL_MODEL"), "new-label")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ollama_falls_back_to_provider_model(self):
        """Older configs stored the model under providers.openrouter.model (the
        wizard wrote it there for both backends). For Ollama, when the top-level
        `model:` key is absent, the model must still resolve from that provider
        block instead of the built-in default."""
        cfg = os.path.join(tempfile.mkdtemp(), "mneme.yaml")
        data = {"providers": {"openrouter": {"model": "old-model"}}}
        saved = {k: os.environ.get(k) for k in ("MNEME_MODEL", "MNEME_BACKEND")}
        os.environ.pop("MNEME_MODEL", None)
        os.environ["MNEME_BACKEND"] = "ollama"
        try:
            with mock.patch.object(mp, "_find_config_path", return_value=cfg), \
                 mock.patch.object(mp, "_parse_config_file", return_value=data):
                mp.load_config()
            self.assertEqual(os.environ.get("MNEME_MODEL"), "old-model")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()

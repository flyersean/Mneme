"""Scope enforcement: the native tools must refuse writes outside the model's
writable area (TOOLS_DIR + MODEL_SCOPE + RUNS_ROOT), and read_file must be
limited to BROWSER_ROOT. This guards the Claude Code "allowWrite" gap — a model
must not be able to write to the user's home or anywhere outside its scope."""

import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import mneme.tools as mntools  # noqa: E402


class TestScopeEnforcement(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "MNEME_TOOLS_DIR", "MNEME_MODEL_SCOPE", "MNEME_BROWSER_ROOT",
            "MNEME_RUNS_DIR", "MNEME_CHUNK_DIR")}
        self.tmp = tempfile.mkdtemp()
        self.tools = os.path.join(self.tmp, "tools")
        self.scope = os.path.join(self.tmp, "output")
        self.runs = os.path.join(self.tmp, "runs")
        self.home = os.path.join(self.tmp, "home")   # browser root (shared, read-only)
        os.makedirs(self.home, exist_ok=True)
        os.environ["MNEME_TOOLS_DIR"] = self.tools
        os.environ["MNEME_MODEL_SCOPE"] = self.scope
        os.environ["MNEME_RUNS_DIR"] = self.runs
        os.environ["MNEME_CHUNK_DIR"] = self.tmp
        mntools.reload_config()
        mntools.set_scope(model_scope=self.scope, browser_root=self.home)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mntools.reload_config()

    def test_write_blocked_outside_scope(self):
        target = os.path.join(self.home, "leak.txt")   # shared file, read-only
        res = mntools.execute_native_tool("write", {"file_path": target, "content": "BAD"})
        self.assertIn("blocked", res)
        self.assertFalse(os.path.exists(target))

    def test_write_allowed_inside_scope(self):
        target = os.path.join(self.scope, "ok.txt")
        mntools.execute_native_tool("write", {"file_path": target, "content": "ok"})
        self.assertTrue(os.path.exists(target))

    def test_write_allowed_inside_tools_dir(self):
        target = os.path.join(self.tools, "scratch.txt")
        mntools.execute_native_tool("write", {"file_path": target, "content": "ok"})
        self.assertTrue(os.path.exists(target))

    def test_read_file_blocked_outside_browser_root(self):
        res = mntools.execute_readonly_tool("read_file", {"path": "/etc/hostname"})
        self.assertIn("blocked", res)

    def test_read_file_allowed_inside_browser_root(self):
        f = os.path.join(self.home, "shared.txt")
        with open(f, "w") as fh:
            fh.write("shared content")
        res = mntools.execute_readonly_tool("read_file", {"path": f})
        self.assertIn("shared content", res)

    def test_writable_roots_include_all_three(self):
        roots = mntools._writable_roots()
        for r in (self.tools, self.scope, self.runs):
            self.assertIn(os.path.realpath(r), [os.path.realpath(x) for x in roots])

    def test_bash_uses_bwrap_when_available(self):
        with mock.patch.object(mntools.shutil, "which", return_value="/usr/bin/bwrap"), \
             mock.patch.object(mntools.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
            mntools.execute_native_tool("bash", {"command": "pwd"})
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[0], "/usr/bin/bwrap")
            self.assertIn("--ro-bind", cmd)
            self.assertIn("--bind", cmd)

    def test_bash_falls_back_without_bwrap(self):
        with mock.patch.object(mntools.shutil, "which", return_value=None), \
             mock.patch.object(mntools.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
            mntools.execute_native_tool("bash", {"command": "pwd"})
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[:2], ["bash", "-c"])


if __name__ == "__main__":
    unittest.main()

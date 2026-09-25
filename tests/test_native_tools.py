"""bash and write must resolve relative paths against the SAME base — the tools
directory — so a file the model writes is reachable by a later bash call. Guards
against the two tools drifting apart (bash used to run from ~ while write
resolved relative paths into the tools dir), which small models can't cope with."""

import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import mneme.tools as mntools  # noqa: E402


class TestNativeToolPathConsistency(unittest.TestCase):
    def setUp(self):
        self._saved_dir = os.environ.get("MNEME_TOOLS_DIR")
        self.tmp = tempfile.mkdtemp()
        os.environ["MNEME_TOOLS_DIR"] = self.tmp
        mntools.reload_config()

    def tearDown(self):
        if self._saved_dir is None:
            os.environ.pop("MNEME_TOOLS_DIR", None)
        else:
            os.environ["MNEME_TOOLS_DIR"] = self._saved_dir
        mntools.reload_config()

    def test_write_relative_lands_in_tools_dir(self):
        mntools.execute_native_tool("write", {"file_path": "foo.txt", "content": "hi"})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "foo.txt")))

    def test_write_absolute_lands_exactly_there(self):
        target = os.path.join(self.tmp, "sub", "abs.txt")
        mntools.execute_native_tool("write", {"file_path": target, "content": "hi"})
        self.assertTrue(os.path.exists(target))

    def test_bash_cwd_is_tools_dir(self):
        with mock.patch.object(mntools.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", stderr="", returncode=0)
            mntools.execute_native_tool("bash", {"command": "pwd"})
            self.assertEqual(os.path.abspath(run.call_args.kwargs["cwd"]),
                             os.path.abspath(self.tmp))

    def test_descriptions_mention_shared_tools_dir(self):
        bash_desc = mntools.NATIVE_BASH_TOOL["function"]["description"]
        write_desc = mntools.NATIVE_WRITE_TOOL["function"]["description"]
        self.assertIn("tools directory", bash_desc.lower())
        self.assertIn("tools directory", write_desc.lower())


if __name__ == "__main__":
    unittest.main()

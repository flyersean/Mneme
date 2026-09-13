"""Tests for the MCP client + manager (hot add/remove) and its wiring into
assemble_tools. Spawns a real stdio MCP server (tests/fixtures/mcp_fixture_server.py)
so the connect → list → call path is exercised against the actual protocol."""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme.mcp_client import get_manager  # noqa: E402

FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "mcp_fixture_server.py")
STDIO = {"command": sys.executable, "args": [FIXTURE]}


class TestMCPClient(unittest.TestCase):
    def setUp(self):
        self.mgr = get_manager()
        self.mgr.reconcile([])  # start clean

    def tearDown(self):
        self.mgr.reconcile([])

    def test_connect_list_call_remove(self):
        srv = self.mgr.add("fixture", dict(STDIO))
        self.assertTrue(srv.wait_ready(20), srv.error)
        names = {t["function"]["name"] for t in srv.tools}
        self.assertEqual(names, {"echo", "add"})
        self.assertEqual(self.mgr.call_tool("echo", {"text": "hi"}), "hi")
        self.assertEqual(self.mgr.call_tool("add", {"a": 2, "b": 3}), "5")
        self.assertEqual(self.mgr.call_tool("nope", {}), "[mcp] unknown tool: nope")
        self.mgr.remove("fixture")
        self.assertNotIn("fixture", self.mgr.names())

    def test_status_shape(self):
        self.mgr.add("fixture", dict(STDIO)).wait_ready(20)
        st = self.mgr.status()["fixture"]
        self.assertTrue(st["ready"])
        self.assertIsNone(st["error"])
        self.assertEqual(st["tools"], ["echo", "add"])

    def test_reconcile_leaves_unchanged_server_running(self):
        self.mgr.reconcile([{"name": "a", **STDIO}])
        a1 = self.mgr._servers["a"]
        a1.wait_ready(20)
        # reconcile with the same config -> same object (not torn down + rebuilt)
        self.mgr.reconcile([{"name": "a", **STDIO}])
        self.assertIs(self.mgr._servers["a"], a1)
        # reconcile with a changed config -> replaced
        self.mgr.reconcile([{"name": "a", "command": sys.executable, "args": [FIXTURE], "env": {"X": "1"}}])
        self.assertIsNot(self.mgr._servers["a"], a1)
        # reconcile with nothing -> removed
        self.mgr.reconcile([])
        self.assertEqual(self.mgr.names(), [])

    def test_assemble_tools_includes_mcp_tools(self):
        import mneme.tools as mntools
        self.mgr.add("fx", dict(STDIO)).wait_ready(20)
        tools = mntools.assemble_tools(None)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("echo", names)
        self.assertIn("add", names)

    def test_failed_server_degrades_gracefully(self):
        srv = self.mgr.add("bad", {"command": sys.executable, "args": ["-c", "import sys; sys.exit(1)"]})
        srv.wait_ready(10)
        self.assertFalse(srv.ready)
        self.assertIsNotNone(srv.error)
        # a broken server contributes no tools
        self.assertEqual(self.mgr.tool_names() & {"echo"}, set())


if __name__ == "__main__":
    unittest.main()

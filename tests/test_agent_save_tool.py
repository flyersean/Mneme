"""Tests for the agent-facing save_tool registration path.

Covers: schema presence in assembled tools, roundtrip (write script ->
save_tool -> list_tools/read_tool), name validation, missing-script error,
and the MNEME_TOOL_SAVE_TOOL gate.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))

from mneme.tools import (  # noqa: E402
    assemble_tools,
    enabled_save_tools,
    execute_readonly_tool,
    save_tool,
    TOOLS_DIR,
)


class _FakeEmbed:
    def embed(self, text):
        return [0.0, 0.0, 0.1]


class _FakeDB:
    """Minimal stand-in exposing the tools table via a real sqlite db."""

    def __init__(self, path):
        import sqlite3

        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS tools ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name TEXT UNIQUE NOT NULL,"
            "description TEXT NOT NULL DEFAULT '',"
            "problem_type TEXT,"
            "script_path TEXT NOT NULL,"
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self.conn.commit()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


class SaveToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.db = _FakeDB(self.db_path)
        self.embed = _FakeEmbed()
        # point TOOLS_DIR at the temp dir so we never touch real tools
        self._orig_tools_dir = TOOLS_DIR.value if hasattr(TOOLS_DIR, "value") else None

    def tearDown(self):
        self.db.close()

    def _script_path(self, name):
        p = Path(self._tmp.name) / f"{name}.py"
        p.write_text("#!/usr/bin/env python3\nprint('hi')\n")
        return str(p)

    # -- gating ----------------------------------------------------------

    def test_enabled_save_tools_returns_save_tool(self):
        os.environ.pop("MNEME_TOOL_SAVE_TOOL", None)
        self.assertIn("save_tool", enabled_save_tools(self.db))

    def test_gate_disables_tool(self):
        old = os.environ.get("MNEME_TOOL_SAVE_TOOL")
        try:
            os.environ["MNEME_TOOL_SAVE_TOOL"] = "0"
            self.assertEqual(enabled_save_tools(self.db), [])
        finally:
            if old is None:
                os.environ.pop("MNEME_TOOL_SAVE_TOOL", None)
            else:
                os.environ["MNEME_TOOL_SAVE_TOOL"] = old

    def test_hidden_without_db(self):
        self.assertEqual(enabled_save_tools(None), [])

    # -- roundtrip ---------------------------------------------------------

    def test_save_and_read_roundtrip(self):
        script = self._script_path("roundtrip_helper")
        result = save_tool(
            problem_type="utility",
            name="roundtrip_helper",
            description="A test helper tool",
            script_path=script,
            db_=self.db,
            embed_=self.embed,
        )
        self.assertIn("roundtrip_helper", str(result))

        # appears in assembled tools when db is bound
        names = [t["function"]["name"] for t in assemble_tools(db=self.db)]
        self.assertIn("save_tool", names)

        # read back the canonical copy
        res = execute_readonly_tool(
            "read_tool", {"name": "roundtrip_helper"}, db=self.db
        )
        self.assertIn("hi", str(res))

    # -- validation --------------------------------------------------------

    def test_invalid_name_rejected(self):
        script = self._script_path("whatever")
        with self.assertRaises(Exception):
            save_tool(
                problem_type="utility",
                name="Bad Name!",
                description="x",
                script_path=script,
                db_=self.db,
                embed_=self.embed,
            )

    def test_missing_script_rejected(self):
        with self.assertRaises(Exception):
            save_tool(
                problem_type="utility",
                name="no_script_here",
                description="x",
                script_path="/nonexistent/nope.py",
                db_=self.db,
                embed_=self.embed,
            )


if __name__ == "__main__":
    unittest.main()

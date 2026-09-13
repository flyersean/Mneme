"""Test the /mcp/servers admin endpoints against a live Flask app (test_client).

Verifies the "add tools to a running proxy" requirement: POST adds an MCP server
and its tools appear without restarting; DELETE removes it; GET lists status."""

import os
import sys
import tempfile
import unittest

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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import mneme_proxy as mp  # noqa: E402

FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "mcp_fixture_server.py")


class TestMCPEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = mp.app.test_client()
        mp.mntools.get_manager().reconcile([])  # start clean

    def test_list_empty(self):
        r = self.client.get("/mcp/servers")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["servers"], {})

    def test_add_list_remove(self):
        # add (stdio) — blocks until connected (up to 20s)
        r = self.client.post("/mcp/servers", json={
            "name": "fixture", "command": sys.executable, "args": [FIXTURE],
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["server"]["ready"], body["server"])
        self.assertEqual(body["server"]["tools"], ["echo", "add"])

        # the tools are now in assemble_tools (surfaced to the model next turn)
        tools = mp.mntools.assemble_tools(None)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("echo", names)

        # list shows it
        self.assertIn("fixture", self.client.get("/mcp/servers").get_json()["servers"])

        # delete
        r = self.client.delete("/mcp/servers/fixture")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/mcp/servers").get_json()["servers"], {})

    def test_add_rejects_bad_config(self):
        r = self.client.post("/mcp/servers", json={"name": "x"})  # no command/url
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/mcp/servers", json={"command": "true"})  # no name
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

"""The overview + Ollama control-panel routes must be registered and serve.
Guards the control-plane rework: a single /overview hub listing every proxy
instance, plus an /ollama panel backed by the local Ollama API."""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_overview_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import mneme_proxy as mp  # noqa: E402


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestOverviewRoutes(unittest.TestCase):
    def _rules(self):
        return {r.rule for r in mp.app.url_map.iter_rules()}

    def test_overview_and_ollama_routes_registered(self):
        rules = self._rules()
        for path in ("/overview", "/overview/instances", "/overview/start",
                     "/overview/stop", "/ollama", "/ollama/models", "/ollama/ps",
                     "/ollama/pull", "/ollama/rm", "/ollama/load", "/ollama/unload"):
            self.assertIn(path, rules, f"missing route {path}")

    def test_overview_instances_returns_list(self):
        r = mp.app.test_client().get("/overview/instances")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("instances", data)
        self.assertIsInstance(data["instances"], list)
        # the current proxy must always be present
        self.assertTrue(any(i.get("self") for i in data["instances"]))

    def test_overview_ui_serves_html(self):
        r = mp.app.test_client().get("/overview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("Content-Type", ""))

    def test_ollama_ui_serves_html(self):
        r = mp.app.test_client().get("/ollama")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("Content-Type", ""))

    def test_overview_config_routes_registered(self):
        rules = self._rules()
        self.assertIn("/overview/config/<int:port>", rules)

    def test_overview_config_get_missing_returns_404(self):
        r = mp.app.test_client().get("/overview/config/9999")
        self.assertEqual(r.status_code, 404)

    def test_overview_config_save_rejects_invalid_yaml(self):
        r = mp.app.test_client().post("/overview/config/9999",
                                      json={"content": "model: [unclosed"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("invalid YAML", r.get_json().get("error", ""))

    def test_overview_config_save_rejects_missing_content(self):
        r = mp.app.test_client().post("/overview/config/9999", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing content", r.get_json().get("error", ""))


if __name__ == "__main__":
    unittest.main()

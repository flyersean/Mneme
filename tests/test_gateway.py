"""The gateway (reverse proxy) must serve its dashboard, list instances with a
base prefix, and inject the /<port>/ shim into proxied HTML so an instance's
root-relative links stay under its prefix."""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_gateway_")
os.environ["MNEME_CHUNK_DIR"] = _TMP
os.environ["MNEME_GATEWAY_PORT"] = "18001"
os.environ["MNEME_GATEWAY_HOST"] = "127.0.0.1"
os.environ.pop("MNEME_GATEWAY_TOKEN", None)

# one fake instance dir
os.makedirs(os.path.join(_TMP, "instances", "8080"), exist_ok=True)
with open(os.path.join(_TMP, "instances", "8080", "mneme.yaml"), "w") as f:
    f.write("model: fake-model\nbackend:\n  type: ollama\n")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))
import gateway as gw  # noqa: E402


@unittest.skipUnless(gw.app is not None, "flask not installed")
class TestGatewayRoutes(unittest.TestCase):
    def _rules(self):
        return {r.rule for r in gw.app.url_map.iter_rules()}

    def test_routes_registered(self):
        rules = self._rules()
        for path in ("/", "/health", "/gateway/instances", "/overview/start",
                     "/overview/stop", "/<int:port>/<path:path>"):
            self.assertIn(path, rules, f"missing route {path}")

    def test_dashboard_serves_html(self):
        r = gw.app.test_client().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.content_type)

    def test_gateway_instances_has_base(self):
        r = gw.app.test_client().get("/gateway/instances")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("instances", data)
        self.assertTrue(all("base" in i and i["base"] == "/" + str(i["port"])
                            for i in data["instances"]))


class TestGatewayShim(unittest.TestCase):
    def test_inject_shim_into_head(self):
        html = b"<html><head><title>x</title></head><body></body></html>"
        out = gw._inject_shim(html, 8080, "http://localhost:8000/")
        self.assertIn(b"prefix='/8080'", out)
        self.assertIn(b"<script>", out)

    def test_inject_shim_no_head(self):
        out = gw._inject_shim(b"<div>hi</div>", 8080, "http://localhost:8000/")
        self.assertIn(b"<script>", out)
        self.assertIn("← Overview".encode(), out)

    def test_shim_patches_fetch(self):
        shim = gw._shim_script(8080)
        self.assertIn("window.fetch", shim)
        self.assertIn("prefix='/8080'", shim)

    def test_shim_decorates_overview_and_port(self):
        shim = gw._shim_script(8080, "http://localhost:8000/")
        # the shim must add an "← Overview" link (pointing at the full gateway
        # URL, not the /<port>/ prefix) and a "port" badge, at runtime via JS
        self.assertIn("← Overview", shim)
        self.assertIn("http://localhost:8000/", shim)
        self.assertIn("· port", shim)
        self.assertIn("querySelector('.mneme-nav')", shim)


if __name__ == "__main__":
    unittest.main()

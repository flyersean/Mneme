"""Proxy multi-user auth: the mneme_proxy instance (started by ``mneme``)
enforces the same mneme.auth login as the gateway — off until a user is added,
with /health and CORS preflight (OPTIONS) staying open."""

import os
import sys
import base64
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_proxy_auth_")
os.environ["MNEME_CHUNK_DIR"] = os.path.join(_TMP, "chunks")
os.environ["MNEME_CONFIG"] = os.path.join(_TMP, "empty.json")
with open(os.environ["MNEME_CONFIG"], "w") as f:
    f.write("{}")
os.environ["MNEME_BACKEND"] = "ollama"
os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
os.environ["MNEME_MODEL"] = "test-model"
os.environ["EMBED_MODEL"] = "test-embed"
os.environ["LABEL_MODEL"] = "test-label"
os.environ["MNEME_GATEWAY_CONFIG_DIR"] = os.path.join(_TMP, "gwconfig")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

import mneme_proxy as mp  # noqa: E402
from mneme.auth import AuthStore, add_user  # noqa: E402


def _b64(s):
    return base64.b64encode(s.encode()).decode()


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyAuthOn(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        self._orig = mp.AUTH
        mp.AUTH = AuthStore(self.uf)

    def tearDown(self):
        mp.AUTH = self._orig

    def _c(self):
        return mp.app.test_client()

    def test_protected_when_user_exists(self):
        r = self._c().get("/")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get("WWW-Authenticate"), 'Basic realm="mneme"')
        self.assertEqual(self._c().get("/chat").status_code, 401)

    def test_health_stays_open(self):
        self.assertEqual(self._c().get("/health").status_code, 200)

    def test_options_preflight_stays_open(self):
        self.assertEqual(self._c().open("/", method="OPTIONS").status_code, 200)

    def test_bearer_token(self):
        r = self._c().get("/", headers={"Authorization": "Bearer tok-a"})
        self.assertEqual(r.status_code, 200)

    def test_basic_auth(self):
        r = self._c().get("/", headers={"Authorization": "Basic " + _b64("alice:hunter2")})
        self.assertEqual(r.status_code, 200)

    def test_query_token(self):
        self.assertEqual(self._c().get("/?token=tok-a").status_code, 200)

    def test_wrong_credentials_401(self):
        self.assertEqual(self._c().get("/", headers={"Authorization": "Bearer nope"}).status_code, 401)


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyAuthOff(unittest.TestCase):
    def test_open_by_default(self):
        self.assertEqual(mp.app.test_client().get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()

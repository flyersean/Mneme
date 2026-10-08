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


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyLoginFlow(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        self._orig = mp.AUTH
        mp.AUTH = AuthStore(self.uf)

    def tearDown(self):
        mp.AUTH = self._orig

    def _c(self):
        return mp.app.test_client()

    def _login_cookie(self):
        r = self._c().post("/login", data={"username": "alice", "password": "hunter2", "next": "/chat"})
        assert r.status_code == 302, r.status_code
        sc = r.headers.get("Set-Cookie", "")
        assert "mneme_session=" in sc
        return sc.split("mneme_session=")[1].split(";")[0]

    def test_login_sets_cookie_and_redirects(self):
        r = self._c().post("/login", data={"username": "alice", "password": "hunter2", "next": "/chat"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/chat", r.headers.get("Location", ""))
        self.assertIn("mneme_session=", r.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", r.headers.get("Set-Cookie", ""))

    def test_login_wrong_password_shows_error(self):
        r = self._c().post("/login", data={"username": "alice", "password": "wrong", "next": "/"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Invalid", r.data)

    def test_session_cookie_authenticates(self):
        ck = self._login_cookie()
        c = self._c()
        c.set_cookie("mneme_session", ck)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)

    def test_browser_redirects_to_login(self):
        r = self._c().get("/", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    def test_api_returns_401_not_redirect(self):
        r = self._c().get("/", headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 401)


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyFirstRun(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")  # absent → no users
        self._orig = mp.AUTH
        mp.AUTH = AuthStore(self.uf)

    def tearDown(self):
        mp.AUTH = self._orig

    def _c(self):
        return mp.app.test_client()

    def test_browser_redirects_to_create_account(self):
        r = self._c().get("/", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/create-account", r.headers.get("Location", ""))

    def test_api_stays_open_when_no_users(self):
        self.assertEqual(self._c().get("/", headers={"Accept": "application/json"}).status_code, 200)

    def test_create_account(self):
        r = self._c().post("/create-account", data={"username": "bob", "password": "secret1", "confirm": "secret1"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("mneme_session=", r.headers.get("Set-Cookie", ""))
        self.assertTrue(mp.AUTH.has_user("bob"))
        # auth is now on — an unauthenticated API call is refused
        self.assertEqual(self._c().get("/", headers={"Accept": "application/json"}).status_code, 401)

    def test_create_account_password_mismatch(self):
        r = self._c().post("/create-account", data={"username": "bob", "password": "secret1", "confirm": "different"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"match", r.data)
        self.assertFalse(mp.AUTH.has_user("bob"))

    def test_login_redirects_to_create_when_no_users(self):
        r = self._c().get("/login")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/create-account", r.headers.get("Location", ""))


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyTokensPage(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        self._orig = mp.AUTH
        mp.AUTH = AuthStore(self.uf)

    def tearDown(self):
        mp.AUTH = self._orig

    def _login(self):
        c = mp.app.test_client()
        r = c.post("/login", data={"username": "alice", "password": "hunter2", "next": "/"})
        assert r.status_code == 302, r.status_code
        return c

    def test_tokens_requires_login(self):
        r = mp.app.test_client().get("/tokens", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    def test_tokens_lists_own_tokens(self):
        r = self._login().get("/tokens")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"tok-a", r.data)

    def test_tokens_generate(self):
        c = self._login()
        r = c.post("/tokens", data={"action": "generate", "label": "telegram-gateway"})
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(mp.AUTH.get_user("alice")["tokens"]), 2)

    def test_tokens_revoke(self):
        c = self._login()
        r = c.post("/tokens", data={"action": "revoke", "token": "tok-a"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(mp.AUTH.check_token("tok-a"))


@unittest.skipUnless(getattr(mp, "FLASK_OK", False), "flask not installed")
class TestProxyUsersPage(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")
        add_user(self.uf, "alice", "hunter2", token="tok-a", admin=True)
        add_user(self.uf, "bob", "s3cret", token="tok-b", admin=False)
        self._orig = mp.AUTH
        mp.AUTH = AuthStore(self.uf)

    def tearDown(self):
        mp.AUTH = self._orig

    def _login(self, username, password):
        c = mp.app.test_client()
        r = c.post("/login", data={"username": username, "password": password, "next": "/"})
        assert r.status_code == 302, r.status_code
        return c

    def test_users_admin_only(self):
        self.assertEqual(self._login("bob", "s3cret").get("/users").status_code, 403)
        self.assertEqual(self._login("alice", "hunter2").get("/users").status_code, 200)

    def test_users_add_account(self):
        c = self._login("alice", "hunter2")
        r = c.post("/users", data={"action": "add", "username": "carol", "password": "secret1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(mp.AUTH.has_user("carol"))
        self.assertFalse(mp.AUTH.is_admin("carol"))

    def test_users_remove_account_cascades_tokens(self):
        c = self._login("alice", "hunter2")
        r = c.post("/users", data={"action": "remove", "username": "bob"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(mp.AUTH.has_user("bob"))
        self.assertIsNone(mp.AUTH.check_token("tok-b"))

    def test_users_reset_password(self):
        c = self._login("alice", "hunter2")
        r = c.post("/users", data={"action": "resetpw", "username": "bob", "password": "newpw1"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(mp.AUTH.check_password("bob", "newpw1"))

    def test_users_cannot_remove_admin(self):
        c = self._login("alice", "hunter2")
        c.post("/users", data={"action": "remove", "username": "alice"})
        self.assertTrue(mp.AUTH.has_user("alice"))

    def test_auth_me(self):
        self.assertEqual(mp.app.test_client().get("/auth/me").status_code, 401)
        d = self._login("alice", "hunter2").get("/auth/me").get_json()
        self.assertEqual(d["username"], "alice")
        self.assertTrue(d["admin"])
        d2 = self._login("bob", "s3cret").get("/auth/me").get_json()
        self.assertEqual(d2["username"], "bob")
        self.assertFalse(d2["admin"])


if __name__ == "__main__":
    unittest.main()

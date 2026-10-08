"""Multi-user gateway auth: the mneme.auth store (token/password/session) and
the gateway's _authorize seam (Bearer / Basic / ?token= / cookie / 401 / off)."""

import os
import sys
import base64
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="mneme_auth_")
os.environ["MNEME_CHUNK_DIR"] = os.path.join(_TMP, "chunks")
os.environ["MNEME_GATEWAY_CONFIG_DIR"] = os.path.join(_TMP, "gwconfig")
os.environ.pop("MNEME_GATEWAY_TOKEN", None)
os.makedirs(os.environ["MNEME_CHUNK_DIR"], exist_ok=True)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme.auth import AuthStore, add_user, sign_session, verify_session  # noqa: E402
import gateway as gw  # noqa: E402


def _b64(s):
    return base64.b64encode(s.encode()).decode()


class TestAuthStore(unittest.TestCase):
    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")

    def test_add_user_hashes_and_mints_token(self):
        e = add_user(self.uf, "alice", "hunter2")
        self.assertEqual(e["username"], "alice")
        self.assertTrue(e["token"])
        self.assertTrue(e["password_hash"].startswith(("pbkdf2:", "scrypt:")))

    def test_add_user_uses_explicit_token(self):
        e = add_user(self.uf, "bob", "s3cret", token="tok-1")
        self.assertEqual(e["token"], "tok-1")

    def test_add_user_updates_existing(self):
        add_user(self.uf, "alice", "oldpw", token="tok-old")
        add_user(self.uf, "alice", "newpw", token="tok-new")
        s = AuthStore(self.uf)
        self.assertEqual(len(s.users), 1)
        self.assertIsNotNone(s.check_password("alice", "newpw"))
        self.assertIsNone(s.check_password("alice", "oldpw"))
        self.assertIsNotNone(s.check_token("tok-new"))

    def test_check_token_and_password(self):
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        add_user(self.uf, "bob", "s3cret", token="tok-b")
        s = AuthStore(self.uf)
        self.assertEqual(s.check_token("tok-a")["username"], "alice")
        self.assertIsNone(s.check_token("nope"))
        self.assertEqual(s.check_password("bob", "s3cret")["username"], "bob")
        self.assertIsNone(s.check_password("bob", "wrong"))

    def test_empty_when_no_file(self):
        s = AuthStore(os.path.join(tempfile.mkdtemp(), "missing.yaml"))
        self.assertFalse(s)
        self.assertIsNone(s.check_token("x"))
        self.assertIsNone(s.check_password("a", "b"))

    def test_mtime_reload_picks_up_new_user(self):
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        s = AuthStore(self.uf)
        self.assertIsNone(s.check_token("tok-c"))
        add_user(self.uf, "carol", "pw", token="tok-c")  # same path, new mtime
        self.assertEqual(s.check_token("tok-c")["username"], "carol")


class TestSessionCookie(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        c = sign_session("secret", "alice")
        self.assertEqual(verify_session("secret", c)["username"], "alice")
        self.assertIsNone(verify_session("secret", c + "x"))
        self.assertIsNone(verify_session("other", c))
        self.assertIsNone(verify_session("secret", ""))


class _AuthorizeBase(unittest.TestCase):
    """Swaps in a temp AuthStore so tests control on/off without real users."""

    def setUp(self):
        self.uf = os.path.join(tempfile.mkdtemp(), "mneme_users.yaml")
        add_user(self.uf, "alice", "hunter2", token="tok-a")
        self._orig_auth, self._orig_token = gw.AUTH, gw.GATEWAY_TOKEN
        gw.AUTH = AuthStore(self.uf)
        gw.GATEWAY_TOKEN = ""

    def tearDown(self):
        gw.AUTH, gw.GATEWAY_TOKEN = self._orig_auth, self._orig_token

    def _client(self):
        return gw.app.test_client()


class TestGatewayAuthorizeOn(_AuthorizeBase):
    def test_401_without_credentials(self):
        self.assertEqual(self._client().get("/health").status_code, 401)

    def test_bearer_token(self):
        c = self._client()
        r = c.get("/health", headers={"Authorization": "Bearer tok-a"})
        self.assertEqual(r.status_code, 200)

    def test_basic_auth(self):
        c = self._client()
        r = c.get("/health", headers={"Authorization": "Basic " + _b64("alice:hunter2")})
        self.assertEqual(r.status_code, 200)

    def test_query_token_and_cookie(self):
        c = self._client()
        self.assertEqual(c.get("/health?token=tok-a").status_code, 200)
        c2 = self._client()
        c2.set_cookie("mneme_token", "tok-a")
        self.assertEqual(c2.get("/health").status_code, 200)

    def test_wrong_credentials_401(self):
        c = self._client()
        self.assertEqual(c.get("/health", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.get("/health", headers={"Authorization": "Basic " + _b64("alice:wrong")}).status_code, 401)

    def test_legacy_gateway_token_still_works(self):
        gw.GATEWAY_TOKEN = "legacy"
        try:
            c = self._client()
            self.assertEqual(c.get("/health", headers={"Authorization": "Bearer legacy"}).status_code, 200)
            self.assertEqual(c.get("/health", headers={"Authorization": "Bearer tok-a"}).status_code, 200)
        finally:
            gw.GATEWAY_TOKEN = ""


class TestGatewayAuthorizeOff(unittest.TestCase):
    def test_open_when_no_users_no_token(self):
        orig_auth, orig_token = gw.AUTH, gw.GATEWAY_TOKEN
        try:
            gw.AUTH = AuthStore(os.path.join(tempfile.mkdtemp(), "missing.yaml"))
            gw.GATEWAY_TOKEN = ""
            self.assertEqual(gw.app.test_client().get("/health").status_code, 200)
        finally:
            gw.AUTH, gw.GATEWAY_TOKEN = orig_auth, orig_token


if __name__ == "__main__":
    unittest.main()

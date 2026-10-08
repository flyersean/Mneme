"""Mneme gateway authentication — multi-user, static, no registration.

Users live in ONE static YAML file (``mneme_users.yaml``) in the gateway config
dir. Each user has:

  username       the login name (for the browser's username/password prompt)
  password_hash  a PBKDF2 hash — never a plaintext password
  token          the API token for agents: ``Authorization: Bearer <token>``

No signup, no email, no password-reset. The admin adds users with the ``adduser``
helper (which hashes the password and can mint a token for you), and the running
gateway re-reads the file on the next request whenever its mtime changes — so no
restart is needed after adding a user.

Auth is OFF while the file is absent or has no users. Backward compatible with
the old single ``MNEME_GATEWAY_TOKEN`` (still honoured by the gateway).

This module is standalone: the gateway imports it, and the proxy can import the
same module later if per-instance auth is ever wanted.

CLI (run from anywhere):
  python3 proxy/mneme/auth.py adduser                          # interactive
  python3 proxy/mneme/auth.py adduser -u alice -p secret       # mint a token
  python3 proxy/mneme/auth.py adduser -u alice -p secret -t tok
  python3 proxy/mneme/auth.py hash-password                    # print a hash
"""

import os
import sys
import getpass
import secrets
import base64

import yaml
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

DEFAULT_CONFIG_DIR = os.path.expanduser(
    os.environ.get("MNEME_GATEWAY_CONFIG_DIR", "~/mneme/gateway")
)
USERS_FILENAME = "mneme_users.yaml"
SESSION_SALT = "mneme-gateway-session"

# Header written at the top of a generated users file so the format documents
# itself. (The example file at the repo root mirrors this.)
_HEADER = (
    "# Mneme gateway users — one entry per user.\n"
    "#\n"
    "#   username       login name (used by the browser's username/password prompt)\n"
    "#   password_hash  a PBKDF2 hash — NEVER a plaintext password. Generate with:\n"
    "#                    python3 proxy/mneme/auth.py adduser\n"
    "#   token          the API token for agents. Send it as:\n"
    "#                    Authorization: Bearer <token>   (or ?token=<token>)\n"
    "#   admin          unused for now — everyone is an admin.\n"
    "#\n"
    "# Auth is OFF while this file has no users (or is absent). The running\n"
    "# gateway re-reads it whenever it changes, so no restart is needed.\n"
)


def _read_users(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f.read()) or {}


class AuthStore:
    """Loaded list of gateway users with token + password lookup.

    ``users_file`` defaults to ``mneme_users.yaml`` under the gateway config
    dir (``MNEME_GATEWAY_CONFIG_DIR`` or ``~/mneme/gateway``). The file is
    re-read lazily whenever its mtime changes.
    """

    def __init__(self, users_file=None):
        self.users_file = users_file or os.path.join(DEFAULT_CONFIG_DIR, USERS_FILENAME)
        self.users = []
        self._token_index = {}
        self._mtime = None
        self._ensure_loaded()

    def _ensure_loaded(self):
        try:
            mtime = os.path.getmtime(self.users_file)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime

        users, token_index = [], {}
        if mtime is not None:
            data = _read_users(self.users_file)
            for u in data.get("users") or []:
                username = (u.get("username") or "").strip()
                if not username:
                    continue
                user = {
                    "username": username,
                    "password_hash": u.get("password_hash") or "",
                    "token": u.get("token") or "",
                    "admin": bool(u.get("admin", True)),
                }
                users.append(user)
                if user["token"]:
                    token_index[user["token"]] = user
        # Reference swap is atomic; readers see old or new, never a partial list.
        self.users = users
        self._token_index = token_index

    def check_token(self, token):
        if not token:
            return None
        self._ensure_loaded()
        return self._token_index.get(token)

    def check_password(self, username, password):
        if not username or not password:
            return None
        self._ensure_loaded()
        for u in self.users:
            if u["username"] == username and u["password_hash"]:
                if check_password_hash(u["password_hash"], password):
                    return u
        return None

    def __bool__(self):
        self._ensure_loaded()
        return bool(self.users)


def check_request(auth_store, headers, query_token="", cookie_token="", legacy_token=""):
    """Return True if the request carries a valid credential.

    Checks a Bearer token, then HTTP Basic, then a ``?token=`` / cookie token.
    ``legacy_token`` (optional) is also accepted as a Bearer / ``?token=`` value
    for backward compatibility with the old single gateway token. Used by both
    the gateway and the proxy so they enforce the exact same rules.
    """
    auth_header = headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        tok = auth_header[7:].strip()
        if auth_store.check_token(tok) or (legacy_token and tok == legacy_token):
            return True
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8", "replace")
            username, _, password = decoded.partition(":")
            if auth_store.check_password(username, password):
                return True
        except Exception:
            pass
    tok = query_token or cookie_token
    if tok and (auth_store.check_token(tok) or (legacy_token and tok == legacy_token)):
        return True
    return False


# ── signed session cookies (for the future login form; secret comes from the
#    Flask app's secret_key) ──────────────────────────────────────────────────
def make_session_serializer(secret):
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT)


def sign_session(secret, username, max_age=7 * 86400):
    return make_session_serializer(secret).dumps({"username": username})


def verify_session(secret, value, max_age=7 * 86400):
    if not value:
        return None
    try:
        return make_session_serializer(secret).loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


# ── user management ──────────────────────────────────────────────────────────
def add_user(users_file, username, password, token=None, admin=True):
    """Add or update a user, hashing the password. Returns the user dict (with
    the final token, minted if none was supplied)."""
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")

    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    final_token = (token or "").strip() or secrets.token_urlsafe(32)
    entry = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "token": final_token,
        "admin": bool(admin),
    }
    for i, u in enumerate(users):
        if u.get("username") == username:
            users[i] = entry
            break
    else:
        users.append(entry)

    os.makedirs(os.path.dirname(users_file), exist_ok=True)
    with open(users_file, "w", encoding="utf-8") as f:
        f.write(_HEADER)
        f.write(yaml.safe_dump({"users": users}, sort_keys=False, default_flow_style=False))
    return entry


def _cli():
    import argparse

    p = argparse.ArgumentParser(description="Mneme gateway user management")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adduser", help="add or update a gateway user")
    a.add_argument("-u", "--username", help="login name (prompted if omitted)")
    a.add_argument("-p", "--password", help="password (prompted, hidden, if omitted)")
    a.add_argument("-t", "--token", help="API token (random one is minted if omitted)")
    a.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")
    a.add_argument("--no-admin", action="store_true",
                   help="mark the user as not-admin (everyone is admin by default)")

    h = sub.add_parser("hash-password", help="print a PBKDF2 hash for a password")
    h.add_argument("-p", "--password", help="password to hash (prompted if omitted)")

    args = p.parse_args()

    if args.cmd == "adduser":
        username = (args.username or input("  username: ")).strip()
        if args.password:
            password = args.password
        else:
            password = getpass.getpass(f"  password for {username}: ")
            confirm = getpass.getpass("  confirm password: ")
            if password != confirm:
                print("  passwords don't match — nothing written", file=sys.stderr)
                sys.exit(1)
        users_file = os.path.join(args.config_dir, USERS_FILENAME)
        entry = add_user(users_file, username, password, token=args.token,
                         admin=not args.no_admin)
        print(f"  wrote {users_file}")
        print(f"  username: {entry['username']}")
        print(f"  token:    {entry['token']}")

    elif args.cmd == "hash-password":
        pw = args.password or getpass.getpass("  password: ")
        print(generate_password_hash(pw))


if __name__ == "__main__":
    _cli()

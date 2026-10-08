"""Mneme authentication — multi-user, one static file, forward-compatible.

Users live in ONE YAML file (``mneme_users.yaml``) in the gateway config dir
(``MNEME_GATEWAY_CONFIG_DIR`` or ``~/mneme/gateway``). Every proxy AND the
gateway read this same file, so users are global to the machine: adding a user
on one proxy makes them valid on all of them, and revoking a user revokes them
everywhere.

Each user has:

  username        unique login name (for the browser login form)
  password_hash   a PBKDF2 hash — never a plaintext password
  admin           True only for the admin account (the first account). Only the
                  admin can create or revoke other accounts.
  email           placeholder — reserved for a future email/verification signup
  full_name       placeholder — reserved for a future personal-info signup
  totp_secret     placeholder — reserved for a future 2FA signup
  totp_enabled    placeholder — reserved for a future 2FA signup
  created_at      when the account was created (ISO-8601 UTC)
  last_login      placeholder — reserved for future login tracking
  tokens          list of API tokens; agents/extensions send one as
                  ``Authorization: Bearer <value>`` (or ``?token=<value>``).
                  Each token: id, label, value, created_at, last_used.

The file is versioned (``version: 1``) and the loader fills defaults for any
missing field, so files written by older versions still load, and future fields
can be added without breaking older readers.

Auth is OFF while the file is absent or has no users. Backward compatible with
the old single-token ``token`` field (auto-migrated into the ``tokens`` list on
load) and the legacy ``MNEME_GATEWAY_TOKEN`` (still honoured by the gateway).

CLI (run from anywhere):
  python3 proxy/mneme/auth.py adduser -u alice -p secret
  python3 proxy/mneme/auth.py deluser -u alice
  python3 proxy/mneme/auth.py resetpw -u alice
  python3 proxy/mneme/auth.py addtoken -u alice -l "telegram-gateway"
  python3 proxy/mneme/auth.py listusers
  python3 proxy/mneme/auth.py hash-password
"""

import os
import sys
import getpass
import secrets
import base64
import datetime

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
    "# Mneme users — one entry per user. Auth is OFF while this file has no users.\n"
    "#\n"
    "#   username        unique login name\n"
    "#   password_hash   PBKDF2 hash — generate with `python3 proxy/mneme/auth.py adduser`\n"
    "#   admin           True only for the admin account (the only one that can\n"
    "#                   create or revoke other accounts)\n"
    "#   email           placeholder — reserved for a future email/verification signup\n"
    "#   full_name       placeholder — reserved for a future personal-info signup\n"
    "#   totp_secret     placeholder — reserved for a future 2FA signup\n"
    "#   totp_enabled    placeholder — reserved for a future 2FA signup\n"
    "#   tokens          API tokens for agents/extensions. Send one as:\n"
    "#                     Authorization: Bearer <value>   (or ?token=<value>)\n"
    "#                   each token: id, label, value, created_at, last_used\n"
    "#\n"
    "# The running gateway/proxy re-reads this file whenever it changes, so no\n"
    "# restart is needed after adding/removing a user or token.\n"
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _new_token_id():
    return "tok_" + secrets.token_hex(8)


def _read_users(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f.read()) or {}


def _write_users(users_file, users):
    os.makedirs(os.path.dirname(users_file), exist_ok=True)
    with open(users_file, "w", encoding="utf-8") as f:
        f.write(_HEADER)
        f.write(yaml.safe_dump({"version": 1, "users": users},
                               sort_keys=False, default_flow_style=False))


def _normalize_tokens(u):
    """Return a normalized list of token dicts for a raw user entry, merging the
    legacy single ``token`` field (if present) into the ``tokens`` list."""
    tokens = []
    seen = set()
    for t in u.get("tokens") or []:
        if not isinstance(t, dict):
            continue
        value = (t.get("value") or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        tokens.append({
            "id": t.get("id") or _new_token_id(),
            "label": (t.get("label") or "default").strip(),
            "value": value,
            "created_at": t.get("created_at") or "",
            "last_used": t.get("last_used") or "",
        })
    legacy = (u.get("token") or "").strip()
    if legacy and legacy not in seen:
        tokens.append({
            "id": _new_token_id(),
            "label": "default",
            "value": legacy,
            "created_at": u.get("created_at") or "",
            "last_used": "",
        })
    return tokens


def _normalize_user(u):
    """Normalize a raw user entry from the file into the full schema, filling
    defaults for any field that is missing (forward compatibility)."""
    user = {
        "username": (u.get("username") or "").strip(),
        "password_hash": u.get("password_hash") or "",
        "admin": bool(u.get("admin", True)),
        "email": u.get("email") or "",
        "full_name": u.get("full_name") or "",
        "totp_secret": u.get("totp_secret") or "",
        "totp_enabled": bool(u.get("totp_enabled", False)),
        "created_at": u.get("created_at") or "",
        "last_login": u.get("last_login") or "",
    }
    user["tokens"] = _normalize_tokens(u)
    return user


def _ensure_token(entry, value, label="default"):
    """Add a token (by value) to an entry's ``tokens`` list if not present."""
    value = (value or "").strip()
    if not value:
        return None
    for t in entry["tokens"]:
        if t["value"] == value:
            return t
    tok = {
        "id": _new_token_id(),
        "label": (label or "default").strip(),
        "value": value,
        "created_at": _now(),
        "last_used": "",
    }
    entry["tokens"].insert(0, tok)
    return tok


class AuthStore:
    """Loaded list of users with token + password lookup.

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
                user = _normalize_user(u)
                if not user["username"]:
                    continue
                users.append(user)
                for t in user["tokens"]:
                    if t["value"]:
                        token_index[t["value"]] = user
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

    def has_user(self, username):
        self._ensure_loaded()
        return any(u["username"] == username for u in self.users)

    def get_user(self, username):
        self._ensure_loaded()
        for u in self.users:
            if u["username"] == username:
                return u
        return None

    def is_admin(self, username):
        self._ensure_loaded()
        for u in self.users:
            if u["username"] == username:
                return u["admin"]
        return False

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


# ── signed session cookies (secret comes from the Flask app's secret_key) ───
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
    """Add a new user (or update an existing one's password/admin), hashing the
    password. A NEW user gets one freshly-minted token (or the one passed); an
    EXISTING user keeps its tokens and placeholder fields. Returns the user dict.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")

    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    existing = next((u for u in users if (u.get("username") or "") == username), None)

    if existing is not None:
        entry = _normalize_user(existing)
        entry["password_hash"] = generate_password_hash(password)
        entry["admin"] = bool(admin)
        if (token or "").strip():
            _ensure_token(entry, token)
    else:
        entry = {
            "username": username,
            "password_hash": generate_password_hash(password),
            "admin": bool(admin),
            "email": "",
            "full_name": "",
            "totp_secret": "",
            "totp_enabled": False,
            "created_at": _now(),
            "last_login": "",
            "tokens": [],
        }
        _ensure_token(entry, (token or "").strip() or secrets.token_urlsafe(32))

    for i, u in enumerate(users):
        if (u.get("username") or "") == username:
            users[i] = entry
            break
    else:
        users.append(entry)

    _write_users(users_file, users)
    return entry


def delete_user(users_file, username):
    """Delete a user (and, since tokens live inside the user record, all of its
    tokens). Returns True if a user was removed."""
    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    remaining = [u for u in users if (u.get("username") or "") != username]
    if len(remaining) == len(users):
        return False
    _write_users(users_file, remaining)
    return True


def reset_password(users_file, username, password):
    """Reset a user's password, preserving everything else. Returns True if the
    user was found and updated."""
    if not password:
        raise ValueError("password is required")
    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    for i, u in enumerate(users):
        if (u.get("username") or "") == username:
            users[i] = _normalize_user(u)
            users[i]["password_hash"] = generate_password_hash(password)
            _write_users(users_file, users)
            return True
    return False


def add_token(users_file, username, label="", token=None):
    """Mint (or accept) a token for a user and append it. Returns the new token
    dict. Raises ValueError if the user does not exist."""
    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    for i, u in enumerate(users):
        if (u.get("username") or "") == username:
            users[i] = _normalize_user(u)
            value = (token or "").strip() or secrets.token_urlsafe(32)
            tok = _ensure_token(users[i], value, label)
            _write_users(users_file, users)
            return tok
    raise ValueError(f"no such user: {username}")


def revoke_token(users_file, token_value):
    """Remove a token by its value (across all users). Returns True if removed."""
    token_value = (token_value or "").strip()
    if not token_value:
        return False
    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    users = [dict(u) for u in (data.get("users") or [])]
    removed = False
    for i, u in enumerate(users):
        norm = _normalize_user(u)
        before = len(norm["tokens"])
        norm["tokens"] = [t for t in norm["tokens"] if t["value"] != token_value]
        if len(norm["tokens"]) < before:
            removed = True
            users[i] = norm
    if removed:
        _write_users(users_file, users)
    return removed


def list_users(users_file):
    """Return the normalized list of users (empty if the file is absent)."""
    data = _read_users(users_file) if os.path.isfile(users_file) else {}
    out = []
    for u in data.get("users") or []:
        user = _normalize_user(u)
        if user["username"]:
            out.append(user)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────
def _cli():
    import argparse

    p = argparse.ArgumentParser(description="Mneme user management")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("adduser", help="add or update a user")
    a.add_argument("-u", "--username", help="login name (prompted if omitted)")
    a.add_argument("-p", "--password", help="password (prompted, hidden, if omitted)")
    a.add_argument("-t", "--token", help="API token (a random one is minted if omitted)")
    a.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")
    a.add_argument("--no-admin", action="store_true",
                   help="mark the user as not-admin (default is admin)")

    d = sub.add_parser("deluser", help="delete a user (and all its tokens)")
    d.add_argument("-u", "--username", required=True)
    d.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")

    r = sub.add_parser("resetpw", help="reset a user's password")
    r.add_argument("-u", "--username", required=True)
    r.add_argument("-p", "--password", help="new password (prompted if omitted)")
    r.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")

    t = sub.add_parser("addtoken", help="mint a token for a user and print it")
    t.add_argument("-u", "--username", required=True)
    t.add_argument("-l", "--label", default="default", help="token label")
    t.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")

    l = sub.add_parser("listusers", help="list users (and token counts)")
    l.add_argument("-d", "--config-dir", default=DEFAULT_CONFIG_DIR,
                   help="gateway config dir (default: %(default)s)")

    h = sub.add_parser("hash-password", help="print a PBKDF2 hash for a password")
    h.add_argument("-p", "--password", help="password to hash (prompted if omitted)")

    args = p.parse_args()
    users_file = os.path.join(args.config_dir, USERS_FILENAME) \
        if hasattr(args, "config_dir") else None

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
        entry = add_user(users_file, username, password, token=args.token,
                         admin=not args.no_admin)
        primary = entry["tokens"][0]["value"] if entry["tokens"] else ""
        print(f"  wrote {users_file}")
        print(f"  username: {entry['username']}")
        print(f"  token:    {primary}")

    elif args.cmd == "deluser":
        if delete_user(users_file, args.username):
            print(f"  removed user: {args.username}")
        else:
            print(f"  no such user: {args.username}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "resetpw":
        password = args.password or getpass.getpass(f"  new password for {args.username}: ")
        if reset_password(users_file, args.username, password):
            print(f"  password reset for {args.username}")
        else:
            print(f"  no such user: {args.username}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "addtoken":
        tok = add_token(users_file, args.username, label=args.label)
        print(f"  token for {args.username} ({tok['label']}): {tok['value']}")

    elif args.cmd == "listusers":
        for u in list_users(users_file):
            admin = "admin" if u["admin"] else "user"
            print(f"  {u['username']:20} {admin:6} tokens={len(u['tokens'])}")

    elif args.cmd == "hash-password":
        pw = args.password or getpass.getpass("  password: ")
        print(generate_password_hash(pw))


if __name__ == "__main__":
    _cli()

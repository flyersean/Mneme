"""Flask glue for Mneme's browser auth UI.

The core auth logic lives in :mod:`mneme.auth` (shared by the proxy and the
gateway). This module only wires the HTTP surface — a before-request guard plus
the browser routes — so ``mneme_proxy.py`` stays lean.

``register(app, get_auth, cors_response)`` wires:

  - a ``before_request`` guard (session cookie → Bearer/Basic/token → 401),
  - ``/login`` and ``/create-account`` (first-run bootstrap, localhost-only),
  - ``/logout``,
  - ``/tokens``  (every signed-in user manages their own tokens),
  - ``/users``   (admin-only: list/add/remove accounts, reset passwords),
  - ``/extensions/generate-token`` (mint a token from the extensions UI).
"""

import html
import os
import secrets
from urllib.parse import quote

from flask import request, redirect, Response

from mneme.auth import (
    check_request, sign_session, verify_session,
    add_user, delete_user, reset_password, add_token, revoke_token,
)

_SESSION_COOKIE = "mneme_session"
_PUBLIC_PATHS = ("/login", "/create-account", "/logout")


def _is_loopback(addr):
    return addr in ("127.0.0.1", "::1", "localhost") or (addr or "").startswith("127.")


_AUTH_STYLE = (
    "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg);display:flex;"
    "align-items:center;justify-content:center;min-height:100vh;padding:16px;"
    "box-sizing:border-box}"
    ".card{background:var(--panel);border:1px solid var(--line);border-radius:12px;"
    "padding:28px;width:min(560px,94vw);box-sizing:border-box}"
    "h1{font-size:18px;margin:0 0 2px}"
    ".sub{color:var(--muted);font-size:13px;margin:0 0 18px}"
    "label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin:12px 0 4px}"
    "input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:7px;"
    "font:inherit;font-size:14px;background:var(--bg);color:var(--fg);box-sizing:border-box}"
    "input:focus{outline:none;border-color:var(--accent)}"
    "button{width:100%;margin-top:14px;padding:10px;background:var(--accent);color:#fff;"
    "border:none;border-radius:7px;font-weight:600;font-size:14px;cursor:pointer}"
    "button.danger{background:#e5534b}"
    "button.inline{width:auto;margin:0;padding:5px 10px;font-size:12px}"
    ".err{color:#e5534b;font-size:13px;margin-top:12px}"
    ".ok{color:#57ab5a;font-size:13px;margin-top:12px}"
    ".row{display:flex;align-items:center;justify-content:space-between;gap:10px;"
    "border-top:1px solid var(--line);padding:10px 0}"
    ".row-main{min-width:0;flex:1}"
    ".lbl{font-weight:600;font-size:14px;display:block}"
    ".role{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}"
    "code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;"
    "color:var(--accent);word-break:break-all;display:block;margin:2px 0}"
    ".meta{color:var(--muted);font-size:12px}"
    ".tokbox{border:1px dashed var(--accent);border-radius:8px;padding:12px;margin:12px 0}"
    "form.inline{display:inline;margin:0}"
    "form.inline input{width:auto}"
    ".nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}"
    ".nav a{color:var(--accent);font-size:13px;text-decoration:none}"
    "h2{font-size:13px;margin:18px 0 8px;color:var(--muted);font-weight:600}"
    ".empty{color:var(--muted);font-size:13px;padding:8px 0}"
)


def _page(title, body):
    esc = html.escape
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<link rel="stylesheet" href="/static/theme.css"><style>{_AUTH_STYLE}</style>'
            f'<title>Mneme — {esc(title)}</title></head><body>'
            f'<div class="card">{body}</div></body></html>')


def register(app, get_auth, cors_response):
    esc = html.escape

    # ── session secret (stable across restarts) ──────────────────────────────
    def _session_secret():
        s = os.environ.get("MNEME_SESSION_SECRET")
        if s:
            return s
        p = os.path.join(os.path.dirname(get_auth().users_file), "session_secret")
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            pass
        v = secrets.token_urlsafe(32)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(v)
        except OSError:
            pass
        return v

    app.secret_key = _session_secret()

    def _wants_html():
        return "text/html" in (request.headers.get("Accept") or "")

    def _session_user():
        val = request.cookies.get(_SESSION_COOKIE)
        if not val:
            return None
        sess = verify_session(app.secret_key, val)
        if not sess:
            return None
        username = sess.get("username")
        return username if username and get_auth().has_user(username) else None

    def _set_session(resp, username):
        resp.set_cookie(_SESSION_COOKIE, sign_session(app.secret_key, username),
                        httponly=True, max_age=7 * 86400, samesite="Lax")
        return resp

    # ── auth guard ───────────────────────────────────────────────────────────
    @app.before_request
    def _authorize():
        if request.method == "OPTIONS" or request.path == "/health":
            return None
        if request.path in _PUBLIC_PATHS or request.path.startswith("/static/"):
            return None
        if not get_auth():
            # No users yet: API stays open (backward compatible); browsers get
            # the first-run create-account page.
            if _wants_html():
                return redirect("/create-account")
            return None
        if _session_user() or check_request(get_auth(), request.headers,
                                            request.args.get("token"),
                                            request.cookies.get("mneme_token")):
            return None
        if _wants_html():
            return redirect("/login?next=" + quote(request.path))
        return Response("unauthorized", status=401,
                        headers={"WWW-Authenticate": 'Basic realm="mneme"'})

    # ── login ────────────────────────────────────────────────────────────────
    def _login_form(nxt):
        return (f'<form method="post"><input type="hidden" name="next" value="{esc(nxt)}">'
                f'<label>Username</label><input name="username" autofocus>'
                f'<label>Password</label><input type="password" name="password">'
                f'<button type="submit">Sign in</button></form>')

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not get_auth():
            return redirect("/create-account")
        nxt = request.form.get("next") or request.args.get("next") or "/"
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if get_auth().check_password(username, password):
                return _set_session(redirect(nxt), username)
            return _page("Sign in",
                         f'<h1>Mneme</h1><p class="sub">Sign in</p>'
                         f'{_login_form(nxt)}'
                         f'<div class="err">Invalid username or password</div>')
        return _page("Sign in", f'<h1>Mneme</h1><p class="sub">Sign in</p>{_login_form(nxt)}')

    @app.route("/logout")
    def logout():
        resp = redirect("/login")
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    # ── who am I (for pages that branch on the signed-in user) ───────────────
    @app.route("/auth/me", methods=["GET"])
    def auth_me():
        username = _session_user()
        if not username:
            return cors_response({"username": None, "admin": False}, status=401)
        return cors_response({"username": username, "admin": get_auth().is_admin(username)})

    # ── first-run account creation (localhost only) ──────────────────────────
    def _create_form():
        return ('<form method="post">'
                '<label>Username</label><input name="username" autofocus>'
                '<label>Password</label><input type="password" name="password">'
                '<label>Confirm password</label><input type="password" name="confirm">'
                '<button type="submit">Create account</button></form>')

    @app.route("/create-account", methods=["GET", "POST"])
    def create_account():
        if get_auth():
            return redirect("/login")
        if not _is_loopback(request.remote_addr):
            return Response("account creation is only available from the machine "
                            "running Mneme", status=403)
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            if not username or not password:
                return _page("Create your account",
                             f'<h1>Mneme</h1><p class="sub">Create your account</p>'
                             f'{_create_form()}'
                             f'<div class="err">Username and password are required</div>')
            if password != confirm:
                return _page("Create your account",
                             f'<h1>Mneme</h1><p class="sub">Create your account</p>'
                             f'{_create_form()}'
                             f'<div class="err">Passwords don\'t match</div>')
            if len(password) < 6:
                return _page("Create your account",
                             f'<h1>Mneme</h1><p class="sub">Create your account</p>'
                             f'{_create_form()}'
                             f'<div class="err">Password must be at least 6 characters</div>')
            add_user(get_auth().users_file, username, password, admin=True)
            return _set_session(redirect("/"), username)
        return _page("Create your account",
                     f'<h1>Mneme</h1><p class="sub">Create your account</p>{_create_form()}')

    # ── tokens page (every signed-in user) ───────────────────────────────────
    @app.route("/tokens", methods=["GET", "POST"])
    def tokens():
        username = _session_user()
        if not username:
            return redirect("/login?next=/tokens")
        user = get_auth().get_user(username)
        message = ""

        if request.method == "POST":
            action = request.form.get("action") or ""
            if action == "generate":
                label = (request.form.get("label") or "default").strip()
                tok = add_token(get_auth().users_file, username, label=label)
                message = f'New token created — copy it now: <code>{esc(tok["value"])}</code>'
                user = get_auth().get_user(username)  # reload
            elif action == "revoke":
                revoke_token(get_auth().users_file, request.form.get("token") or "")
                message = "Token revoked."
                user = get_auth().get_user(username)

        rows = []
        for t in user["tokens"]:
            rows.append(
                f'<div class="row"><div class="row-main">'
                f'<span class="lbl">{esc(t["label"])}</span>'
                f'<code>{esc(t["value"])}</code>'
                f'<span class="meta">created {esc(t["created_at"] or "—")}</span></div>'
                f'<form method="post" class="inline">'
                f'<input type="hidden" name="action" value="revoke">'
                f'<input type="hidden" name="token" value="{esc(t["value"])}">'
                f'<button type="submit" class="danger inline">Revoke</button></form></div>'
            )
        body = (
            f'<div class="nav"><h1>Tokens</h1><a href="/">back</a></div>'
            f'<p class="sub">API tokens authenticate agents and extensions against this '
            f'Mneme. Send one as <code>Authorization: Bearer &lt;token&gt;</code>.</p>'
        )
        if message:
            body += f'<div class="tokbox">{message}</div>'
        tokens_html = "".join(rows) if rows else '<div class="empty">No tokens yet.</div>'
        body += f'<h2>Your tokens</h2>{tokens_html}'
        body += (f'<h2>Generate a token</h2>'
                 f'<form method="post"><input type="hidden" name="action" value="generate">'
                 f'<label>Label (what is this for?)</label>'
                 f'<input name="label" placeholder="e.g. telegram-gateway">'
                 f'<button type="submit">Generate token</button></form>')
        return _page("Tokens", body)

    # ── users page (admin only) ──────────────────────────────────────────────
    @app.route("/users", methods=["GET", "POST"])
    def users():
        username = _session_user()
        if not username:
            return redirect("/login?next=/users")
        if not get_auth().is_admin(username):
            return Response("admin only", status=403)
        message = ""

        if request.method == "POST":
            action = request.form.get("action") or ""
            if action == "add":
                new_u = (request.form.get("username") or "").strip()
                new_p = request.form.get("password") or ""
                if new_u and len(new_p) >= 6:
                    add_user(get_auth().users_file, new_u, new_p, admin=False)
                    message = f'Added account <b>{esc(new_u)}</b>.'
                else:
                    message = '<div class="err">Username required; password must be at least 6 characters.</div>'
            elif action == "remove":
                target = (request.form.get("username") or "").strip()
                if target and target != username:
                    delete_user(get_auth().users_file, target)
                    message = f'Removed account <b>{esc(target)}</b> and all its tokens.'
                else:
                    message = '<div class="err">Cannot remove the admin account.</div>'
            elif action == "resetpw":
                target = (request.form.get("username") or "").strip()
                new_p = request.form.get("password") or ""
                if len(new_p) >= 6:
                    reset_password(get_auth().users_file, target, new_p)
                    message = f'Reset password for <b>{esc(target)}</b>.'
                else:
                    message = '<div class="err">Password must be at least 6 characters.</div>'

        rows = []
        for u in get_auth().users:
            role = "admin" if u["admin"] else "user"
            placeholders = (f'<span class="meta">email: {esc(u["email"] or "—")} · '
                            f'full name: {esc(u["full_name"] or "—")} · '
                            f'2FA: {"on" if u["totp_enabled"] else "off"}</span>')
            rows.append(
                f'<div class="row"><div class="row-main">'
                f'<span class="lbl">{esc(u["username"])} <span class="role">{role}</span></span>'
                f'{placeholders}'
                f'<span class="meta">{len(u["tokens"])} token(s)</span></div>'
                f'<form method="post" class="inline">'
                f'<input type="hidden" name="action" value="remove">'
                f'<input type="hidden" name="username" value="{esc(u["username"])}">'
                f'<button type="submit" class="danger inline">Remove</button></form></div>'
            )
        body = f'<div class="nav"><h1>Users</h1><a href="/">back</a></div>'
        body += f'<p class="sub">This admin account can create and revoke other accounts.</p>'
        if message:
            body += f'<div class="tokbox">{message}</div>'
        body += f'<h2>Accounts</h2>{"".join(rows)}'
        body += (f'<h2>Add account</h2>'
                 f'<form method="post"><input type="hidden" name="action" value="add">'
                 f'<label>Username</label><input name="username" autocomplete="off">'
                 f'<label>Password</label><input type="password" name="password">'
                 f'<button type="submit">Add account</button></form>')
        body += (f'<h2>Reset a password</h2>'
                 f'<form method="post"><input type="hidden" name="action" value="resetpw">'
                 f'<label>Username</label><input name="username" autocomplete="off">'
                 f'<label>New password</label><input type="password" name="password">'
                 f'<button type="submit">Reset password</button></form>')
        return _page("Users", body)

    # ── extensions: mint a token for a dedicated 'gateway' user ──────────────
    @app.route("/extensions/generate-token", methods=["POST"])
    def extensions_generate_token():
        auth = get_auth()
        if not auth.has_user("gateway"):
            add_user(auth.users_file, "gateway", secrets.token_urlsafe(24), admin=False)
        tok = add_token(auth.users_file, "gateway", label="gateway")
        return cors_response({"ok": True, "token": tok["value"]})

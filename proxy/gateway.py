#!/usr/bin/env python3
"""Mneme Gateway — one reverse proxy in front of every Mneme proxy instance.

Binds ONE port (default 8000) and gives a single connection to the whole pod.
On RunPod, bind the INTERNAL port: RunPod's nginx owns the reserved ports and
forwards 8001 -> localhost:8000, so the gateway binds 8000 and is reachable at
the reserved 8001 (https://<pod>-8001.proxy.runpod.net).

  - ``/``                  gateway dashboard (every instance listed, links go
                           through the gateway as ``/<port>/…``)
  - ``/gateway/instances`` the instance list as JSON (with a ``base`` prefix)
  - ``/overview/start`` / ``/overview/stop``  start/stop an instance
  - ``/<port>/<path>``     reverse-proxy to ``http://127.0.0.1:<port>/<path>`` —
                           web UI and ``/v1`` alike, SSE streamed unbuffered.
                           HTML responses get a tiny shim injected so each
                           instance's root-relative links/fetches stay under its
                           ``/<port>/`` prefix.

This is the auth choke-point: the instances stay 127.0.0.1 / no-auth; every
request is gated here (see ``_authorize``). Auth is OFF while no users are
configured and ``MNEME_GATEWAY_TOKEN`` is unset. Multi-user auth lives in
``mneme.auth`` — a ``mneme_users.yaml`` file of {username, password_hash,
token} under the gateway config dir. Requests authenticate via a Bearer token,
HTTP Basic, ``?token=``, or a ``mneme_token`` cookie.

Config (env): MNEME_GATEWAY_HOST (127.0.0.1), MNEME_GATEWAY_PORT (8000),
MNEME_CHUNK_DIR (shared dir holding instances/), MNEME_GATEWAY_TOKEN (""),
MNEME_GATEWAY_CONFIG_DIR (gateway config dir, default ~/mneme/gateway).
"""

import os
import re
import sys
import time
import json
import subprocess

from flask import Flask, request, Response, stream_with_context

from mneme.auth import AuthStore, check_request

try:
    import requests
except ImportError:
    print("  [GATEWAY] requests not installed", file=sys.stderr)
    sys.exit(1)

GATEWAY_HOST = os.environ.get("MNEME_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("MNEME_GATEWAY_PORT", "8000"))
CHUNK_DIR = os.path.abspath(os.environ.get("MNEME_CHUNK_DIR") or os.path.expanduser("~/mneme/chunks"))
GATEWAY_TOKEN = os.environ.get("MNEME_GATEWAY_TOKEN", "").strip()
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

AUTH = AuthStore()

app = Flask(__name__)

# Headers that must NOT be forwarded between hops (handled per-connection).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding", "accept-encoding",
}

HTTP_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


# ── auth seam (off until users exist or MNEME_GATEWAY_TOKEN is set) ──────────
@app.before_request
def _authorize():
    if not AUTH and not GATEWAY_TOKEN:
        return None  # auth off

    if check_request(AUTH, request.headers, request.args.get("token"),
                     request.cookies.get("mneme_token"), GATEWAY_TOKEN):
        return None

    return Response("unauthorized", status=401,
                    headers={"WWW-Authenticate": 'Basic realm="mneme", Bearer'})


# ── instance discovery (mirrors the proxy's overview) ───────────────────────
def _instances_root():
    root = os.path.join(CHUNK_DIR, "instances")
    return root if os.path.isdir(root) else None


def _instance_meta(port):
    inst_dir = os.path.join(_instances_root() or "", str(port))
    cfg = os.path.join(inst_dir, "mneme.yaml")
    model, backend = None, "ollama"
    if os.path.isfile(cfg):
        try:
            import yaml as _yaml
            with open(cfg, "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f.read()) or {}
            model = data.get("model") or ((data.get("providers") or {}).get("openrouter") or {}).get("model")
            backend = (data.get("backend") or {}).get("type", "ollama")
        except Exception:
            pass
    return model, backend


def _proxy_up(port):
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _pid_on_port(port):
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            m = re.search(rf":{port}\s.*pid=(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _list_instances():
    out = []
    root = _instances_root()
    names = [n for n in os.listdir(root) if n.isdigit()] if root else []
    seen = set()
    for n in names:
        port = int(n)
        if port in seen:
            continue
        seen.add(port)
        model, backend = _instance_meta(port)
        out.append({"port": port, "model": model or "(unknown)",
                    "backend": backend or "ollama", "running": _proxy_up(port)})
    out.sort(key=lambda d: d["port"])
    return out


# ── HTML shim: keep an instance's root-relative URLs under its /<port>/ prefix,
#    and add the gateway "← Overview" link + a port label into the page. ─────
def _shim_script(port, host_url=""):
    p = str(port)
    overview_href = host_url or "/"
    return (
        "<script>(function(){var port='" + p + "',prefix='/" + p + "';"
        "function fix(u){if(typeof u!=='string'||!u)return u;"
        "if(u.charAt(0)!=='/')return u;if(u.indexOf('//')===0)return u;"
        "if(u===prefix||u.indexOf(prefix+'/')===0)return u;"
        "if(u==='/')return prefix+'/';return prefix+u;}"
        "function rewrite(){var els=document.querySelectorAll('a[href],form[action],script[src],link[href],img[src]');"
        "for(var i=0;i<els.length;i++){var el=els[i];['href','action','src'].forEach(function(a){"
        "var v=el.getAttribute(a);if(v){var f=fix(v);if(f!==v)el.setAttribute(a,f);}});}}"
        "var of=window.fetch;if(of)window.fetch=function(u,o){"
        "if(typeof u==='string')u=fix(u);"
        "else if(u&&u.url){var c=Object.create(u);c.url=fix(u.url);u=c;}return of(u,o);};"
        "var oo=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){return oo.call(this,m,fix(u));};"
        "document.addEventListener('click',function(e){var a=e.target&&e.target.closest?e.target.closest('a[href]'):null;"
        "if(!a)return;var h=a.getAttribute('href');if(!h)return;var f=fix(h);"
        "if(f!==h){e.preventDefault();window.location=f;}},true);"
        "function decorate(){"
        "var nav=document.querySelector('.mneme-nav');"
        "if(nav){var oa=document.createElement('a');oa.href='" + overview_href + "';oa.textContent='← Overview';nav.insertBefore(oa,nav.firstChild);}"
        "var h1=document.querySelector('h1');"
        "if(h1){var s=document.createElement('span');s.textContent=' · port ' + port;"
        "s.style.cssText='color:#6b7280;font-weight:400;font-size:0.72em';h1.appendChild(s);}"
        "}"
        "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){rewrite();decorate();});else{rewrite();decorate();}"
        "})();</script>"
    )


def _inject_shim(body_bytes, port, host_url=""):
    html = body_bytes.decode("utf-8", "replace")
    shim = _shim_script(port, host_url)
    # inject the prefix shim into <head> (or right after <html>/at the start)
    m = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if m:
        return (html[:m.end()] + shim + html[m.end():]).encode("utf-8")
    m = re.search(r"<html[^>]*>", html, re.IGNORECASE)
    if m:
        return (html[:m.end()] + shim + html[m.end():]).encode("utf-8")
    return (shim + html).encode("utf-8")


# ── routes ──────────────────────────────────────────────────────────────────
def _serve_html(name):
    path = os.path.join(STATIC_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        return Response(json.dumps({"error": f"{name} not found"}), status=404,
                        content_type="application/json")


@app.route("/", methods=["GET"])
def gateway_dashboard():
    return _serve_html("gateway.html")


@app.route("/health", methods=["GET"])
def gateway_health():
    return Response(json.dumps({"status": "ok", "role": "gateway",
                                "instances": len(_list_instances())}),
                    content_type="application/json")


@app.route("/gateway/instances", methods=["GET"])
def gateway_instances():
    out = []
    for it in _list_instances():
        it = dict(it)
        it["base"] = "/" + str(it["port"])
        out.append(it)
    return Response(json.dumps({"instances": out}), content_type="application/json")


@app.route("/overview/start", methods=["POST"])
def gateway_start():
    data = request.get_json(force=True, silent=True) or {}
    port = int(data.get("port", 0) or 0)
    if not port:
        return Response(json.dumps({"error": "missing port"}), status=400, content_type="application/json")
    inst_dir = os.path.join(_instances_root() or "", str(port))
    script = None
    for cand in ("start_proxy.sh", f"start_proxy_{port}.sh"):
        p = os.path.join(inst_dir, cand)
        if os.path.isfile(p):
            script = p
            break
    if not script:
        return Response(json.dumps({"ok": False, "error": f"no start script for port {port}"}),
                        status=404, content_type="application/json")
    try:
        subprocess.Popen(["bash", script], cwd=REPO_ROOT, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return Response(json.dumps({"ok": True, "port": port}), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, content_type="application/json")


@app.route("/overview/stop", methods=["POST"])
def gateway_stop():
    data = request.get_json(force=True, silent=True) or {}
    port = int(data.get("port", 0) or 0)
    if not port:
        return Response(json.dumps({"error": "missing port"}), status=400, content_type="application/json")
    pid = _pid_on_port(port)
    if not pid:
        return Response(json.dumps({"ok": False, "message": f"nothing listening on port {port}"}),
                        content_type="application/json")
    try:
        os.kill(pid, 15)
        for _ in range(10):
            time.sleep(0.4)
            if _pid_on_port(port) is None:
                break
        else:
            os.kill(pid, 9)
        return Response(json.dumps({"ok": True, "port": port}), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, content_type="application/json")


def _forward(port, path):
    target = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    headers["Host"] = f"127.0.0.1:{port}"
    headers.setdefault("X-Forwarded-For", request.remote_addr or "")

    try:
        resp = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data() or None,
            stream=True,
            allow_redirects=False,
            timeout=None,
        )
    except requests.exceptions.ConnectionError:
        return Response("gateway: instance not running on port %d" % port, status=502)
    except Exception as e:
        return Response(f"gateway: {e}", status=502)

    rheaders = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    rheaders["X-Accel-Buffering"] = "no"  # keep SSE un-buffered through any nginx in front

    ctype = resp.headers.get("Content-Type", "")
    if "text/html" in ctype:
        # Full page: inject the prefix shim + "← Overview" breadcrumb.
        return Response(_inject_shim(resp.content, port, request.host_url),
                        status=resp.status_code, headers=rheaders)

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(stream_with_context(generate()), status=resp.status_code, headers=rheaders)


@app.route("/<int:port>/", defaults={"path": ""}, methods=HTTP_METHODS)
@app.route("/<int:port>/<path:path>", methods=HTTP_METHODS)
def proxy(port, path):
    return _forward(port, path)


if __name__ == "__main__":
    print(f"  [GATEWAY] chunk dir: {CHUNK_DIR}", flush=True)
    print(f"  [GATEWAY] serving on http://{GATEWAY_HOST}:{GATEWAY_PORT}", flush=True)
    if AUTH.users or GATEWAY_TOKEN:
        n = len(AUTH.users)
        extra = " + legacy token" if GATEWAY_TOKEN else ""
        print(f"  [GATEWAY] auth: ON ({n} user{'s' if n != 1 else ''}{extra})", flush=True)
    else:
        print("  [GATEWAY] auth: OFF (open)", flush=True)
    app.run(host=GATEWAY_HOST, port=GATEWAY_PORT, threaded=True)

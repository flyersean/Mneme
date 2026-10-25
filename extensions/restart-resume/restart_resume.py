#!/usr/bin/env python3
"""restart-resume — wake the agent back up after the Mneme proxy self-restarts.

An HTTP-only extension (talks to the proxy over HTTP; imports nothing from the
proxy). A one-shot sentinel: arm it before a self-restart, and after the proxy
goes down and comes back up ready, it POSTs a wake-up prompt into the chat so
the agent resumes and reports status.

Subcommands
-----------
arm    Write a handoff note and spawn a detached watcher as a transient systemd
       unit (so it survives the proxy restart). Run this BEFORE triggering the
       restart (e.g. before calling restart_proxy).
watch  The sentinel loop: baseline -> down -> up(ready) -> settle -> fire -> exit.
       Normally spawned by `arm`; can also be run manually.
fire   POST the wake-up prompt immediately (manual resume / testing).
check  Verify prerequisites (sudo, systemd-run, /health ready) without firing.

Why systemd-run for detachment
------------------------------
The proxy runs as systemd unit mneme@<port>.service. A child spawned from within
the proxy (even via nohup/setsid) shares the proxy's cgroup, so systemd kills it
when the unit restarts. Launching the watcher as its own transient unit gives it
an independent cgroup that survives the restart.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:7979"
DEFAULT_PORT = 7979
DEFAULT_SETTLE = 8
DEFAULT_TIMEOUT = 120
DEFAULT_BASELINE_TIMEOUT = 30
DEFAULT_FIRE_TIMEOUT = 600
DEFAULT_FIRE_ATTEMPTS = 3


def _env_or(name: str, default):
    return os.environ.get(name) or default


def _log(msg: str, log_file=None) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _get(url: str, token: str, timeout: float):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(req, timeout=timeout)


def _post(url: str, token: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(req, timeout=timeout)


def is_ready(url: str, token: str, timeout: float = 5.0):
    """Return (ready: bool, detail: str). ready = HTTP 200 and body.status == 'ok'."""
    detail = "?"
    try:
        with _get(url.rstrip("/") + "/health", token, timeout) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        if body.get("status") != "ok":
            return False, "status=%r" % body.get("status")
        chat = (body.get("chat") or {}).get("ok")
        embed = (body.get("embed") or {}).get("ok")
        label = (body.get("label") or {}).get("ok")
        # "Complete start" = proxy up AND embedder proven working since startup.
        # chat.ok / label.ok are set BY a chat turn (recorded after process_chat /
        # labeling completes), so gating on them would deadlock: the wake-up POST
        # IS the first chat turn after restart. embed.ok is warmed at startup and
        # is therefore the valid readiness gate.
        if embed is not True:
            return False, ("status=ok embed.ok=%s chat.ok=%s label.ok=%s "
                           "(waiting for embedder warm-up)" % (embed, chat, label))
        return True, "chat.ok=%s embed.ok=%s label.ok=%s" % (chat, embed, label)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except Exception as e:  # URLError, timeout, connection refused
        return False, "%s: %s" % (type(e).__name__, e)


def build_wakeup(handoff: str, port: int) -> str:
    return (
        "The Mneme proxy on port %d just completed an intentional self-restart. "
        "You were restarted to load a change. Verify that GET /health reports "
        "ready (status ok, embedder/labeler ok), then resume what you were doing "
        "and report a concise status update.\n\n"
        "Handoff note from before the restart:\n%s" % (port, handoff)
    )


def _sse_text(raw):
    """Extract assistant text from a stream=True SSE response body."""
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            t = d.get("content")
            if t:
                parts.append(t)
    return "".join(parts)


def post_wakeup(url, token, handoff, port, conversation_id, timeout, log_file,
                attempts=DEFAULT_FIRE_ATTEMPTS):
    """Fire the wake-up prompt with stream=True.

    stream=True is REQUIRED: the proxy only persists a turn to the conversations
    DB on the streaming path (_chat_stream -> _persist_conversation). A non-stream
    wake-up returns the reply to us but never saves it for the client to see.
    Passing conversation_id resumes that exact thread.
    """
    last_err = None
    for i in range(attempts):
        payload = {
            "messages": [{"role": "user", "content": build_wakeup(handoff, port)}],
            "stream": True,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        _log("wake-up attempt %d/%d -> %s/v1/chat/completions (stream, conv=%s)"
             % (i + 1, attempts, url.rstrip("/"), conversation_id or "(none)"),
             log_file)
        try:
            with _post(url.rstrip("/") + "/v1/chat/completions", token, payload,
                       timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            _log("wake-up stream received (%d bytes)" % len(raw), log_file)
            text = _sse_text(raw)
            _log("reply text head: %s"
                 % ((text[:500].replace("\n", " ") or "(empty)")), log_file)
            return text
        except Exception as e:
            last_err = e
            _log("wake-up attempt %d/%d failed: %s" % (i + 1, attempts, e), log_file)
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise RuntimeError("all %d wake-up attempts failed (last: %s)"
                       % (attempts, last_err))


def _poll_until(predicate, url, token, timeout, what, log_file):
    detail = "?"
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, detail = is_ready(url, token)
        if predicate(ok):
            _log("%s: reached (%s)" % (what, detail), log_file)
            return True
        time.sleep(2)
    _log("%s: TIMEOUT after %ds (last: %s)" % (what, timeout, detail), log_file)
    return False


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_arm(args):
    script = os.path.abspath(__file__)
    handoff_file = args.handoff_file or ("/tmp/restart-resume-handoff-%d.txt" % args.port)
    with open(handoff_file, "w") as f:
        f.write(args.handoff)
    _log("handoff written to %s" % handoff_file)

    cmd = [
        "sudo", "-n", "systemd-run", "--collect",
        "--unit=mneme-restart-resume-%d" % args.port,
        "--description=Mneme restart-resume sentinel (port %d)" % args.port,
        "--",
        sys.executable, script, "watch",
        "--url", args.url,
        "--port", str(args.port),
        "--handoff-file", handoff_file,
        "--settle", str(args.settle),
        "--timeout", str(args.timeout),
        "--baseline-timeout", str(args.baseline_timeout),
        "--log-file", args.log_file or ("/tmp/restart-resume-%d.log" % args.port),
    ]
    if args.conversation_id:
        cmd += ["--conversation-id", args.conversation_id]
    if args.token:
        cmd += ["--token", args.token]

    _log("spawn command: %s" % " ".join(cmd))
    if args.dry_run:
        _log("DRY RUN -- sentinel not spawned.")
        if args.restart:
            _log("would also restart: sudo -n systemctl restart mneme@%d.service" % args.port)
        else:
            _log("Now trigger the restart, e.g.: sudo -n systemctl restart mneme@%d.service"
                 % args.port)
        return 0
    r = subprocess.run(cmd)
    if r.returncode != 0:
        _log("ERROR: systemd-run failed with exit %d" % r.returncode)
        return r.returncode
    _log("sentinel armed (transient unit mneme-restart-resume-%d)." % args.port)
    if args.restart:
        restart_cmd = ["sudo", "-n", "systemctl", "restart",
                       "mneme@%d.service" % args.port]
        _log("restarting proxy now: %s" % " ".join(restart_cmd))
        r2 = subprocess.run(restart_cmd)
        if r2.returncode != 0:
            _log("WARNING: restart command exited %d (sentinel is still armed and "
                 "will fire if the proxy goes down/up anyway)" % r2.returncode)
        else:
            _log("restart triggered -- sentinel will fire the wake-up once /health "
                 "reports ready")
    else:
        _log("Now trigger the restart, e.g.: sudo -n systemctl restart mneme@%d.service"
             % args.port)
    _log("sentinel log: %s" % (args.log_file or ("/tmp/restart-resume-%d.log" % args.port)))
    return 0


def cmd_watch(args):
    log_file = args.log_file
    _log("sentinel started: url=%s port=%d settle=%ds timeout=%ds"
         % (args.url, args.port, args.settle, args.timeout), log_file)

    # Phase 1 -- baseline: is the proxy up right now?
    ok, detail = is_ready(args.url, args.token)
    _log("baseline health: %s"
         % ("ready" if ok else ("down/unready (%s)" % detail)), log_file)

    if ok:
        # Phase 2 -- wait for the restart (down).
        if not _poll_until(lambda o: not o, args.url, args.token,
                           args.timeout, "down", log_file):
            _log("never saw shutdown within %ds -- aborting (restart cancelled?)"
                 % args.timeout, log_file)
            return 2
    else:
        _log("proxy not ready at start -- already down, skipping down-wait", log_file)

    # Phase 3 -- wait for complete start (ready).
    if not _poll_until(lambda o: o, args.url, args.token,
                       args.timeout, "up(ready)", log_file):
        _log("proxy never came back ready within %ds -- aborting" % args.timeout,
             log_file)
        return 3

    # Phase 4 -- settle.
    _log("proxy ready -- settling %ds before firing" % args.settle, log_file)
    time.sleep(args.settle)

    # Phase 5 -- fire the wake-up prompt.
    handoff = args.handoff
    if args.handoff_file and os.path.exists(args.handoff_file):
        try:
            handoff = open(args.handoff_file).read().strip() or handoff
        except OSError:
            pass
    if not handoff:
        handoff = "Restart completed -- verify /health and report project status."
    try:
        post_wakeup(args.url, args.token, handoff, args.port,
                    args.conversation_id, args.fire_timeout, log_file)
        _log("sentinel done (exit 0)", log_file)
        return 0
    except Exception as e:
        _log("ERROR firing wake-up: %s" % e, log_file)
        return 4


def cmd_fire(args):
    handoff = args.handoff
    if args.handoff_file and os.path.exists(args.handoff_file):
        try:
            handoff = open(args.handoff_file).read().strip() or handoff
        except OSError:
            pass
    if not handoff:
        handoff = "Restart completed -- verify /health and report project status."
    try:
        post_wakeup(args.url, args.token, handoff, args.port,
                    args.conversation_id, args.fire_timeout, args.log_file)
        return 0
    except Exception as e:
        _log("ERROR firing wake-up: %s" % e, args.log_file)
        return 4


def cmd_check(args):
    ok = True

    r = subprocess.run(["sudo", "-n", "true"])
    print("sudo -n true        : %s"
          % ("OK" if r.returncode == 0 else "FAIL (exit %d)" % r.returncode))
    ok = ok and r.returncode == 0

    try:
        r2 = subprocess.run(["systemd-run", "--version"],
                            capture_output=True, text=True)
        has_run = r2.returncode == 0
    except FileNotFoundError:
        has_run = False
    print("systemd-run         : %s" % ("OK" if has_run else "MISSING"))
    ok = ok and has_run

    ready, detail = is_ready(args.url, args.token)
    print("health %s : %s"
          % (args.url, "ready (%s)" % detail if ready else "NOT ready (%s)" % detail))
    ok = ok and ready

    unit = "mneme@%d.service" % args.port
    print("restart unit        : %s" % unit)
    print("arm command         : python3 %s arm --url %s --port %d --handoff \"...\""
          % (os.path.abspath(__file__), args.url, args.port))
    print("restart command     : sudo -n systemctl restart %s" % unit)
    print("\nCHECK %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def _add_common(p, default_port=DEFAULT_PORT):
    p.add_argument("--url", default=_env_or("MNEME_URL", DEFAULT_URL))
    p.add_argument("--port", type=int, default=default_port)
    p.add_argument("--token", default=_env_or("MNEME_API_TOKEN", ""))
    p.add_argument("--log-file", default=None)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="restart-resume: wake the agent after a proxy self-restart")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_arm = sub.add_parser("arm", help="write handoff + spawn detached watcher")
    _add_common(p_arm)
    p_arm.add_argument("--handoff", default=_env_or("RESUME_HANDOFF", ""),
                       help="note to inject verbatim into the wake-up prompt")
    p_arm.add_argument("--handoff-file", default=None)
    p_arm.add_argument("--conversation-id", default=None,
                       help="resume this session id instead of a fresh one")
    p_arm.add_argument("--settle", type=int,
                       default=int(_env_or("RESUME_SETTLE", DEFAULT_SETTLE)))
    p_arm.add_argument("--timeout", type=int,
                       default=int(_env_or("RESUME_TIMEOUT", DEFAULT_TIMEOUT)))
    p_arm.add_argument("--baseline-timeout", type=int, default=DEFAULT_BASELINE_TIMEOUT)
    p_arm.add_argument("--dry-run", action="store_true")
    p_arm.add_argument("--restart", action="store_true",
                       help="after arming, also trigger the proxy restart "
                            "(sudo systemctl restart mneme@<port>); without it the "
                            "restart stays a separate, deliberate step")
    p_arm.set_defaults(func=cmd_arm)

    p_watch = sub.add_parser("watch", help="the sentinel loop (run detached)")
    _add_common(p_watch)
    p_watch.add_argument("--handoff", default=_env_or("RESUME_HANDOFF", ""))
    p_watch.add_argument("--handoff-file", default=None)
    p_watch.add_argument("--conversation-id", default=None)
    p_watch.add_argument("--settle", type=int,
                         default=int(_env_or("RESUME_SETTLE", DEFAULT_SETTLE)))
    p_watch.add_argument("--timeout", type=int,
                         default=int(_env_or("RESUME_TIMEOUT", DEFAULT_TIMEOUT)))
    p_watch.add_argument("--baseline-timeout", type=int, default=DEFAULT_BASELINE_TIMEOUT)
    p_watch.add_argument("--fire-timeout", type=int, default=DEFAULT_FIRE_TIMEOUT)
    p_watch.set_defaults(func=cmd_watch)

    p_fire = sub.add_parser("fire", help="POST the wake-up prompt immediately")
    _add_common(p_fire)
    p_fire.add_argument("--handoff", default=_env_or("RESUME_HANDOFF", ""))
    p_fire.add_argument("--handoff-file", default=None)
    p_fire.add_argument("--conversation-id", default=None)
    p_fire.add_argument("--fire-timeout", type=int, default=DEFAULT_FIRE_TIMEOUT)
    p_fire.set_defaults(func=cmd_fire)

    p_check = sub.add_parser("check", help="verify prerequisites without firing")
    _add_common(p_check)
    p_check.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

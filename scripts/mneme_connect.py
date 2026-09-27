#!/usr/bin/env python3
"""Mneme Connect — one SSH tunnel to the Mneme gateway (every proxy).

Establishes a stay-alive SSH tunnel from this machine to the pod's GATEWAY
(default port 8000), then shows you the local URL. Every proxy on the pod is
reachable through that ONE URL as ``/<port>/…`` — no tunnel per instance.

Usage (install & run):
    curl -sSL -o /tmp/mneme_connect.py https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/mneme_connect.py && python3 /tmp/mneme_connect.py

    # or, to keep it around:
    curl -sSL -o ~/.local/bin/mneme-connect https://raw.githubusercontent.com/flyersean/Mneme/main/scripts/mneme_connect.py && chmod +x ~/.local/bin/mneme-connect

The gateway port on the pod is read from MNEME_GATEWAY_PORT (default 8000).

Stdlib-only (no pip installs). Requires `ssh` on this machine and the pod's SSH
key already set up.
"""

import subprocess
import sys
import os
import time
import shutil
import urllib.request
import urllib.error

GATEWAY_PORT = os.environ.get("MNEME_GATEWAY_PORT", "8000")


def banner():
    print("""
  \033[36m███╗   ███╗███╗   ██╗███████╗███╗   ███╗███████╗
  ████╗ ████║████╗  ██║██╔════╝████╗ ████║██╔════╝
  ██╔████╔██║██╔██╗ ██║█████╗  ██╔████╔██║█████╗
  ██║╚██╔╝██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══╝
  ██║ ╚═╝ ██║██║ ╚████║███████╗██║ ╚═╝ ██║███████╗
  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚══════╝\033[0m

  Connect to the Mneme gateway on a remote pod
""")


def main():
    banner()

    ssh = shutil.which("ssh")
    if not ssh:
        print("  ✗ 'ssh' not found — install openssh-client first.")
        sys.exit(1)

    # ── Pod connection details ──
    pod_ip = input("  Pod address (IP or hostname): ").strip()
    if not pod_ip:
        sys.exit(1)
    pod_port = input("  SSH port [22140]: ").strip() or "22140"
    ssh_user = input("  SSH user [root]: ").strip() or "root"
    local_port = input(f"  Local port for the gateway tunnel [{GATEWAY_PORT}]: ").strip() or GATEWAY_PORT

    # ── Build the stay-alive tunnel (to the GATEWAY, not an instance) ──
    print(f"\n  Opening stay-alive tunnel:  localhost:{local_port} → {ssh_user}@{pod_ip}:{pod_port} (pod's gateway :{GATEWAY_PORT})")
    print("  Keep this window open — the tunnel stays up until you press Ctrl+C.\n")

    err_log = "/tmp/mneme_connect_ssh.log"
    errf = open(err_log, "w")
    cmd = [
        ssh, "-N",
        "-L", f"{local_port}:localhost:{GATEWAY_PORT}",
        "-p", pod_port, f"{ssh_user}@{pod_ip}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",       # keep the link alive through idle + NAT
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf)

    # ── Wait for the tunnel to be healthy ──
    print("  Connecting", end="", flush=True)
    ok = False
    for _ in range(25):
        if proc.poll() is not None:
            break  # ssh died — will report below
        try:
            urllib.request.urlopen(f"http://localhost:{local_port}/health", timeout=3)
            ok = True
            break
        except Exception:
            print(".", end="", flush=True)
            time.sleep(1)
    print()

    if not ok:
        print("  ✗ Tunnel failed (or the gateway isn't up on the pod).")
        tail = ""
        try:
            with open(err_log) as f:
                tail = f.read()[-600:]
        except Exception:
            pass
        if tail.strip():
            print("  SSH said:\n" + tail)
        print(f"  Check the pod address, SSH port, and that the gateway is running")
        print(f"  on the pod (scripts/start_gateway.sh, port {GATEWAY_PORT}).")
        proc.terminate()
        sys.exit(1)

    # ── Show the connection settings ──
    print("  ✓ Connected.\n")
    print("  ── One connection, every proxy ──")
    print(f"  Gateway dashboard:  http://localhost:{local_port}/")
    print(f"  A proxy instance:   http://localhost:{local_port}/<port>/   (e.g. /8080/chat, /8083/memory)")
    print()
    print("  Open the dashboard in your browser. The gateway lists every proxy and")
    print("  routes /<port>/… to that instance, so all of them are reachable through")
    print("  this one URL — no separate tunnel per port.")
    print("\n  Press Ctrl+C to close the tunnel.")

    # ── Stay alive ──
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n  Tunnel closed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        sys.exit(130)

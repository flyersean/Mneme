#!/usr/bin/env python3
"""Mneme Connect — standalone SSH tunnel to a pod running Mneme.

Establishes a stay-alive SSH tunnel from this machine to a pod, then shows you
the local URLs to use. No agent setup, no Pi/Hermes config — just the tunnel.

Usage (install & run):
    curl -sSL -o /tmp/mneme_connect.py https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_connect.py && python3 /tmp/mneme_connect.py

    # or, to keep it around:
    curl -sSL -o ~/.local/bin/mneme-connect https://raw.githubusercontent.com/flyersean/Mneme/unified_mneme/scripts/mneme_connect.py && chmod +x ~/.local/bin/mneme-connect

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


def banner():
    print("""
  \033[36m███╗   ███╗███╗   ██╗███████╗███╗   ███╗███████╗
  ████╗ ████║████╗  ██║██╔════╝████╗ ████║██╔════╝
  ██╔████╔██║██╔██╗ ██║█████╗  ██╔████╔██║█████╗
  ██║╚██╔╝██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══╝
  ██║ ╚═╝ ██║██║ ╚████║███████╗██║ ╚═╝ ██║███████╗
  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚══════╝\033[0m

  Connect to Mneme on a remote pod
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
    local_port = input("  Local port for the tunnel [8080]: ").strip() or "8080"

    # ── Build the stay-alive tunnel ──
    print(f"\n  Opening stay-alive tunnel:  localhost:{local_port} → {ssh_user}@{pod_ip}:{pod_port} (pod's :8080)")
    print("  Keep this window open — the tunnel stays up until you press Ctrl+C.\n")

    err_log = "/tmp/mneme_connect_ssh.log"
    errf = open(err_log, "w")
    cmd = [
        ssh, "-N",
        "-L", f"{local_port}:localhost:8080",
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
        print("  ✗ Tunnel failed (or the proxy isn't up on the pod).")
        tail = ""
        try:
            with open(err_log) as f:
                tail = f.read()[-600:]
        except Exception:
            pass
        if tail.strip():
            print("  SSH said:\n" + tail)
        print(f"  Check the pod address, SSH port, and that the proxy is running on the pod at :8080.")
        proc.terminate()
        sys.exit(1)

    # ── Show the connection settings ──
    print("  ✓ Connected.\n")
    print("  ── Use these on THIS machine ──")
    print(f"  OpenAI API base:   http://localhost:{local_port}/v1")
    print(f"  Chat app:          http://localhost:{local_port}/")
    print(f"  Prompt editor:     http://localhost:{local_port}/instructions")
    print()
    print("  Open either URL in your browser, or point any OpenAI-compatible")
    print("  client (Pi, Hermes, Open WebUI, ...) at the API base URL above.")
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

#!/usr/bin/env python3
"""Mneme Connect — SSH tunnel to a pod running Mneme, then launch Pi or Hermes.

Usage:
    python3 mneme_connect.py
    # or
    curl -sSL <url> | python3 -
"""

import subprocess, sys, os, tempfile, time, json, shutil

try:
    import questionary
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "questionary", "-q"], check=True)
    import questionary

def check_cmd(cmd):
    return shutil.which(cmd) is not None

print("""
  \033[36m███╗   ███╗███╗   ██╗███████╗███╗   ███╗███████╗
  ████╗ ████║████╗  ██║██╔════╝████╗ ████║██╔════╝
  ██╔████╔██║██╔██╗ ██║█████╗  ██╔████╔██║█████╗
  ██║╚██╔╝██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══╝
  ██║ ╚═╝ ██║██║ ╚████║███████╗██║ ╚═╝ ██║███████╗
  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚══════╝\033[0m

  Connect to Mneme on a remote pod
""")

# ── Step 1: Pod connection ──
pod_ip = questionary.text(
    "Pod address (IP or hostname):",
    default="69.30.85.102"
).ask()
if not pod_ip: sys.exit(1)

pod_port = questionary.text(
    "SSH port:",
    default="22140"
).ask()
if not pod_port: sys.exit(1)

ssh_user = questionary.text(
    "SSH user:",
    default="root"
).ask()
if not ssh_user: sys.exit(1)

# ── Step 2: Agent choice ──
agent_choice = questionary.select(
    "Choose AI agent:",
    choices=[
        "Hermes (CLI agent with profiles)",
        "Pi (terminal coding agent)",
    ]
).ask()
if not agent_choice: sys.exit(1)

use_hermes = "Hermes" in agent_choice
use_pi = "Pi" in agent_choice

hermes_profile = None
if use_hermes:
    # Discover profiles
    profiles = ["deep1", "default"]
    if check_cmd("hermes"):
        try:
            r = subprocess.run(["hermes", "profiles", "list"], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                parts = line.strip().split()
                if parts and not line.startswith("Profile") and not line.startswith("---"):
                    profiles.append(parts[0])
            profiles = list(dict.fromkeys(profiles))  # dedupe
        except:
            pass
    
    hermes_profile = questionary.select(
        "Hermes profile:",
        choices=profiles + ["Custom (enter name)"],
    ).ask()
    
    if hermes_profile and "Custom" in hermes_profile:
        hermes_profile = questionary.text("Profile name:").ask()
    
    if not hermes_profile: sys.exit(1)

# ── Step 3: Local port ──
local_port = questionary.text(
    "Local port for tunnel:",
    default="8080"
).ask()
if not local_port: sys.exit(1)

# ── Build tunnel ──
print(f"\nOpening SSH tunnel: localhost:{local_port} → {pod_ip}:{pod_port}:8080\n")

# Use SSH with -f -N to background the tunnel
tunnel_cmd = [
    "ssh", "-f", "-N",
    "-L", f"{local_port}:localhost:8080",
    "-p", pod_port, f"{ssh_user}@{pod_ip}",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=30",
    "-o", "ExitOnForwardFailure=yes",
]

r = subprocess.run(tunnel_cmd, capture_output=True, text=True)
if r.returncode != 0:
    print(f"SSH tunnel failed: {r.stderr}")
    print("Check your pod address, port, and SSH key setup.")
    sys.exit(1)

# Test tunnel
print("Testing tunnel...", end=" ", flush=True)
for _ in range(10):
    time.sleep(1)
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"http://localhost:{local_port}/health", timeout=3)
        data = json.loads(resp.read())
        print(f"OK! ({data.get('chunks',0)} chunks, backend: {data.get('backend','?')})")
        break
    except:
        continue
else:
    print("tunnel open but proxy not responding. Continuing anyway...")

# ── Configure agent ──
print()

if use_hermes:
    # Configure Hermes to use the tunnel
    if not check_cmd("hermes"):
        print("Hermes not found. Install with: pip install hermes-agent")
        sys.exit(1)
    
    subprocess.run(["hermes", "config", "set", "model.default", "text-mneme:64k"], capture_output=True)
    subprocess.run(["hermes", "config", "set", "model.provider", "custom"], capture_output=True)
    subprocess.run(["hermes", "config", "set", "model.base_url", f"http://localhost:{local_port}/v1"], capture_output=True)
    subprocess.run(["hermes", "config", "set", "model.api_key", "none"], capture_output=True)
    
    print(f"\033[32m✓ Hermes configured for Mneme at localhost:{local_port}\033[0m")
    print(f"  Profile: {hermes_profile}")
    print(f"\nLaunching Hermes...\n")
    os.execvp("hermes", ["hermes", "--profile", hermes_profile])

elif use_pi:
    # Configure Pi to use the tunnel
    if not check_cmd("pi") and not check_cmd("node"):
        print("Pi not found. Install Node.js 22+ and: npm install -g @earendil-works/pi-coding-agent")
        sys.exit(1)
    
    pi_dir = os.path.expanduser("~/.pi/agent")
    os.makedirs(pi_dir, exist_ok=True)
    
    pi_config = {
        "providers": {
            "mneme": {
                "baseUrl": f"http://localhost:{local_port}/v1",
                "api": "openai-completions",
                "apiKey": "none",
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [{"id": "text-mneme:64k", "name": "Mneme (remote pod)", "contextWindow": 32000, "reasoning": False}]
            }
        }
    }
    with open(os.path.join(pi_dir, "models.json"), "w") as f:
        json.dump(pi_config, f, indent=2)
    
    # Check for search_memory extension
    ext_path = os.path.expanduser("~/mneme-search-tool.ts")
    ext_flag = f" --extension {ext_path}" if os.path.exists(ext_path) else ""
    if not os.path.exists(ext_path):
        print("  Tip: copy extensions/pi/mneme-search-tool.ts to ~/ for search_memory tool")
    
    print(f"\033[32m✓ Pi configured for Mneme at localhost:{local_port}\033[0m")
    print(f"\nLaunching Pi...\n")
    
    pi_cmd = ["pi", "--provider", "mneme", "--model", "text-mneme:64k"]
    if os.path.exists(ext_path):
        pi_cmd.extend(["--extension", ext_path])
    
    os.execvp("pi", pi_cmd)

print("\nDisconnect: kill the SSH tunnel or close this terminal.")

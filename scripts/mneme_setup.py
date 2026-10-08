#!/usr/bin/env python3
"""Mneme — unified setup wizard (one wizard, every backend).

Walks the user through ONE flow that configures the whole system:
  1. Backend   → OpenRouter (hosted) or Ollama (local)
  2. Models    → chat / embedder / labeler (per backend)
  3. Pi        → optional terminal assistant (or use the built-in chat / any client)
  4. Port      → the HTTP port the proxy listens on

Then writes ONE config file (mneme.yaml), a start script, and launches the proxy.

Runs as a standalone script after the installer. It imports only PyYAML beyond the
standard library, and install.sh installs PyYAML, so it can be fetched and run with
no separate pip step:

  curl -sSL -o /tmp/setup.py https://raw.githubusercontent.com/flyersean/Mneme/<branch>/scripts/mneme_setup.py && python3 /tmp/setup.py

(If you run it on a machine where the installer hasn't run, install PyYAML first:
`pip install pyyaml` or your distro's python3-yaml package.)
"""

import os
import sys
import json
import time
import shutil
import socket
import getpass
import hashlib
import subprocess
import urllib.request
import urllib.error
import re
import yaml

# ── Defaults ─────────────────────────────────────────────────────
DEFAULT_PORT = 8080
MEMORY_DIR = os.environ.get("MNEME_CHUNK_DIR", os.path.expanduser("~/mneme/chunks"))
KEY_FILE = os.environ.get("MNEME_KEY_FILE", os.path.expanduser("~/mneme/env"))
_REPO_DERIVED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = _REPO_DERIVED  # overwritten by main() → find_repo() (resolves standalone installs)

# OpenRouter defaults (hosted)
OR_BASE = "https://openrouter.ai/api/v1"
OR_DEFAULT_MAIN = "deepseek/deepseek-v4-flash"
OR_DEFAULT_EMBED = "qwen/qwen3-embedding-8b"          # 1024-dim (MRL-truncatable)
OR_DEFAULT_LABEL = "meta-llama/llama-3.2-3b-instruct"  # non-thinking

# Ollama defaults (local)
OL_DEFAULT_EMBED = "qwen3-embedding:8b"   # 1024-dim (MRL-truncatable)
OL_DEFAULT_LABEL = "qwen2.5:1.5b"              # small non-thinking labeler (better labels than 0.5b)

# Hosted OpenAI-compatible providers (mirrors the proxy's PROVIDER_CATALOG). All
# speak the same wire format — only base_url + key_env + model ids differ. Order
# matters: index 0 is the default hosted pick.
HOSTED_PROVIDERS = [
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("openai",     "OpenAI",     "https://api.openai.com/v1",    "OPENAI_API_KEY"),
    ("anthropic",  "Anthropic",  "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    ("google",     "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "GOOGLE_API_KEY"),
    ("deepseek",   "DeepSeek",   "https://api.deepseek.com",      "DEEPSEEK_API_KEY"),
    ("groq",       "Groq",       "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ("mistral",    "Mistral",    "https://api.mistral.ai/v1",     "MISTRAL_API_KEY"),
    ("xai",        "xAI",        "https://api.x.ai/v1",           "XAI_API_KEY"),
    ("together",   "Together AI", "https://api.together.xyz/v1",  "TOGETHER_API_KEY"),
    ("routeway",   "Routeway",   "https://api.routeway.ai/v1",    "ROUTEWAY_API_KEY"),
    ("featherless","Featherless","https://api.featherless.ai/v1", "FEATHERLESS_API_KEY"),
]
# Local OpenAI-compatible servers (no key, models are pre-loaded at server start).
LOCAL_OPENAI_PROVIDERS = [
    ("vllm",     "vLLM",      "http://localhost:8000/v1", ""),
    ("llamacpp", "llama.cpp", "http://localhost:8080/v1", ""),
]

# The full per-role provider menu = every catalog entry, in one list. This MUST
# mirror the proxy's PROVIDER_CATALOG (proxy/mneme_proxy.py) — that dict is what
# the chat page's model picker renders, and setup must offer the same list or the
# two drift apart (the exact bug this wizard is fixing). Ollama is appended last
# because it is the only entry with a custom (pull-list) model flow. Each entry
# is (provider_id, label, base_url, key_env, kind).
ALL_PROVIDERS = (
    [(p, lbl, base, ke, "openai") for (p, lbl, base, ke) in HOSTED_PROVIDERS]
    + [(p, lbl, base, ke, "openai") for (p, lbl, base, ke) in LOCAL_OPENAI_PROVIDERS]
    + [("ollama", "Ollama (local — pulled to this machine)", "", "", "ollama")]
)


def _provider_menu():
    """Labels for the per-role provider picker, in ALL_PROVIDERS order.

    Hosted providers get a "(hosted)" tag and keyless local servers a "(local)"
    tag; Ollama's label already says "(local — ...)" so it is left as-is."""
    labels = []
    for (_p, lbl, _b, ke, kind) in ALL_PROVIDERS:
        if kind == "ollama":
            labels.append(lbl)
        elif ke:
            labels.append(f"{lbl} (hosted)")
        else:
            labels.append(f"{lbl} (local)")
    return labels



def run(cmd, timeout=None):
    """Run a shell command. Never raises on timeout — returns a CompletedProcess
    with returncode 124 (the `timeout` convention) and a stderr note instead, so
    callers that check returncode see a clean failure and this never crashes the
    wizard with a raw traceback."""
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def _retry_run(cmd, timeout, retries=3, label=None):
    """Run cmd, retrying on failure/timeout with exponential backoff. Returns the
    final result (returncode 0 means it succeeded on some attempt)."""
    last = None
    for attempt in range(retries):
        last = run(cmd, timeout=timeout)
        if last.returncode == 0:
            return last
        if attempt < retries - 1:
            print(f"    ⚠ {label or cmd} failed (rc={last.returncode}) — retrying ({attempt + 1}/{retries - 1})")
            time.sleep(2 ** attempt)
    return last


def ask(prompt, default=None):
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


def choose(prompt, options):
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"Choice (1-{len(options)}): ").strip()
        try:
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return idx
        except Exception:
            pass
        print(f"  Enter 1-{len(options)}")


def banner():
    print("""
  \033[36m███╗   ███╗███╗   ██╗███████╗███╗   ███╗███████╗
  ████╗ ████║████╗  ██║██╔════╝████╗ ████║██╔════╝
  ██╔████╔██║██╔██╗ ██║█████╗  ██╔████╔██║█████╗
  ██║╚██╔╝██║██║╚██╗██║██╔══╝  ██║╚██╔╝██║██╔══╝
  ██║ ╚═╝ ██║██║ ╚████║███████╗██║ ╚═╝ ██║███████╗
  ╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚═╝╚══════╝\033[0m

  Conversational memory proxy — setup wizard
""")


def detect_branch(repo_root):
    """Which repo branch is installed? Drives the memory-only default and the Pi
    extension download URL. Prefers the git branch of the cloned repo; falls back
    to MNEME_BRANCH (set by the README's install command), then agent-harness."""
    if repo_root and os.path.isdir(os.path.join(repo_root, ".git")):
        r = run(f"git -C {repo_root} rev-parse --abbrev-ref HEAD", timeout=10)
        b = (r.stdout or "").strip()
        if b and b != "HEAD":  # detached HEAD (tarball install) -> fall back
            return b
    return os.environ.get("MNEME_BRANCH", "agent-harness")


def find_repo():
    """Locate the repo (needs proxy/mneme_proxy.py). Tries known paths, then clones."""
    candidates = [
        _REPO_DERIVED,
        os.path.expanduser("~/mneme/repo"),
        os.getcwd(),
    ]
    for c in candidates:
        if c and os.path.exists(os.path.join(c, "proxy", "mneme_proxy.py")):
            return c
    print("\n  Mneme proxy code not found. Run the installer first:")
    print("    curl -sSL https://raw.githubusercontent.com/flyersean/Mneme/agent-harness/scripts/install.sh | MNEME_BRANCH=agent-harness bash")
    print("  ...or enter the repo path below (blank to git-clone it now).")
    path = input("  Repo path [clone]: ").strip()
    if path:
        if os.path.exists(os.path.join(path, "proxy", "mneme_proxy.py")):
            return path
        print(f"  proxy/mneme_proxy.py not found in {path}.")
        sys.exit(1)
    dest = os.path.expanduser("~/mneme/repo")
    print(f"  Cloning into {dest} ...")
    _br = os.environ.get("MNEME_BRANCH", "agent-harness")
    r = run(f"git clone --depth 1 -b {_br} https://github.com/flyersean/Mneme.git {dest}", timeout=300)
    if r.returncode != 0:
        print(f"  ✗ Clone failed: {r.stderr[:300]}")
        sys.exit(1)
    return dest


# ── OpenRouter backend ──────────────────────────────────────────
def or_get(key, path):
    req = urllib.request.Request(OR_BASE + path, headers={"Authorization": f"Bearer {key}"})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return None
    except Exception:
        return None


def load_saved_keys():
    """Return {KEY_ENV: value} for every key currently in the env file.

    The env file is shared across roles/providers, so it can hold several
    KEY=value lines (e.g. OPENROUTER_API_KEY and ANTHROPIC_API_KEY). Read all of
    them so the wizard can offer "use the saved key?" per role.
    """
    keys = {}
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, val = line.split("=", 1)
                    keys[name.strip()] = val.strip().strip('"').strip("'")
        except Exception:
            pass
    return keys


def load_saved_key(key_env="OPENROUTER_API_KEY"):
    """Back-compat single-key accessor (used by the old OpenRouter path)."""
    return load_saved_keys().get(key_env, "")


def save_key(key, key_env="OPENROUTER_API_KEY"):
    """Merge `key_env=key` into the env file, PRESERVING every other line.

    This file is shared by all roles and providers, so it commonly holds several
    distinct keys (chat on OpenAI, embedder on Anthropic, ...). The previous
    implementation opened with O_TRUNC and wrote a single line, so saving a
    second provider's key silently DELETED the first — breaking exactly the
    multi-provider setups this wizard now supports. We now read-modify-write:
    replace the matching KEY= line if present, else append, leaving the rest
    untouched. Written atomically at 0600 so the key is never world-readable and
    never half-written.
    """
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    lines = []
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE) as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []
    # Drop any existing line for this exact key name (handles re-entry/replace),
    # keep every other key intact.
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key_env)}\s*=")
    kept = [ln for ln in lines if not pat.match(ln)]
    kept.append(f"{key_env}={key}")
    # Atomic write (temp + fsync + rename), created 0600 from the start.
    tmp = KEY_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(kept) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, KEY_FILE)
    # Belt and braces: if the file pre-existed with looser bits, os.open's mode
    # only applies on create, so enforce 0600 here too.
    os.chmod(KEY_FILE, 0o600)
    print(f"  Saved {key_env} to {KEY_FILE} (chmod 600).")



def ask_key(provider_label, key_env):
    """Resolve the API key for a provider, offering a saved key first.

    If `key_env` already has a value in the env file, ask ONCE whether to reuse
    it (keys expire and a saved one may be stale, so the user always gets the
    choice). Yes → reuse; No → prompt for a new one and overwrite the saved
    value. No online validation: only OpenRouter exposes a key-check endpoint,
    and validating some providers but not others is the inconsistency this
    wizard exists to remove. Returns the key (or "" if none entered).
    """
    saved = load_saved_keys().get(key_env, "")
    if saved:
        print(f"\n\033[1m{provider_label} API key\033[0m")
        ans = ask(f"  A saved {key_env} was found — use it? [Y/n]", "Y").strip().lower()
        if ans not in ("n", "no"):
            print(f"  ✓ Using saved {key_env}.")
            return saved
        print(f"  Entering a new {key_env} (replaces the saved one).")
    else:
        print(f"\n\033[1m{provider_label} API key\033[0m")
    key = getpass.getpass(f"  {key_env} (input is hidden): ").strip()
    if key:
        save_key(key, key_env)
        return key
    # Nothing entered: fall back to any saved value rather than leaving it empty.
    return saved



CTX_PRESETS = [
    ("32K", 32000),
    ("64K", 64000),
    ("128K", 128000),
    ("200K", 200000),
    ("1M (frontier — e.g. stealth/ox-alpha)", 1000000),
    ("Custom (enter any token count)", None),
]


def pick_context_window(default=64000):
    """Pick the model's context window.

    Used for both Ollama (a derived Modelfile pins num_ctx to this) and OpenRouter
    (frontier models span wildly different windows — pick one matching the MODEL's
    capability, not the proxy default, or the context-budget math will be off).
    """
    idx = choose("Context window (match the model's capability)", [c[0] for c in CTX_PRESETS])
    val = CTX_PRESETS[idx][1]
    if val is None:  # custom
        raw = ask("Context window in tokens", str(default))
        try:
            return max(2048, int(str(raw).replace(",", "").replace("_", "")))
        except ValueError:
            print(f"  → not a number; using {default}")
            return default
    return val


def _budget_parts(ctx_tokens):
    """Split the context window into (completion_reserve, tool_followup_tokens).

    Both scale with the window, so the recent-context slice (ctx - reserve - tool)
    is always the remainder and the three input consumers (memory injection +
    recent window + tool results) can never sum past the model's limit. Floored at
    2048 so tiny models still get sane slices."""
    reserve = max(2048, int(ctx_tokens) // 8)
    tool = max(2048, int(ctx_tokens) // 6)
    return reserve, tool


def _or_model_available(model_id, timeout=10):
    """Check an OpenRouter model actually has a live endpoint.

    A model id can exist in the catalogue while having ZERO endpoints (retired, or
    a stealth model that has rotated out) — and picking one produces a config that
    fails on the first message with a confusing provider error. Checking at setup
    time turns that into a clear message at the point of choice.

    Uses urllib (stdlib) like the rest of this script — it must run via
    `curl | python3` with only PyYAML installed, so `requests` is not available.

    Returns (known_bool, endpoint_count) — known_bool is False when the lookup
    itself failed (offline, API change) so we never block on our own error.
    """
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1/models/{model_id}/endpoints",
        headers={"Accept": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        payload = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError,
            OSError, ValueError, TypeError):
        return False, 0
    data = (payload or {}).get("data") or {}
    return True, len(data.get("endpoints") or [])


def _warn_if_model_dead(model_id, label="model"):
    """Non-blocking availability warning for a chosen OpenRouter model id."""
    known, n = _or_model_available(model_id)
    if not known:
        return  # couldn't tell — don't cry wolf
    if n == 0:
        print(f"  ⚠ '{model_id}' is listed by OpenRouter but currently has NO live")
        print(f"    endpoints (retired, or a stealth model that rotated out).")
        print(f"    The {label} will fail on the first message. Choose another id, or")
        print(f"    update it later in mneme.yaml (providers.<name>.{label}).")
    else:
        print(f"  ✓ {model_id} — {n} endpoint(s) live")


def _pick_role(role_label, default_model, allow_ollama_list=True):
    """Collect (provider, base_url, key_env, model) for ONE model role.

    Every role — chat, embedder, labeler — runs this identical three-step flow:

      1. Provider: the full catalog (same list the chat page's picker shows).
      2. Key: if the provider has a key_env, resolve it via ask_key() (offers any
         saved key, else prompts). Local providers (vLLM/llama.cpp/Ollama) have
         no key and skip this.
      3. Model: free text. The ONLY exception is an Ollama CHAT pick, which uses
         the pulled-list + "enter name" menu and pulls the model if it is not
         already present (that flow works and is kept).

    `default_model` is offered as the press-Enter default. `allow_ollama_list`
    is False for the embedder/labeler, which are free-text-with-default even on
    Ollama (pressing Enter takes the default; typing takes your choice).

    Returns a dict: {provider, provider_label, base_url, key_env, model}.
    """
    print(f"\n\033[1m── {role_label} ──\033[0m")
    idx = choose(f"{role_label} provider", _provider_menu())
    provider, provider_label, base_url, key_env, kind = ALL_PROVIDERS[idx]

    # 2. Key (hosted providers only).
    if key_env:
        ask_key(provider_label, key_env)

    # 3. Model.
    if kind == "ollama" and allow_ollama_list:
        # Chat on Ollama: pulled-list + enter-name, then pull if missing.
        model = setup_ollama_chat_model()
    else:
        model = ask(f"{role_label} model id", default_model) or default_model
    return {"provider": provider, "provider_label": provider_label,
            "base_url": base_url, "key_env": key_env, "model": model}


def setup_ollama_chat_model():
    """Pick (and pull) an Ollama CHAT model via the pulled-list + enter-name menu.

    Kept from the original local flow: models already pulled are listed for
    one-tap reuse; anything not present is pulled on selection; "enter name"
    takes an arbitrary Ollama model. Returns the model name."""
    ensure_ollama()
    pulled = get_pulled_models()
    entries = []
    if pulled:
        entries.append(("── Already pulled ──", None))
        for p in pulled:
            entries.append((f"{p}  (pulled)", p))
    entries.append(("── Pull a recommended model ──", None))
    entries += [
        ("qwen3:32b  (strong general model)", "qwen3:32b"),
        ("qwen3:14b  (lighter)", "qwen3:14b"),
        ("llama3.1:8b  (small)", "llama3.1:8b"),
    ]
    entries.append(("Enter a model name", "__custom__"))
    model = _menu("Chat model", entries)
    if model == "__custom__":
        model = ask("Enter Ollama model name") or "qwen3:32b"
    pull_model(model)   # no-op if already present
    return model



# ── Ollama backend ──────────────────────────────────────────────
def ensure_ollama():
    """Make sure ollama is installed and serving. Returns True on success."""
    if not shutil.which("ollama"):
        print("  Ollama not found — installing...")
        _retry_run("curl -fsSL https://ollama.com/install.sh | sh", timeout=300,
                   retries=2, label="Ollama install")
    if not shutil.which("ollama"):
        print("  ✗ Ollama install failed. Run: curl -fsSL https://ollama.com/install.sh | sh")
        return False
    if run("curl -s --max-time 2 http://localhost:11434 >/dev/null", timeout=5).returncode != 0:
        print("  Starting ollama serve...")
        # Mirror the install.sh systemd drop-in here: this fallback runs when Ollama
        # is NOT under systemd (e.g. RunPod images), so without this the serve
        # process inherits none of the OLLAMA_* settings. keep_alive=-1 keeps models
        # resident; flash_attention=0 matches the installer's default (OFF, because
        # some vision-patched GGUF models crash with "CUDA illegal memory access" on
        # long prompts when it is ON — set 1 only if your model is unaffected);
        # sched_spread=1 spreads models across ALL GPUs instead of packing them onto
        # GPU 0 (the second A40 would otherwise sit idle at 0%).
        _env = os.environ.copy()
        _env["OLLAMA_KEEP_ALIVE"] = "-1"
        _env["OLLAMA_FLASH_ATTENTION"] = "0"
        _env["OLLAMA_SCHED_SPREAD"] = "1"
        subprocess.Popen(["ollama", "serve"], env=_env, stdout=open("/tmp/ollama.log", "ab"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(20):
            if run("curl -s --max-time 2 http://localhost:11434 >/dev/null", timeout=5).returncode == 0:
                break
            time.sleep(1)
        if run("curl -s --max-time 2 http://localhost:11434 >/dev/null", timeout=5).returncode != 0:
            print("  ⚠ ollama serve did not come up within 20s — see /tmp/ollama.log")
            return False
    return True


def get_pulled_models():
    out = run("ollama list", timeout=10).stdout
    models = []
    for line in out.splitlines():
        parts = line.split()
        if not parts or "NAME" in line:
            continue
        models.append(parts[0])
    return models


def _ollama_base():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/")


def _fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def _pull_model_stream(name):
    """Pull `name` via Ollama's /api/pull stream, rendering a live progress bar
    (current layer) with cumulative bytes downloaded and speed. Raises on failure
    so the caller can fall back to the CLI."""
    url = f"{_ollama_base()}/api/pull"
    req = urllib.request.Request(
        url, data=json.dumps({"name": name}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=900)

    tty = sys.stdout.isatty()
    layer_done = {}        # digest -> completed bytes (cumulative across layers)
    cur_total = cur_done = 0   # current layer's total / completed
    layers = 0
    speed = 0.0
    last_done = 0
    last_t = time.time()
    last_pct = -1.0
    status = ""
    saw_success = False   # set only when Ollama emits the terminal "success" event

    def downloaded():
        return sum(layer_done.values())

    def render():
        nonlocal last_pct
        dl = downloaded()
        if cur_total > 0:
            frac = min(1.0, cur_done / cur_total)
            pct = frac * 100
            if tty:
                filled = int(frac * 30)
                bar = "█" * filled + "░" * (30 - filled)
                spd = _fmt_bytes(speed) + "/s" if speed > 0 else "         "
                sys.stdout.write(
                    f"\r  {name}: [{bar}] {pct:5.1f}%  layer {layers}  "
                    f"{_fmt_bytes(dl)} downloaded  {spd}   "
                )
                sys.stdout.flush()
            elif abs(pct - last_pct) >= 10:
                print(f"  {name}: {pct:5.1f}%  layer {layers}  "
                      f"{_fmt_bytes(dl)} downloaded  {_fmt_bytes(speed)}/s")
                last_pct = pct
        elif status and tty:
            sys.stdout.write(f"\r  {name}: {status}...                    ")
            sys.stdout.flush()

    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = ev.get("status", "") or status
        # A real failure is an explicit error event — Ollama sends {"error": "..."},
        # NOT the absence of a "success" event. Treating stream-end as failure is
        # what broke hf.co/ pulls: HF manifest resolution stalls the event stream
        # right after "pulling manifest", the for-loop below ends, and the old
        # code raised a phantom error on a pull that was still running.
        err = ev.get("error")
        if err:
            raise RuntimeError(f"ollama reported: {err}")
        digest = ev.get("digest", "")
        total = ev.get("total") or 0
        completed = ev.get("completed") or 0
        if digest and total:
            if digest not in layer_done:
                layers += 1
            cur_total, cur_done = total, completed
            layer_done[digest] = completed
            now = time.time()
            dt = now - last_t
            if dt >= 0.1:
                dl = downloaded()
                speed = (dl - last_done) / dt
                last_done, last_t = dl, now
        render()
        if status == "success":
            saw_success = True
            break

    if tty:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
    if saw_success:
        print(f"  ✓ {name} pulled ({_fmt_bytes(downloaded())}).")
    else:
        # Stream ended without a success event and without an error event. The
        # pull may still be running server-side, so don't claim failure — verify.
        if name in get_pulled_models():
            print(f"  ✓ {name} pulled.")
        else:
            raise RuntimeError(
                f"stream ended before completion (last status: {status or 'unknown'})"
            )


def pull_model(name):
    if name in get_pulled_models():
        print(f"  {name} already pulled — skipping.")
        return
    print(f"  Pulling {name}...")
    try:
        _pull_model_stream(name)
        return
    except Exception as e:
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(f"    (streaming progress unavailable: {e} — falling back to `ollama pull`)")
    # Fail loud: surface what Ollama actually said instead of guessing at a cause.
    # `run()` captures output, so print it — a silent discard here is what turned
    # a working hf.co/ pull into an unexplained "may be a typo or network" warning.
    r = run(f"ollama pull {name}", timeout=900)
    if r.returncode == 0:
        print(f"  ✓ {name} pulled.")
    elif name in get_pulled_models():
        # The pull can complete server-side even when we couldn't read the stream.
        print(f"  ✓ {name} pulled.")
    else:
        detail = (r.stderr or r.stdout or "").strip()
        if detail:
            print(f"  ⚠ could not pull {name}:")
            for line in detail.splitlines()[-5:]:
                print(f"      {line}")
        else:
            print(f"  ⚠ could not pull {name} (exit {r.returncode}) — continuing")


def _menu(prompt, entries):
    """entries: list of (label, value). Entries with value=None are non-selectable
    section headers, printed without a number. Only real options get numbered.
    Returns the chosen value."""
    print(f"\n{prompt}")
    selectable = []
    n = 0
    for label, value in entries:
        if value is None:
            print(f"  {label}")
        else:
            n += 1
            selectable.append(value)
            print(f"  {n}. {label}")
    while True:
        try:
            val = input(f"Choice (1-{n}): ").strip()
        except EOFError:
            print()
            sys.exit(1)
        try:
            i = int(val)
            if 1 <= i <= n:
                return selectable[i - 1]
        except ValueError:
            pass
        print(f"  Enter 1-{n}")


_OLLAMA_MAX_NAME_LEN = 80  # measured: ollama 0.34.x rejects model names > 80 chars


def _cap_model_name(name, max_len=_OLLAMA_MAX_NAME_LEN):
    """Cap an auto-generated Ollama model name at the length Ollama accepts.

    Ollama rejects model names longer than 80 chars ("invalid model name"). The
    wizard derives names from base-model paths (HF paths, quant tags), which can
    easily exceed that. When one would, truncate and append a stable 8-hex hash
    of the FULL name so the result stays valid, deterministic, and collision-free
    — two different long base names never collapse onto the same capped name.
    """
    if len(name) <= max_len:
        return name
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    keep = max_len - 1 - len(h)
    return f"{name[:keep]}-{h}"


def _derived_model_name(base_model, ctx_size):
    """Deterministic derived-model name keyed on the base model + context window
    (NOT the port). Instances sharing a base model + context then share one
    derived name, so Ollama keeps a single resident copy of the weights instead
    of one per port. ':' '/' '.' are sanitized to '-'; a short hash of the RAW
    base model is included so two distinct models that sanitize identically
    (e.g. org/Foo.Bar vs org/Foo/Bar) never collide."""
    frag = re.sub(r"[^a-zA-Z0-9]+", "-", base_model).strip("-").lower()
    h = hashlib.sha1((base_model or "").encode("utf-8")).hexdigest()[:6]
    return _cap_model_name(f"mneme-chat-{frag}-{h}-{int(ctx_size) // 1000}k")


def create_context_modelfile(base_model, ctx_size, name=None):
    """Create a derived Ollama model with an explicit context window, so num_ctx
    matches what the proxy expects. `name` is auto-derived from the base model +
    context window when omitted, so instances pointing at the SAME base + context
    collapse onto one derived model (one resident copy in VRAM). Returns the
    derived model name on success, or the base model name if `ollama create`
    fails."""
    if not shutil.which("ollama"):
        return base_model
    if name is None:
        name = _derived_model_name(base_model, ctx_size)
    mf = os.path.join(MEMORY_DIR, f"Modelfile.{name}")
    content = (
        f"# Mneme — derived from {base_model} (quantized base)\n"
        f"# Pins the context window to match sampling.ctx_tokens in mneme.yaml.\n"
        f"FROM {base_model}\n"
        f"PARAMETER num_ctx {ctx_size}\n"
    )
    try:
        with open(mf, "w") as f:
            f.write(content)
        r = run(f"ollama create {name} -f {mf}", timeout=300)
        if r.returncode == 0:
            print(f"  ✓ created '{name}' (num_ctx={ctx_size}) from {base_model}")
            return name
    except Exception:
        pass
    print(f"  ⚠ could not create Modelfile — using {base_model} as-is")
    return base_model


# ── Pi (optional terminal assistant) ────────────────────────────
def _log_node_version():
    v = run("node --version", timeout=10).stdout.strip()
    print(f"    ✓ Node.js {v}" if v else "    ✓ Node.js installed")


def _install_node_tarball(version="22.14.0"):
    """Fallback install: download the official Node binary tarball and extract it
    to /usr/local. No apt or distro dependency — the most portable path, used when
    the NodeSource/apt route fails."""
    machine = os.uname().machine if hasattr(os, "uname") else ""
    arch = "arm64" if machine == "aarch64" else "x64"  # x86_64/amd64/anything -> x64
    url = f"https://nodejs.org/dist/v{version}/node-v{version}-linux-{arch}.tar.xz"
    tarball = "/tmp/node.tar.xz"
    print(f"    downloading {url} ...")
    if run(f"curl -fsSL {url} -o {tarball}", timeout=300).returncode != 0:
        print("    ✗ failed to download the Node tarball")
        return False
    if run(f"tar -xJf {tarball} -C /usr/local --strip-components=1", timeout=120).returncode != 0:
        print("    ✗ failed to extract the Node tarball")
        return False
    return shutil.which("node") is not None


def _ensure_node():
    """Install Node.js 22 if missing. Returns True if node is available afterward."""
    if shutil.which("node"):
        return True
    print("  Node.js not found — installing Node 22...")

    # 1. Fresh apt lists + the tools the NodeSource script needs (best-effort).
    run("apt-get update -y", timeout=300)
    run("apt-get install -y ca-certificates curl gnupg", timeout=300)

    # 2. NodeSource repo (retry — its internal apt-get update is slow on a fresh pod).
    if _retry_run("curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
                  timeout=240, retries=3, label="NodeSource setup").returncode == 0:
        run("apt-get install -y nodejs", timeout=300)
        if shutil.which("node"):
            _log_node_version()
            return True

    # 3. Fallback: official tarball (survives NodeSource/apt breakage).
    print("    ⚠ NodeSource/apt path failed — falling back to the official Node tarball")
    if _install_node_tarball():
        _log_node_version()
        return True

    print("    ✗ Node.js could not be installed.")
    return False


def setup_pi(ctx_size, branch="agent-harness", port=8080):
    """Install Pi + write its provider config pointing at this proxy. Returns True on success.

    `port` MUST be this instance's actual port — it is chosen before this runs.
    Hardcoding 8080 produced a silently broken Pi setup for anyone who picked a
    different port.
    """
    if not _ensure_node():
        print("  ⚠ Pi install skipped — Node.js unavailable.")
        return False
    if not shutil.which("npm"):
        print("  ✗ npm not found — Pi install skipped.")
        return False
    print("  Installing Pi (terminal AI coding assistant)...")
    if _retry_run("npm install -g @earendil-works/pi-coding-agent", timeout=300,
                  retries=2, label="npm install pi").returncode != 0:
        print("  ⚠ Pi install failed — skipping. Re-run the setup wizard to retry.")
        return False

    pi_config = {
        "providers": {
            "mneme": {
                "baseUrl": f"http://localhost:{port}/v1",
                "api": "openai-completions",
                "apiKey": "none",
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [{"id": "text-mneme:64k", "name": "Mneme", "contextWindow": ctx_size or 32000, "reasoning": False}],
            }
        }
    }
    os.makedirs(os.path.expanduser("~/.pi/agent"), exist_ok=True)
    with open(os.path.expanduser("~/.pi/agent/models.json"), "w") as f:
        json.dump(pi_config, f, indent=2)

    # Download the Pi extensions into ~/.pi/mneme-extensions/ (a stable, discoverable
    # location the printed command below can reference exactly). Previously these
    # landed as loose files in $HOME while the README documented the in-repo path,
    # so the two disagreed.
    _ext_dir = os.path.expanduser("~/.pi/mneme-extensions")
    os.makedirs(_ext_dir, exist_ok=True)
    _ext_paths = []
    for name, fname in [("search_memory", "mneme-search-tool.ts"), ("web tools", "mneme-web-tools.ts")]:
        url = f"https://raw.githubusercontent.com/flyersean/Mneme/{branch}/extensions/pi/{fname}"
        dest = os.path.join(_ext_dir, fname)
        r = run(f"curl -sSL --fail -o {dest} '{url}?{int(time.time())}'")
        print(f"    {'✓' if r.returncode == 0 else '⚠'} {name} extension")
        if r.returncode == 0:
            _ext_paths.append(dest)

    print("  ✓ Pi configured → run with:")
    if _ext_paths:
        print("      pi --provider mneme --model text-mneme:64k \\")
        for i, p in enumerate(_ext_paths):
            _tail = " \\" if i < len(_ext_paths) - 1 else ""
            print(f"         --extension {p}{_tail}")
    else:
        print("      pi --provider mneme --model text-mneme:64k")
    return True


# ── Config + start script ───────────────────────────────────────
def _instance_dir(db_dir, port):
    """Per-instance config dir. Each proxy instance owns its own config + prompts
    (chunk_dir) while the memory DB lives at db_dir (shared, portable)."""
    return os.path.join(db_dir, "instances", str(port))


def _mcp_yaml(servers):
    """Render the mcp_servers block. Empty -> `mcp_servers: []`."""
    servers = [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]
    if not servers:
        return "mcp_servers: []"
    dumped = yaml.safe_dump(servers, default_flow_style=None, sort_keys=False).rstrip("\n")
    return "mcp_servers:\n" + "\n".join("  " + ln for ln in dumped.split("\n"))


def _embedder_floor(embed_model):
    """Pick an initial `inject_min_similarity` for the chosen embedder.

    This MUST be embedder-dependent: every embedding model has its own similarity
    scale, so a value that works for one injects noise for another.

    These are measured starting points, not tuned values — the README tells users
    to tune per embedder. Returns (value, note) where note documents the reasoning
    in the generated config.
    """
    m = (embed_model or "").lower()
    if "qwen" in m:
        return 0.45, "qwen3-embedding-8b — 0.45 measured starting point"
    if "voyage" in m:
        return 0.62, "voyage ~0.48 noise / ~0.70 relevant — 0.62 measured starting point"
    if "snowflake" in m:
        return 0.45, "snowflake-arctic-embed2 — 0.45 measured starting point"
    if "nomic" in m:
        return 0.55, "nomic-embed — unverified starting point; tune for your data"
    # Unknown embedder: be explicit that this is a guess, not a measurement.
    return 0.50, ("UNKNOWN EMBEDDER — this is a placeholder, not a measurement. "
                  "Tune it: see 'Tune inject_min_similarity per embedder' in the README")


def _user_templates_path():
    """User template catalogue — kept OUT of the repo (survives git pull) and in
    the shared memory dir so every instance sees the same saved templates."""
    return os.environ.get("MNEME_TEMPLATES_FILE") or os.path.join(MEMORY_DIR, "templates.yaml")


def _templates_module():
    """Import mneme.templates, adding the repo's proxy/ dir to sys.path once."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "proxy"))
    from mneme import templates as _tpl
    return _tpl


def _template_owned_keys(model_template):
    """Keys the chosen template sets, so the wizard can OMIT them from the file.

    The wizard writing `temperature: 0.2` into a config that also selects the
    gemma4-repeat template (which wants 1.0) produced a config whose stated
    settings disagreed with the effective ones. Omitting template-owned keys makes
    the generated file state the truth: what the template supplies is the
    template's to supply, and anything absent falls back to the built-in default.

    Returns (sampling_keys, notes) — sampling keys to skip, and the template's
    values for a comment so the file still documents what will be in force.
    """
    if not model_template:
        return set(), {}
    try:
        _tpl = _templates_module()
        path = _tpl.default_templates_path(REPO_ROOT)
        tpl = (_tpl.load_templates(path, _user_templates_path()) or {}).get(model_template) or {}
        return set((tpl.get("sampling") or {}).keys()), (tpl.get("sampling") or {})
    except Exception:
        return set(), {}


def _common_yaml(instance_dir, db_path, port, inject, ctx_tokens, memory_only, mcp_servers=None, hot_reload=True, model_template="", embed_model=""):
    reserve, tool = _budget_parts(ctx_tokens)
    _mcp_block = _mcp_yaml(mcp_servers)
    _hot_reload_s = "true" if hot_reload else "false"
    # Skip any sampling key the selected template owns, and say so in the file.
    _tpl_keys, _tpl_vals = _template_owned_keys(model_template)
    if _tpl_keys:
        _tpl_summary = ", ".join(f"{k}: {_tpl_vals[k]}" for k in sorted(_tpl_keys))
        _tpl_note = (f"#   supplied by the {model_template!r} template: {_tpl_summary}\n"
                     f"#   (edit these in model_templates.yaml, or delete the\n"
                     f"#    model_template line to fall back to the defaults below)\n")
    else:
        _tpl_note = ""

    def _s(key, default, comment):
        """Emit `key: default` unless the template supplies it.

        An empty `default` means the key has no built-in fallback (e.g.
        reasoning_effort), so it is emitted commented-out rather than as a bare
        `key: ` which would be invalid YAML.
        """
        if key in _tpl_keys:
            return f"  # {key}: {_tpl_vals.get(key)}  (from the template)\n"
        if default == "":
            return f"  # {key}: (unset){comment}\n"
        return f"  {key}: {default}{comment}\n"

    _floor, _floor_note = _embedder_floor(embed_model)
    # The second floor must stay BELOW the injection floor, or the strategy layer
    # can never fire. Derive it rather than hardcoding a value that silently
    # inverts if the injection floor is low.
    _strategy_floor = round(max(0.0, _floor - 0.05), 2)
    return f"""# Mneme proxy config — generated by the unified setup wizard.
# Precedence: environment variable > this file > built-in default.
# Full reference (every option + comment): mneme.yaml.example in the repo.
#
# Live-reload on save (no restart): sampling.*, models.*, storage.memory_only /
# storage.memory_enabled / storage.inject_enabled, mcp_servers, and the prompts.
# Backend/provider/model identity and backend/port/db path are restart-only.
# Set runtime.hot_reload: false to lock all of the above (restart to change).

# Model template: named, known-good generation settings for a specific model.
# Each template in model_templates.yaml bundles sampling / thinking-mode / output
# caps measured to work well for that model. TEMPLATE VALUES WIN over the settings
# below — anything the template does not set falls back to the built-in default.
# Remove this line to use only the settings in this file.
{_tpl_note}model_template: "{model_template}"

backend:
  type: @@BTYPE@@          # "ollama" | "openai" (openai = any OpenAI-compatible provider)
  provider: @@BPROV@@      # which `providers:` entry to use (ignored when type=ollama)
  ollama_url: http://localhost:11434   # used only when type=ollama

# The chat / embed / label models — authoritative for BOTH backends. The proxy
# reads these directly (mapped to MNEME_MODEL / EMBED_MODEL / LABEL_MODEL), so
# editing them here is enough: the generated start script clears the inherited
# model env vars instead of exporting them, letting these keys win. This is the
# single source of truth for model identity; the providers.openrouter.* copies
# below remain for the OpenRouter request path.
model: "@@MAIN@@"
embed_model: "@@EMBED@@"
label_model: "@@LABEL@@"

# Backend models (embedder/labeler) — pin to a DIFFERENT provider than the chat
# model (set once; the embedder defines your memory index). Empty = follow chat.
embed_provider: "@@EMBED_PROV@@"
label_provider: "@@LABEL_PROV@@"

# Backend transports — set only when a role's transport DIFFERS from the chat
# backend (e.g. chat hosted on OpenAI, embedder on local Ollama). "openai" or
# "ollama"; empty = follow the chat backend.
embed_backend: "@@EMBED_BACKEND@@"
label_backend: "@@LABEL_BACKEND@@"

providers:
  @@PROV@@:
    base_url: "@@PROV_BASE@@"
    api_key_env: "@@PROV_KEY@@"   # key read from this env var, never stored here
    model: "@@MAIN@@"
    embed_model: "@@EMBED@@"
    label_model: "@@LABEL@@"
    fallback_models: ["google/gemini-2.5-flash"]   # OpenRouter failover (ignored by other providers)
    provider: {{}}            # OpenRouter provider prefs (e.g. preferred_max_latency)
    stream: true             # false = let OpenRouter buffer + fail over

# Default generation settings. A selected model_template WINS over these; the
# per-model overrides under `models:` beat both. Keys the template supplies are
# commented out below so this file states what is actually in force.
sampling:
{_s("temperature", "0.2", "          # randomness; lower = more deterministic")}{_s("top_p", "0.9", "                # nucleus sampling — cut off the improbable tail")}{_s("top_k", "64", "                 # ollama only (ignored by the openai path)")}  ctx_tokens: {ctx_tokens}
  # max_tokens: leave unset — a global output cap truncates long summaries.
  completion_reserve: {reserve}   # reply reserve — scales with ctx (ctx/8)
  # Reasoning/thinking is OFF by default (a reasoning model can runaway-think on
  # a trivial ask). Set reasoning_enabled: 1 to opt back in; reasoning_effort
  # (low/high/max) is for effort-level models (deepseek etc.), not Qwen3.6.
{_s("reasoning_enabled", "0", "")}{_s("reasoning_effort", "", "  # low | medium | xhigh (thinking models only)")}

# Anti-hang guardrails, not tuning knobs — leave unless a provider is flaky.
timeouts:
  chat_timeout: 300
  ollama_chat_timeout: 300
  first_token_timeout: 45   # no-bytes budget before the FIRST token (fail fast on a hung provider)
  stale_chunk_timeout: 20   # no-bytes budget BETWEEN chunks once streaming has started
  novelty_timeout: 600
  embed_timeout: 60
  label_timeout: 30
  edge_failures: 2          # consecutive failures before flagging an "edge case"
  edge_ratio: 0.5           # fraction of recent failing turns that trips the edge detector

# Where things live and how memory is staged.
storage:
  chunk_dir: "{instance_dir}"
  db_path: "{db_path}"
  port: {port}
  inject_system: {inject}          # prepend Mneme's system instructions to the prompt
  memory_only: {memory_only}       # true = memory-only (strategy/learning layer off)
  memory_enabled: true             # master switch: false = NO memory (no inject, no save, no search)
  inject_enabled: true             # false = SAVE-ONLY (no injection, but save + search still work)
  staging_turns: 1                 # flush conversation to memory every N turns
  staging_idle: 120                # ...or after this many idle seconds
  context_recent_extra: 14         # recent-convo window beyond staging_turns
  belief_evolution: false          # experimental — OFF

# Memory retrieval: what gets injected into the prompt each turn.
retrieval:
  max_injected_tokens: 8000        # token budget for memory stuffed into the prompt
  # inject_min_similarity is EMBEDDER-DEPENDENT: every embedding model has its own
  # similarity scale, so this value is chosen for YOUR embedder ({embed_model}).
  #   {_floor_note}
  # Changing embedder? Re-tune this (see the README).
  inject_min_similarity: {_floor}
  strategy_min_similarity: {_strategy_floor}    # second floor (must stay below inject_min_similarity)
  keyword_fallback: false          # pad sparse FAISS with substring matches (pollutes context)
  route_threshold: 0.08            # only used by the /search debug endpoint
  baseline_noise: 0.20             # auto-calibrated at startup
  age_decay_days: 7                # recency half-life (days)
  max_siblings: 3                  # sibling chunks pulled per topic hit
  topic_switch_sim: 0.45           # below this cosine vs recent turns = a topic switch (0 = off)
  topic_switch_grace: 2            # turns to harden injection after a switch
  novel_inject_floor: 0.60         # raised floor during the switch grace window
  max_per_topic: 3                 # cap on injected chunks per topic (0 = off)
  max_chunk_words: 500             # split user messages longer than this (words)
  max_chunk_size: 10000            # chars per embed chunk; overflow becomes siblings

# Character/truncation limits. Rarely tuned.
caps:
  max_history_messages: 32
  db_msg_cap: 8000
  compress_threshold: 500
  compress_max_tok: 2048
  max_tool_forward: 12000
  tool_followup_tokens: {tool}    # tool-results slice — scales with ctx (ctx/6)
  max_server_rounds: 30           # cap on tool-loop rounds per turn (raise for long coding/agent steps)
  chunk_size: 4000

# Inbuilt tools — set any to false to hide it from the model.
# `inject_*` auto-inject relevant built-tool descriptions into context so the
# model knows a tool exists; it can always list_tools / read_tool manually.
tools:
  native: auto            # bash/write bootstrap: auto | on | off (NOT a boolean — "off" to disable)
  dir: {instance_dir}/tools   # where the model's built tools live (default <chunk_dir>/tools)
  bash_timeout: 30        # seconds before a bash command is killed
  inject_min_similarity: 0.75  # auto-inject a built tool's description if the query scores >= this
  inject_max: 3           # max built tools auto-injected per turn
  inject_tokens: 600      # token budget for injected tool descriptions
  search_memory: true     # memory search tool
  list_tools: true        # list the tools you've built
  read_tool: true         # read a built tool's source
  read_file: true         # read host files by path
  fetch_url: true         # fetch + clean a web page
  web_search: true        # web search

# Per-model generation overrides (beat the `sampling:` defaults). Keyed by the
# EXACT model name — the same string as the `model:` line above (for Ollama that
# is the MNEME_MODEL export in start_proxy.sh). NOTE: the wizard rewrites the
# Ollama model name to a derived `mneme-chat-<base>-<ctx>k` name to pin the
# context window — use THAT derived name as the key, not the name you typed.
# Uncomment + fill in per model:
#
# models:
#   "your-model-name":      # ← must match the chat model name exactly
#     reasoning: false      # false = instruct mode (think:false); true = thinking
#     reasoning_effort: low # thinking depth: low | medium | xhigh (thinking only)
#     temperature: 0.7      # randomness — use the model card's recommended sampling
#     top_p: 0.8            # nucleus sampling
#     top_k: 20             # top-k (ollama only)
#     min_p: 0.0            # min-p filtering (0.0 = off)
#     presence_penalty: 1.5 # instruct ~1.5 / thinking ~0.0 (per model card)
#     repetition_penalty: 1.0
#     num_ctx: 32768        # cap the context window to what fits your VRAM
#     num_predict: 0        # cap the reply length (0 = unlimited)
models: {{}}

# Model Context Protocol (MCP) servers — "install any tool and it just works".
# Each server's tools surface to the model like built-in tools. Hot-reloadable:
# edit this list (or POST/DELETE /mcp/servers) with no restart. stdio servers use
# command+args (the proxy spawns them); HTTP servers use a url.
{_mcp_block}

# Runtime behavior.
runtime:
  hot_reload: {_hot_reload_s}   # true = config/prompts/swarm_config live-edit (changes apply immediately)
                                # false = LOCKED — changes take effect only after a restart

# Logging — the proxy owns its own per-port log at {instance_dir}/proxy-<port>.log (append mode).
# max_entries caps EVERY log the proxy writes (proxy log, thinking.log, errors.log,
# extension logs) to the newest N lines, so none grow without bound. Set to 0 to
# turn logging off, or remove the block for no limit.
logging:
  max_entries: 200    # 0 = logging off; N = keep newest N lines
"""


def write_config(backend, models, port, inject, memory_only, instance_dir, db_path, mcp_servers=None, hot_reload=True, model_template="", provider="openrouter", base_url=OR_BASE, key_env="OPENROUTER_API_KEY", embed_provider="", label_provider="", embed_backend="", label_backend=""):
    """Write mneme.yaml for the chosen backend into this instance's config dir."""
    os.makedirs(instance_dir, exist_ok=True)
    inject_s = "true" if str(inject) == "1" else "false"
    mo_s = "true" if memory_only else "false"
    if backend == "openrouter":
        btype, bprov = "openai", provider
    else:
        btype, bprov = "ollama", ""
    # Named `cfg_text`, not `yaml` — a local called `yaml` shadows the imported
    # PyYAML module for the rest of this function, which is a landmine for any
    # later edit that needs yaml.safe_load/dump here.
    cfg_text = _common_yaml(instance_dir, db_path, port, inject_s, models.get("ctx_size", 64000), mo_s, mcp_servers, hot_reload, model_template, models.get("embed_model", ""))
    # json.dumps() escaping is YAML-compatible inside double-quoted scalars, so a
    # custom model id containing a quote/backslash can't produce malformed YAML.
    # EMBED_BACKEND / LABEL_BACKEND pin a role to a transport that DIFFERS from the
    # chat backend (e.g. chat hosted, embedder on local Ollama). Empty means
    # "follow the chat backend", which is the default.
    cfg_text = (cfg_text.replace("@@BTYPE@@", btype).replace("@@BPROV@@", bprov)
                .replace("@@PROV@@", json.dumps(provider)[1:-1])
                .replace("@@PROV_BASE@@", json.dumps(base_url)[1:-1])
                .replace("@@PROV_KEY@@", json.dumps(key_env)[1:-1])
                .replace("@@EMBED_PROV@@", json.dumps(embed_provider)[1:-1])
                .replace("@@LABEL_PROV@@", json.dumps(label_provider)[1:-1])
                .replace("@@EMBED_BACKEND@@", json.dumps(embed_backend)[1:-1])
                .replace("@@LABEL_BACKEND@@", json.dumps(label_backend)[1:-1])
                .replace("@@MAIN@@", json.dumps(models.get("model", ""))[1:-1])
                .replace("@@EMBED@@", json.dumps(models.get("embed_model", ""))[1:-1])
                .replace("@@LABEL@@", json.dumps(models.get("label_model", ""))[1:-1]))
    path = os.path.join(instance_dir, "mneme.yaml")
    # Write atomically (temp + fsync + rename) so a proxy launched right after
    # this can never read a half-written config. A partial config load silently
    # left reasoning ON and made thinking models runaway-timeout (the 12B hang).
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(cfg_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def choose_model_template():
    """Let the user pick a model template, or none (the default).

    A template bundles known-good generation settings for a specific model, so
    settings that took measurement to find don't have to be rediscovered. Any
    value it sets can still be overridden in mneme.yaml afterward — the template
    is a defaults layer, not a lock-in. Choosing none keeps the current defaults
    exactly as they were before templates existed.
    """
    try:
        _tpl = _templates_module()
        path = _tpl.default_templates_path(REPO_ROOT)
        names = _tpl.list_template_names(path, _user_templates_path())
    except Exception as e:
        print(f"  (model templates unavailable: {e})")
        return ""
    if not names:
        return ""
    entries = [("None — use the built-in default settings", "")]
    for n in names:
        try:
            d = _tpl.describe(n, path, _user_templates_path())
            entries.append((f"{n}  — {d['description']}", n))
        except Exception:
            entries.append((n, n))
    print("\nModel templates package known-good generation settings for a specific")
    print("model (sampling, thinking mode, output caps). Pick one to load them into")
    print("the config, or None to keep the current defaults. You can still override")
    print("any individual setting in mneme.yaml afterward.")
    choice = _menu("Model template", entries)
    if choice:
        try:
            d = _tpl.describe(choice, path, _user_templates_path())
            if d.get("notes"):
                print(f"  note: {d['notes']}")
            print(f"  ✓ template {choice!r} will be written as `model_template:` in mneme.yaml")
        except Exception:
            pass
    return choice


def _modelfile_model_name(model_template, chosen_model, port=None, shared=True):
    """Deterministic, non-colliding name for a Modelfile-derived model, keyed on
    the template + chosen model. By default (`shared=True`) two proxies on the
    SAME model + template derive the SAME name, so Ollama keeps ONE resident
    copy of the weights. With `shared=False` the name is further keyed on the
    instance port, so each proxy gets its OWN derived model (a per-proxy
    Modelfile) at the cost of N× VRAM."""
    tfrag = re.sub(r"[^a-zA-Z0-9]+", "-", model_template).strip("-").lower()
    mfrag = re.sub(r"[^a-zA-Z0-9]+", "-", chosen_model).strip("-").lower()
    name = f"{tfrag}-{mfrag}"
    if port is not None and not shared:
        name = f"{name}-p{port}"
    return _cap_model_name(name)


def _install_template_modelfile(model_template, backend, current_model, current_ctx,
                                port=None, instance_dir=None, shared=True):
    """If the selected template ships a custom Ollama Modelfile, build it against
    the CHOSEN model and create it. Returns (model, ctx_size) to use — the created
    model name and the Modelfile's num_ctx (falling back to the current values
    when there is nothing to install, the backend isn't Ollama, or create fails).

    The template's `from` is only a DEFAULT source (e.g. a specific quant). When
    the user already picked a model, the Modelfile is auto-edited to
    `FROM <chosen model>` instead, because the chat TEMPLATE + PARAMETERs are
    quant-agnostic — so a Q4 model can use a template authored against Q5 and
    still get the corrected chat format.
    """
    if not model_template or backend != "ollama":
        return current_model, current_ctx
    try:
        _tpl = _templates_module()
        d = _tpl.describe(model_template, _tpl.default_templates_path(REPO_ROOT),
                          _user_templates_path())
        mf = d.get("modelfile") or {}
    except Exception:
        return current_model, current_ctx
    if not mf:
        return current_model, current_ctx
    if not current_model:
        return current_model, current_ctx  # nothing pulled to build against
    src = (mf.get("from") or "").strip()
    ensure_ollama()
    # Auto-edit the Modelfile to reference the chosen model instead of the
    # template's hardcoded source (which may be a different quant).
    mf = dict(mf)
    mf["from"] = current_model
    if src and src != current_model:
        print(f"  ∎ modelfile edited to match chosen model (FROM {src} → {current_model})")
    name = _modelfile_model_name(model_template, current_model, port=port, shared=shared)
    mf_path = os.path.join(instance_dir or MEMORY_DIR, f"Modelfile.{name}")
    try:
        with open(mf_path, "w") as f:
            f.write(_tpl.render_modelfile(mf))
        r = run(f"ollama create {name} -f {mf_path}", timeout=300)
    except Exception as e:
        print(f"  ⚠ could not write/run Modelfile: {e}")
        return current_model, current_ctx
    if r.returncode != 0:
        print(f"  ⚠ could not create {name!r}: {(r.stderr or r.stdout or '')[-200:]}")
        return current_model, current_ctx
    print(f"  ✓ created {name!r} with the corrected template")
    new_ctx = current_ctx
    params = mf.get("parameters") or {}
    if params.get("num_ctx"):
        new_ctx = int(params["num_ctx"])
        print(f"  ✓ context window set to {new_ctx} to match the Modelfile")
    return name, new_ctx


def _port_free_lines():
    """Bash lines that stop any proxy already listening on $MNEME_PORT before the
    proxy starts, so re-running the start script is a clean stop-and-restart (it
    frees the port) instead of a bind conflict. Mirrors stop_proxy_on_port():
    SIGTERM, wait ~5s, then SIGKILL."""
    return [
        "# Stop any proxy already on this port, so re-running this script is a clean",
        "# stop-and-restart (frees the port) rather than a bind conflict.",
        '_PID="$(ss -ltnp 2>/dev/null | grep -E ":${MNEME_PORT}[[:space:]]" | sed -n \'s/.*pid=\\([0-9]*\\).*/\\1/p\' | head -1)"',
        'if [ -n "${_PID}" ]; then',
        '  if ! grep -q "mneme_proxy.py" "/proc/${_PID}/cmdline" 2>/dev/null; then',
        '    echo "⚠ port ${MNEME_PORT} is held by a non-Mneme process (pid ${_PID}) — NOT stopping it."',
        '  else',
        '    echo "Stopping existing proxy on port ${MNEME_PORT} (pid ${_PID})..."',
        '    kill "${_PID}" 2>/dev/null',
        '    for _i in $(seq 1 50); do',
        '      kill -0 "${_PID}" 2>/dev/null || break',
        '      sleep 0.1',
        '    done',
        '    kill -9 "${_PID}" 2>/dev/null',
        '    sleep 1',
        '  fi',
        'fi',
        '',
    ]


def write_start_script(backend, models, port, instance_dir, provider="openrouter", key_env="OPENROUTER_API_KEY", embed_provider="", label_provider=""):
    """Write a start script into this instance's config dir."""
    os.makedirs(instance_dir, exist_ok=True)
    path = os.path.join(instance_dir, "start_proxy.sh")
    lines = [
        "#!/bin/bash",
        "# Mneme proxy — generated by the unified setup wizard.",
        f"# Start/restart with:  {path}",
        "",
    ]
    if backend == "openrouter":
        lines += [
            "# Source saved API key(s) unless already exported",
            f'if [ -f "{KEY_FILE}" ]; then export $(grep -v "^#" "{KEY_FILE}" | xargs) 2>/dev/null; fi',
            'export MNEME_BACKEND="openrouter"',
            "unset MNEME_MODEL EMBED_MODEL LABEL_MODEL MNEME_PROVIDER EMBED_PROVIDER LABEL_PROVIDER MNEME_INJECT_SYSTEM",
        ]
    else:
        lines += [
            'export MNEME_BACKEND="ollama"',
            "# Models are read from this instance's mneme.yaml (top-level model:) —",
            "# clearing inherited values keeps the config authoritative and stops a",
            "# stale env var from locking an old model in.",
            "unset MNEME_MODEL EMBED_MODEL LABEL_MODEL MNEME_INJECT_SYSTEM",
        ]
    lines += [
        f'export MNEME_CHUNK_DIR="{instance_dir}"',
        f'export MNEME_PORT="{port}"',
        f'export MNEME_CONFIG="{os.path.join(instance_dir, "mneme.yaml")}"',
        "export PYTHONDONTWRITEBYTECODE=1",
        "",
    ]
    lines += _port_free_lines()
    lines += [
        f'cd "{REPO_ROOT}"',
        "exec python3 -uB proxy/mneme_proxy.py",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path


def free_port(start=8080):
    for p in range(start, 65536):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if s.connect_ex(("127.0.0.1", p)) != 0:
            s.close()
            return p
        s.close()
    # Entire range busy (pathological): let the OS pick an ephemeral port rather
    # than returning the busy 8080 and guaranteeing a bind conflict.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pid_on_port(port):
    """PID of the process listening on `port`, or None. Reads `ss -ltnp` (present
    on standard Linux images, incl. RunPod)."""
    try:
        import re
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            m = re.search(rf":{port}\s.*pid=(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _is_mneme_proxy_pid(pid):
    """True if `pid` is a Mneme proxy process. Guards stop_proxy_on_port and the
    generated start script against killing an unrelated service that happens to
    hold the port (a Jupyter server, another app, etc.)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"mneme_proxy.py" in f.read()
    except Exception:
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True, timeout=5).stdout
            return "mneme_proxy.py" in out
        except Exception:
            return False


def stop_proxy_on_port(port):
    """Stop a running Mneme proxy on `port` (best-effort). Returns True if one was
    found and killed. SIGTERM first, SIGKILL if it doesn't exit within 5s.

    Only kills processes that are actually Mneme proxies — a port held by some
    other service is left alone (with a warning) rather than killed blindly."""
    pid = _pid_on_port(port)
    if not pid:
        return False
    if not _is_mneme_proxy_pid(pid):
        print(f"  ⚠ port {port} is held by a non-Mneme process (pid {pid}) — NOT stopping it.")
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
        for _ in range(10):
            time.sleep(0.5)
            if _pid_on_port(port) is None:
                return True
        os.kill(pid, 9)  # SIGKILL
        return True
    except Exception:
        return False


def _ensure_port_free(port):
    """Stop anything holding `port` and confirm it's actually free before a new
    proxy launches. A failed stop leaves the port held, and the new proxy would
    fail to bind — the symptom looks like a frozen/empty log. Returns True if the
    port is free (or was cleared)."""
    if not _pid_on_port(port):
        return True
    stop_proxy_on_port(port)
    for _ in range(10):
        if _pid_on_port(port) is None:
            return True
        time.sleep(0.5)
    return _pid_on_port(port) is None


def start_proxy(backend, models, port, instance_dir):
    if not _ensure_port_free(port):
        print(f"  ✗ port {port} still in use — aborting start", flush=True)
        return False
    env = os.environ.copy()
    # The instance's mneme.yaml is authoritative for model identity. Clear any
    # inherited model env vars so a stale shell (or an earlier proxy's export)
    # can't lock this proxy onto an old model — the config's top-level model:/
    # embed_model:/label_model: keys supply them instead.
    for _mv in ("MNEME_MODEL", "EMBED_MODEL", "LABEL_MODEL", "MNEME_INJECT_SYSTEM"):
        env.pop(_mv, None)
    env["MNEME_CHUNK_DIR"] = instance_dir
    env["MNEME_PORT"] = str(port)
    env["MNEME_CONFIG"] = os.path.join(instance_dir, "mneme.yaml")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if backend == "openrouter":
        # Propagate EVERY saved provider key into the child process. The chat
        # model may run on a non-OpenRouter provider (routeway/deepseek/…), and
        # the embedder/labeler may run on their own providers — each needs its
        # own key in the child env. Only OpenRouter was propagated before, so a
        # non-OpenRouter chat key never reached the proxy (silent 401).
        for _k, _v in load_saved_keys().items():
            if _v:
                env[_k] = _v
        env["MNEME_BACKEND"] = "openrouter"
    else:
        env["MNEME_BACKEND"] = "ollama"
    log = None  # the proxy now owns its own per-port log ($CHUNK_DIR/proxy-<port>.log)
    subprocess.Popen([sys.executable, "-uB", "proxy/mneme_proxy.py"],
                     cwd=REPO_ROOT, env=env, start_new_session=True)
    print(f"  Starting proxy on port {port}...", end=" ", flush=True)
    for _ in range(30):
        time.sleep(1)
        try:
            d = json.loads(urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3).read())
            print(f"running ({d.get('chunks', 0)} chunks, backend={d.get('backend')})")
            return True
        except Exception:
            continue
    print(f"timeout — check {instance_dir}/proxy-{port}.log")
    return False


# ── Multi-instance (shared DB) ──────────────────────────────────
# Multiple proxy instances share ONE memory DB dir. The FIRST setup writes the
# shared config (mneme.yaml) + saves the shared settings to setup_config.json so
# a later "add instance" can lock the embedder/labeler (the vectors in one DB
# must all come from the SAME embedder or similarity is meaningless). Each added
# instance gets its own start script that overrides only the chat model + port.

def _count_chunks(memory_dir):
    try:
        import sqlite3
        c = sqlite3.connect(os.path.join(memory_dir, "mneme.db"))
        n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        c.close()
        return n
    except Exception:
        return "?"


def _scfg_path(memory_dir):
    return os.path.join(memory_dir, "setup_config.json")


def load_shared_config(memory_dir):
    """Read the shared-instance metadata saved by the first setup. {} when absent."""
    p = _scfg_path(memory_dir)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception as e:
            print(f"  ⚠ setup_config.json is unreadable ({e}) — treating as absent. "
                  f"The embedder/labeler will fall back to defaults, which may not "
                  f"match the vectors already in this DB.")
    return {}


def save_shared_config(memory_dir, models, backend, port=None, inject=None, memory_only=None, shared_weights=None, embed_provider=None, label_provider=None, embed_backend=None, label_backend=None):
    """Persist the shared settings (embedder/labeler + their backends/providers)
    so a later 'add instance' locks them to this DB's original choice, and
    'reconfigure' can recover the original port + injection settings.

    embed_provider/label_provider are stored EXPLICITLY (resolved to the chat
    provider when the wizard left them empty) so a sibling proxy re-pins the same
    backend provider even if its own chat provider differs — the shared-DB match
    rule depends on this. embed_backend/label_backend likewise record the
    resolved transport ("openai" | "ollama")."""
    data = {
        "db_dir": memory_dir,
        "backend": backend,
        "embed_model": models.get("embed_model", ""),
        "embed_backend": embed_backend or backend,
        "label_model": models.get("label_model", ""),
        "label_backend": label_backend or backend,
    }
    if embed_provider is not None:
        data["embed_provider"] = embed_provider
    if label_provider is not None:
        data["label_provider"] = label_provider
    if port is not None:
        data["port"] = int(port)
    if inject is not None:
        data["inject"] = inject
    if memory_only is not None:
        data["memory_only"] = bool(memory_only)
    if shared_weights is not None:
        data["shared_weights"] = bool(shared_weights)
    with open(_scfg_path(memory_dir), "w") as f:
        json.dump(data, f, indent=2)


def db_exists(memory_dir):
    """True if a prior install exists here. Checks more than mneme.db: a setup
    that crashed AFTER writing config/start files but BEFORE the proxy ever
    started (e.g. the Node/Pi install timeout) leaves setup_config.json and the
    per-instance config but no DB file. Those still count as an existing install
    so the wizard offers add/reconfigure/wipe instead of silently overwriting."""
    if os.path.exists(os.path.join(memory_dir, "mneme.db")):
        return True
    if os.path.exists(_scfg_path(memory_dir)):
        return True
    inst = os.path.join(memory_dir, "instances")
    return os.path.isdir(inst) and bool(os.listdir(inst))


def wipe_db(memory_dir):
    """Delete the memory DB, FAISS index, and generated config/start files so a
    'new install' starts from zero. Returns the paths removed. Leaves anything it
    doesn't recognize untouched."""
    removed = []
    for name in ("mneme.db", "mneme.db-wal", "mneme.db-shm",
                 "faiss.index", "faiss.idmap", "faiss.lock",
                 "mneme.yaml", "setup_config.json"):
        p = os.path.join(memory_dir, name)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    # per-instance start scripts (start_proxy.sh, start_proxy_8081.sh, …)
    try:
        for name in sorted(os.listdir(memory_dir)):
            if name.startswith("start_proxy") and name.endswith(".sh"):
                p = os.path.join(memory_dir, name)
                try:
                    os.remove(p)
                    removed.append(p)
                except OSError:
                    pass
    except OSError:
        pass
    # instruction overrides (re-materialized fresh from code defaults on next start)
    inst = os.path.join(memory_dir, "instructions")
    if os.path.isdir(inst):
        shutil.rmtree(inst, ignore_errors=True)
        removed.append(inst)
    # per-instance config dirs (instances/<port>/)
    inst_root = os.path.join(memory_dir, "instances")
    if os.path.isdir(inst_root):
        shutil.rmtree(inst_root, ignore_errors=True)
        removed.append(inst_root)
    return removed


def write_instance_start_script(instance_dir, db_dir, port, chat_backend, chat_model,
                                embed_model, embed_backend, label_model, label_backend,
                                inject, memory_only):
    """Write a per-instance start script: overrides the shared config with this
    instance's chat model + port, points at the shared DB, and reuses the locked
    embedder/labeler (keeping their original backends via MNEME_*_BACKEND)."""
    os.makedirs(instance_dir, exist_ok=True)
    path = os.path.join(instance_dir, f"start_proxy_{port}.sh")
    lines = [
        "#!/bin/bash",
        f"# Mneme proxy instance — chat model: {chat_model} (port {port})",
        f"# Shares the memory DB with other instances at: {db_dir}",
        f"# Start/restart with:  {path}",
        "",
    ]
    if chat_backend == "openrouter":
        lines += [
            "# Source ALL saved provider keys (chat may be on a non-OpenRouter provider).",
            f'if [ -f "{KEY_FILE}" ]; then export $(grep -v "^#" "{KEY_FILE}" | xargs) 2>/dev/null; fi',
        ]
    lines += [
        f'export MNEME_BACKEND="{chat_backend}"',
        "# Models are read from this instance's mneme.yaml (top-level model:) —",
        "# clearing inherited values keeps the config authoritative, so a stale env",
        "# var from another instance/shell can't lock an old model or provider in.",
        "unset MNEME_MODEL EMBED_MODEL LABEL_MODEL MNEME_INJECT_SYSTEM MNEME_PROVIDER EMBED_PROVIDER LABEL_PROVIDER MNEME_EMBED_BACKEND MNEME_LABEL_BACKEND",
    ]
    # Aux backends: only set when they differ from this instance's chat backend,
    # so the embedder/labeler keep running where the DB originally set them up.
    if embed_backend and embed_backend != chat_backend:
        lines.append(f'export MNEME_EMBED_BACKEND="{embed_backend}"')
    if label_backend and label_backend != chat_backend:
        lines.append(f'export MNEME_LABEL_BACKEND="{label_backend}"')
    lines += [
        f'export MNEME_CHUNK_DIR="{instance_dir}"',
        f'export MNEME_PORT="{port}"',
        f'export MNEME_CONFIG="{os.path.join(instance_dir, "mneme.yaml")}"',
        "export PYTHONDONTWRITEBYTECODE=1",
        "",
    ]
    lines += _port_free_lines()
    lines += [
        f'cd "{REPO_ROOT}"',
        "exec python3 -uB proxy/mneme_proxy.py",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path


def start_instance(instance_dir, port, chat_backend, chat_model,
                   embed_model, embed_backend, label_model, label_backend,
                   inject, memory_only):
    """Launch an added instance and wait for its health check."""
    if not _ensure_port_free(port):
        print(f"  ✗ port {port} still in use — aborting start", flush=True)
        return False
    env = os.environ.copy()
    # Same as start_proxy: the instance's mneme.yaml is authoritative for model
    # identity — clear inherited model env vars so a stale value can't lock this
    # instance onto an old model.
    for _mv in ("MNEME_MODEL", "EMBED_MODEL", "LABEL_MODEL", "MNEME_INJECT_SYSTEM"):
        env.pop(_mv, None)
    env["MNEME_CHUNK_DIR"] = instance_dir
    env["MNEME_PORT"] = str(port)
    env["MNEME_CONFIG"] = os.path.join(instance_dir, "mneme.yaml")
    env["MNEME_BACKEND"] = chat_backend
    if embed_backend and embed_backend != chat_backend:
        env["MNEME_EMBED_BACKEND"] = embed_backend
    if label_backend and label_backend != chat_backend:
        env["MNEME_LABEL_BACKEND"] = label_backend
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if chat_backend == "openrouter":
        # Propagate EVERY saved provider key — the chat model may be on a
        # non-OpenRouter provider and the (locked) embedder/labeler on their own.
        for _k, _v in load_saved_keys().items():
            if _v:
                env[_k] = _v
    subprocess.Popen([sys.executable, "-uB", "proxy/mneme_proxy.py"],
                     cwd=REPO_ROOT, env=env, start_new_session=True)
    print(f"  Starting proxy on port {port}...", end=" ", flush=True)
    for _ in range(30):
        time.sleep(1)
        try:
            d = json.loads(urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3).read())
            print(f"running ({d.get('chunks', 0)} chunks, backend={d.get('backend')})")
            return True
        except Exception:
            continue
    print(f"timeout — check {instance_dir}/proxy-{port}.log")
    return False


def _ask_mcp_servers():
    """Interactive MCP-server step — shared by fresh setup AND add-instance.

    Returns a list of {name, command+args | url} dicts (empty = no servers).
    Servers can also be added later via POST /mcp/servers on the running proxy,
    or by editing the mcp_servers block in this instance's mneme.yaml."""
    mcp_servers = []
    print("\n\033[1mMCP tools (optional)\033[0m")
    print("  Add an MCP server to give the model extra tools. Edit mneme.yaml's")
    print("  mcp_servers: block, or POST/DELETE /mcp/servers, to change them later")
    print("  on a running proxy (no restart).")
    while True:
        idx = choose("Add an MCP server?", ["No — done", "Yes — add one"])
        if idx != 1:
            break
        name = ask("Server name", "")
        if not name.strip():
            print("  ⚠ name required — skipping.")
            continue
        entry = {"name": name.strip()}
        transport = choose("Transport?", ["stdio (command + args)", "HTTP (url)"])
        if transport == 0:
            cmd = ask("Command (e.g. npx, uvx, python3)", "")
            if not cmd.strip():
                print("  ⚠ command required — skipping.")
                continue
            entry["command"] = cmd.strip()
            args = ask("Args (space-separated, optional)", "")
            if args.strip():
                entry["args"] = args.split()
        else:
            url = ask("URL (streamable-HTTP endpoint)", "")
            if not url.strip():
                print("  ⚠ url required — skipping.")
                continue
            entry["url"] = url.strip()
        mcp_servers.append(entry)
        print(f"  ✓ added MCP server '{entry['name']}'")
    return mcp_servers


def _add_instance(memory_dir, shared, memory_only):
    """Add a new proxy instance to an existing shared DB.

    Same provider→key→model flow as a fresh install, but for the embedder and
    labeler the choice is LOCKED: they define the memory index, so every proxy
    sharing THIS DB must use the same embedder (and labeler). A user who wants
    different backend models must run a separate DB (a second cluster) — the
    wizard says so explicitly rather than letting them walk into a broken index.
    """
    print("\n\033[1mAdd a proxy instance to the existing DB\033[0m")
    print(f"  Shared DB:  {memory_dir}")
    embed_model = shared.get("embed_model") or OL_DEFAULT_EMBED
    embed_backend = shared.get("embed_backend") or "ollama"
    label_model = shared.get("label_model") or OL_DEFAULT_LABEL
    label_backend = shared.get("label_backend") or "ollama"
    print(f"  Embedder (locked): {embed_model}  ({embed_backend})")
    print(f"  Labeler  (locked): {label_model}  ({label_backend})")
    print("  These are fixed by the DB — a proxy sharing this DB MUST use the same")
    print("  embedder/labeler. To use different backend models, run a separate DB.")

    print("\n\033[1mChat model for this instance\033[0m")
    print("  The chat model is per-instance and may differ from other proxies.")
    chat = _pick_role("Chat", OR_DEFAULT_MAIN)
    chat_backend = "ollama" if chat["provider"] == "ollama" else "openrouter"
    chat_model = chat["model"]
    chat_provider = chat["provider"]
    chat_base_url = chat["base_url"]
    chat_key_env = chat["key_env"]

    port = int(ask("Port for this instance", str(free_port(DEFAULT_PORT))) or DEFAULT_PORT)
    instance_dir = _instance_dir(memory_dir, port)
    db_path = os.path.join(memory_dir, "mneme.db")

    # Guard against clobbering a stopped instance: free_port only avoids
    # *listening* ports, so a manually-stopped instance's port is offered again,
    # and typing Enter would overwrite its mneme.yaml / Modelfile / start script.
    if os.path.isfile(os.path.join(instance_dir, "mneme.yaml")):
        ans = ask(f"  ⚠ an instance already exists on port {port} — overwrite its config and start script? [y/N]", "N").strip().lower()
        if ans not in ("y", "yes"):
            print("  Cancelled — keeping the existing instance.")
            return 0

    # Context window applies to the CHAT model (per-instance).
    ctx_size = pick_context_window()

    # Model template (optional) — known-good generation settings for this model.
    model_template = choose_model_template()

    # Local Ollama chat: pin the context window via a derived Modelfile (see the
    # fresh-install path for why). Then a template that ships a custom Modelfile
    # overrides the picked chat model + context window.
    if chat_backend == "ollama":
        chat_model = create_context_modelfile(chat_model, ctx_size)
        chat_model, ctx_size = _install_template_modelfile(
            model_template, chat_backend, chat_model, ctx_size,
            port=port, instance_dir=instance_dir,
            shared=bool(shared.get("shared_weights", True)))

    idx = choose("Inject Mneme's system instructions?", [
        "Yes (default — inject the memory instructions + toolset prompt)",
        "No (skip — use a merged prompt from your own harness)",
    ])
    inject = "1" if idx == 0 else "0"

    # MCP servers (optional) — same step as a fresh setup, so a new proxy can
    # register web/filesystem tools without hand-editing its config afterward.
    mcp_servers = _ask_mcp_servers()

    # Per-instance config: this instance's own chat model + the shared DB path.
    # The embedder/labeler are re-pinned to the DB's original providers (their
    # backends too) so this proxy indexes into the same vector space as its
    # siblings — the "sharing a DB must match" rule, enforced at write time.
    instance_models = {
        "model": chat_model,
        "embed_model": embed_model,
        "label_model": label_model,
        "ctx_size": ctx_size,
    }
    cfg = write_config(chat_backend, instance_models, port, inject, memory_only,
                       instance_dir, db_path, mcp_servers, model_template=model_template,
                       provider=chat_provider, base_url=chat_base_url, key_env=chat_key_env,
                       embed_provider=shared.get("embed_provider", ""),
                       label_provider=shared.get("label_provider", ""),
                       embed_backend=embed_backend, label_backend=label_backend)
    script = write_instance_start_script(instance_dir, memory_dir, port, chat_backend, chat_model,
                                         embed_model, embed_backend, label_model, label_backend,
                                         inject, memory_only)
    started = start_instance(instance_dir, port, chat_backend, chat_model,
                             embed_model, embed_backend, label_model, label_backend,
                             inject, memory_only)
    create_access_symlinks()

    print("\n\033[1mInstance added.\033[0m")
    print(f"  Chat model:  {chat_model}  (backend {chat_backend})")
    print(f"  Port:        {port}")
    print(f"  Config:      {cfg}")
    print(f"  Start:       {script}")
    print(f"  Log:         {instance_dir}/proxy-{port}.log")
    print(f"  Shared DB:   {memory_dir}")
    print(f"  Dashboard:   http://localhost:{port}/")
    print(f"  Chat UI:     http://localhost:{port}/chat")
    return 0 if started else 1


def add_instance_noninteractive(params):
    """Add a proxy instance with NO interactive prompts (driven by the dashboard's
    "Add proxy" dialog). `params` is a dict with:
        port           (int, required)
        chat_backend   ("openrouter" | "ollama", default "openrouter")
        chat_model     (str, required — full OpenRouter id or Ollama model name)
        api_key        (optional str — save a new OpenRouter key; else reuse the saved one)
        inject         (bool, default True)
        ctx_size       (optional int, default 64000)
        model_template (optional str, default "" — none)
        mcp_servers    (optional list, default [])
        overwrite      (optional bool, default False)
    Returns 0 on success, 1 on failure. Prints progress to stdout (streamed to the
    dashboard's terminal pane)."""
    global REPO_ROOT, MEMORY_DIR
    REPO_ROOT = find_repo()
    branch = detect_branch(REPO_ROOT)
    memory_only = (branch == "memory-only")

    db_dir = os.path.abspath(os.path.expanduser(
        os.environ.get("MNEME_CHUNK_DIR") or os.path.expanduser("~/mneme/chunks")))
    MEMORY_DIR = db_dir
    if not db_exists(db_dir):
        print(f"  ✗ no existing install at {db_dir} — run the full setup first", flush=True)
        return 1
    shared = load_shared_config(db_dir)

    chat_backend = params.get("chat_backend", "openrouter") or "openrouter"
    port = int(params.get("port") or 0)
    if not port:
        print("  ✗ missing port", flush=True)
        return 1
    chat_model = (params.get("chat_model") or "").strip()
    if not chat_model:
        print("  ✗ missing chat_model", flush=True)
        return 1
    inject = "1" if params.get("inject", True) else "0"
    model_template = params.get("model_template") or ""
    mcp_servers = params.get("mcp_servers") or []
    ctx_size = int(params.get("ctx_size") or 64000)

    embed_model = shared.get("embed_model") or OL_DEFAULT_EMBED
    embed_backend = shared.get("embed_backend") or "ollama"
    label_model = shared.get("label_model") or OL_DEFAULT_LABEL
    label_backend = shared.get("label_backend") or "ollama"

    print("  Add a proxy instance to the existing DB", flush=True)
    print(f"  Shared DB:  {db_dir}", flush=True)
    print(f"  Embedder (locked): {embed_model}  ({embed_backend})", flush=True)
    print(f"  Labeler  (locked): {label_model}  ({label_backend})", flush=True)
    print(f"  Chat backend: {chat_backend}", flush=True)
    print(f"  Chat model:   {chat_model}", flush=True)
    print(f"  Port:         {port}", flush=True)

    instance_dir = _instance_dir(db_dir, port)
    db_path = os.path.join(db_dir, "mneme.db")

    if os.path.isfile(os.path.join(instance_dir, "mneme.yaml")) and not params.get("overwrite"):
        print(f"  ✗ an instance already exists on port {port} — not overwriting (pass overwrite:true)", flush=True)
        return 1

    if chat_backend == "openrouter":
        api_key = (params.get("api_key") or "").strip()
        if api_key:
            info = or_get(api_key, "/auth/key")
            if not (info and "data" in info):
                print("  ✗ invalid OpenRouter API key", flush=True)
                return 1
            save_key(api_key)
            print("  ✓ OpenRouter key saved", flush=True)
        elif not load_saved_key():
            print("  ✗ no OpenRouter key available — pass api_key", flush=True)
            return 1

    if chat_backend == "ollama":
        ensure_ollama()
        pulled = get_pulled_models()
        if chat_model not in pulled:
            print(f"  Pulling {chat_model}…", flush=True)
            pull_model(chat_model)
        chat_model = create_context_modelfile(chat_model, ctx_size)
        if model_template:
            chat_model, ctx_size = _install_template_modelfile(
                model_template, chat_backend, chat_model, ctx_size,
                port=port, instance_dir=instance_dir,
                shared=bool(shared.get("shared_weights", True)))

    instance_models = {
        "model": chat_model,
        "embed_model": embed_model,
        "label_model": label_model,
        "ctx_size": ctx_size,
    }
    # Re-pin the DB's embed/label providers so this proxy indexes into the same
    # vector space as its siblings (the sharing-a-DB match rule).
    cfg = write_config(chat_backend, instance_models, port, inject, memory_only,
                       instance_dir, db_path, mcp_servers, model_template=model_template,
                       provider=params.get("chat_provider", "openrouter"),
                       base_url=params.get("chat_base_url", OR_BASE),
                       key_env=params.get("chat_key_env", "OPENROUTER_API_KEY"),
                       embed_provider=shared.get("embed_provider", ""),
                       label_provider=shared.get("label_provider", ""),
                       embed_backend=embed_backend, label_backend=label_backend)
    script = write_instance_start_script(instance_dir, db_dir, port, chat_backend, chat_model,
                                         embed_model, embed_backend, label_model, label_backend,
                                         inject, memory_only)
    started = start_instance(instance_dir, port, chat_backend, chat_model,
                             embed_model, embed_backend, label_model, label_backend,
                             inject, memory_only)
    create_access_symlinks()

    print("", flush=True)
    print("  Instance " + ("started" if started else "configured but FAILED to start") + ".", flush=True)
    print(f"  Chat model:  {chat_model}  (backend {chat_backend})", flush=True)
    print(f"  Port:        {port}", flush=True)
    print(f"  Config:      {cfg}", flush=True)
    print(f"  Start:       {script}", flush=True)
    print(f"  Log:         {instance_dir}/proxy-{port}.log", flush=True)
    print(f"  Chat UI:     http://localhost:{port}/chat", flush=True)
    return 0 if started else 1


def create_access_symlinks():
    """Expose the memory dir + repo to JupyterLab's file browser via /workspace
    symlinks, so a user can browse and download the DB, config, and prompts from
    the GUI (RunPod's JupyterLab is rooted at /workspace, which ~/mneme isn't
    under, and /root is mode 700 so the GUI can't climb to it). No-op off-pod."""
    if not os.path.isdir("/workspace"):
        return
    links = {
        "/workspace/mneme-chunks": MEMORY_DIR,
        "/workspace/mneme-repo": REPO_ROOT,
    }
    made = []
    for link, target in links.items():
        try:
            if os.path.islink(link):
                os.remove(link)
            elif os.path.exists(link):
                continue  # a real file/dir is there; don't clobber it
            os.symlink(target, link)
            made.append(link)
        except Exception:
            pass
    if made:
        print("\n  JupyterLab shortcuts (browse/download your DB):")
        for link in made:
            print(f"    {link}  →  {links[link]}")


# ── Main ─────────────────────────────────────────────────────────
def main():
    global REPO_ROOT, MEMORY_DIR
    banner()
    REPO_ROOT = find_repo()
    print(f"  Repo: {REPO_ROOT}")
    branch = detect_branch(REPO_ROOT)
    memory_only = (branch == "memory-only")
    if memory_only:
        print(f"  Branch: {branch} → memory-only build (strategy/learning layer off)")
    else:
        print(f"  Branch: {branch} → full build (strategy/learning layer on)")

    # 0. Memory DB location — shared by every instance of this Mneme install.
    #    Point it at a mounted shared volume to let instances on OTHER machines
    #    use the same memory (same-machine multi-instance needs no special setup).
    print("\n\033[1mStep 0/4 — Memory DB location\033[0m")
    _default_db = os.path.abspath(os.environ.get("MNEME_CHUNK_DIR") or os.path.expanduser("~/mneme/chunks"))
    # Normalize to an ABSOLUTE path. The proxy resolves a relative chunk_dir against
    # ITS OWN cwd (the repo), which differs from where setup runs — so a relative
    # path silently resolves somewhere ephemeral inside the repo instead of the
    # intended persistent location. abspath() makes the stored path deterministic
    # no matter where setup or the proxy runs from.
    _raw = ask("Memory DB directory (shared by all instances)", _default_db) or _default_db
    MEMORY_DIR = os.path.abspath(os.path.expanduser(_raw))
    if MEMORY_DIR != _raw:
        print(f"  → resolved to: {MEMORY_DIR}")
    if os.path.isdir("/workspace") and not (MEMORY_DIR == "/workspace" or MEMORY_DIR.startswith("/workspace/")):
        print("  ⚠ /workspace is the persistent volume here — a path outside it is wiped on stop/restart.")
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # Existing DB? Offer to add an instance, reconfigure, or wipe it for a fresh
    # install.
    reconf_port = None
    if db_exists(MEMORY_DIR):
        shared = load_shared_config(MEMORY_DIR)
        if os.path.exists(os.path.join(MEMORY_DIR, "mneme.db")):
            print(f"\n  Existing memory DB found at {MEMORY_DIR} ({_count_chunks(MEMORY_DIR)} chunks).")
        else:
            print(f"\n  Existing install found at {MEMORY_DIR} (config present, but no memory DB yet — a previous setup didn't finish).")
        idx = choose("What would you like to do?", [
            "Add another proxy instance (new chat model + port, sharing this DB)",
            "Reconfigure this install (re-pick backend / models / port — keeps the DB)",
            "New install (wipe this DB and start fresh with all new settings)",
        ])
        if idx == 0:
            return _add_instance(MEMORY_DIR, shared, memory_only)
        if idx == 2:
            # New install: stop any running instance, wipe the DB, then fall
            # through to the fresh setup below (reconf_port stays None so the
            # fresh defaults — port 8080 etc. — apply).
            ans = ask("Wipe the existing memory DB and start fresh? This permanently deletes ALL saved memory, the FAISS index, and the generated config/start scripts (including any prompts or settings you edited). [y/N]", "N").strip().lower()
            if ans not in ("y", "yes"):
                print("  Cancelled — keeping the existing install.")
                return 0
            # Stop EVERY running instance, not just the last-saved port. A
            # multi-instance setup has several proxies; stopping only one leaves
            # the others holding the DB open while we delete it — they'd keep
            # writing to the deleted file (resurrecting a partial wipe). Enumerate
            # instances/<port>/ plus the saved port, then stop each Mneme proxy.
            _ports = set()
            _inst = os.path.join(MEMORY_DIR, "instances")
            if os.path.isdir(_inst):
                for _n in os.listdir(_inst):
                    if _n.isdigit():
                        _ports.add(int(_n))
            _old_port = shared.get("port")
            if _old_port:
                _ports.add(int(_old_port))
            for _p in sorted(_ports):
                if stop_proxy_on_port(_p):
                    print(f"  Stopped instance on port {_p}.")
                    time.sleep(0.5)
            wiped = wipe_db(MEMORY_DIR)
            print(f"  Wiped {len(wiped)} file(s). Starting a fresh install...")
        else:
            # Reconfigure: reuse the SAVED port as the default (not the next free
            # port) and stop the old instance there so it's a true stop-and-restart,
            # not a duplicate on a new port.
            reconf_port = shared.get("port")
            if reconf_port:
                print(f"  Reconfiguring — reusing port {reconf_port} (old instance there will be stopped).")

    # 1. Provider + models — SAME six steps for each of the three roles:
    #      provider → key (if the provider has one) → model.
    #    The three roles are fully independent: the chat model can run on one
    #    provider while the embedder and labeler run on others. One key per
    #    provider (an env var holds a single value), so a provider shared by two
    #    roles reuses the same key; different providers get different keys.
    print("\n\033[1mStep 1/4 — Chat model\033[0m")
    print("  The chat model can be changed later from the chat page's model menu.")
    chat = _pick_role("Chat", OR_DEFAULT_MAIN)
    provider = chat["provider"]
    provider_label = chat["provider_label"]
    base_url = chat["base_url"]
    key_env = chat["key_env"]
    # The chat backend is "ollama" only when the chat model itself is local
    # Ollama; every OpenAI-compatible provider (hosted OR local vLLM/llama.cpp)
    # goes through the openrouter-style backend path.
    backend = "ollama" if provider == "ollama" else "openrouter"

    print("\n\033[1mStep 2/4 — Backend models (embedder + labeler)\033[0m")
    print("  The embedder is SET-ONCE: it defines your memory index (one DB = one")
    print("  embedder + dimension). The labeler can differ; both may share the chat")
    print("  provider or use their own.")
    embedder = _pick_role("Embedder", OR_DEFAULT_EMBED, allow_ollama_list=False)
    labeler = _pick_role("Labeler", OR_DEFAULT_LABEL, allow_ollama_list=False)

    # Context window applies to the CHAT model. Hosted providers suggest a default
    # (their known window); local models need it set explicitly (Ollama pins
    # num_ctx via the Modelfile below).
    models = {
        "model": chat["model"],
        "embed_model": embedder["model"],
        "label_model": labeler["model"],
        "ctx_size": pick_context_window(),
    }
    # Backend-model providers: only pin when they DIFFER from the chat provider —
    # empty means "follow the chat provider", which is the common case and keeps
    # the config clean (backward-compatible with pre-decoupling installs).
    embed_provider = "" if embedder["provider"] == provider else embedder["provider"]
    label_provider = "" if labeler["provider"] == provider else labeler["provider"]
    # Backend TRANSPORTS ("openai" | "ollama"), derived from each role's provider.
    # The proxy's _aux_backend() expects a transport, not a provider id — e.g. an
    # embedder on Groq still uses the "openai" transport. Empty = follow the chat
    # backend (the default).
    def _transport(role_provider):
        return "ollama" if role_provider == "ollama" else "openai"
    embed_backend = _transport(embedder["provider"]) if embed_provider else ""
    label_backend = _transport(labeler["provider"]) if label_provider else ""


    # Model template (optional) — known-good generation settings for this model.
    model_template = choose_model_template()

    # 3. Pi (optional)
    print("\n\033[1mStep 3/4 — Chat interface\033[0m")
    print("  Pi is a lightweight terminal AI assistant. If you say no, you can still:")
    print("    • use the built-in chat page at http://localhost:8080/chat")
    print("    • connect any OpenAI-compatible client to http://localhost:8080/v1")
    idx = choose("Install Pi?", ["No — proxy only (built-in chat / my own client)", "Yes — install Pi"])
    install_pi = (idx == 1)

    # 4. Port + injection
    print("\n\033[1mStep 4/4 — Port & instructions\033[0m")
    port = int(ask("Proxy port", str(reconf_port or free_port(DEFAULT_PORT))) or (reconf_port or DEFAULT_PORT))
    idx = choose("Inject Mneme's system instructions?", [
        "Yes (default — inject the memory instructions + toolset prompt)",
        "No (skip — use a merged prompt from your own harness)",
    ])
    inject = "1" if idx == 0 else "0"

    # Hot-reload lock — live-edit is great for experimenting, but a coding/studying
    # agent can edit its own config/prompts. Locking freezes them (restart to change).
    idx = choose("Live-edit config & prompts?", [
        "Yes — hot-reload (edits apply immediately)",
        "No — lock them (changes require a restart)",
    ])
    hot_reload = (idx == 0)

    # MCP servers (optional) — add tools from any MCP server (web, filesystem, ...).
    mcp_servers = _ask_mcp_servers()

    # Per-instance config dir + shared DB path.
    instance_dir = _instance_dir(MEMORY_DIR, port)
    db_path = os.path.join(MEMORY_DIR, "mneme.db")

    # Reconfigure with a CHANGED embedder: existing vectors were built with the
    # old embedder, so similarity against them becomes meaningless. Warn loudly
    # (same cross-embedder hazard the wizard documents at add-instance) instead
    # of silently letting the user walk into it.
    if reconf_port is not None:
        _old_embed = (shared.get("embed_model") or "").strip()
        _new_embed = (models.get("embed_model") or "").strip()
        if _old_embed and _new_embed and _old_embed != _new_embed and os.path.exists(db_path):
            _n = _count_chunks(MEMORY_DIR)
            if _n:
                print(f"  ⚠ WARNING: embedder changed from {_old_embed!r} to {_new_embed!r}. "
                      f"The {_n} existing chunk vector(s) were built with the old embedder — "
                      f"retrieval similarity will be meaningless. Consider a fresh install (wipe).")

    # VRAM: share model weights across proxies on the same model, or give each
    # proxy its own derived model (per-proxy Modelfile)? Shared is the default
    # (one resident copy per model); per-proxy lets each proxy edit its own
    # Modelfile (context window / stop tokens) at the cost of N× VRAM.
    shared_weights = True
    if backend == "ollama":
        shared_weights = (choose("Share model weights across proxies on the same model?", [
            "Yes — share (default; one resident copy, saves VRAM)",
            "No — per-proxy Modelfile (each proxy edits its own; more VRAM)",
        ]) == 0)

    # Local Ollama chat: pin the context window via a derived Modelfile so the
    # model actually loads with num_ctx == ctx_size (matching sampling.ctx_tokens).
    # Without this, Ollama loads the model's default context and silently
    # truncates when the proxy sends a longer prompt. (vLLM/llama.cpp take the
    # context at server start, so they skip this.)
    if backend == "ollama":
        models["model"] = create_context_modelfile(models["model"], models.get("ctx_size", 64000))

    # A template that ships a custom Modelfile must create its model from it
    # (Ollama only). With shared_weights the derived name ignores the port (one
    # model per base+template); otherwise it's keyed on the port (per-proxy).
    if backend == "ollama":
        models["model"], models["ctx_size"] = _install_template_modelfile(
            model_template, backend, models["model"], models.get("ctx_size", 64000),
            port=port, instance_dir=instance_dir, shared=shared_weights)

    # Write config + start script, then launch.
    cfg_path = write_config(backend, models, port, inject, memory_only, instance_dir, db_path, mcp_servers, hot_reload, model_template, provider=provider, base_url=base_url, key_env=key_env, embed_provider=embed_provider, label_provider=label_provider, embed_backend=embed_backend, label_backend=label_backend)
    # Persist the RESOLVED backend providers + transports (chat's when the role
    # followed the chat) so a sibling proxy re-pins the same embedder/labeler even
    # if its own chat provider differs — the shared-DB match rule depends on this.
    save_shared_config(MEMORY_DIR, models, backend, port=port, inject=inject,
                       memory_only=memory_only, shared_weights=shared_weights,
                       embed_provider=embedder["provider"], label_provider=labeler["provider"],
                       embed_backend=_transport(embedder["provider"]),
                       label_backend=_transport(labeler["provider"]))
    start_script = write_start_script(backend, models, port, instance_dir, provider=provider, key_env=key_env, embed_provider=embed_provider, label_provider=label_provider)
    print(f"\n  Config:      {cfg_path}")
    print(f"  Start/stop:  {start_script}")

    if install_pi:
        try:
            setup_pi(models.get("ctx_size"), branch, port=port)
        except Exception as e:
            print(f"  ⚠ Pi setup failed ({type(e).__name__}: {e}) — continuing without Pi.")

    # Reconfigure: stop the old instance on its saved port before starting the
    # new one (a restart, not a second instance).
    if reconf_port is not None:
        if stop_proxy_on_port(reconf_port):
            print(f"  Stopped old instance on port {reconf_port}.")
            time.sleep(1)

    started = start_proxy(backend, models, port, instance_dir)
    create_access_symlinks()

    # Wrap up
    print("\n\033[1mSetup complete.\033[0m")
    print(f"  Backend:    {backend}")
    print(f"  Memory DB:  {MEMORY_DIR}")
    print(f"  Config:     {instance_dir}")
    print(f"  Start/stop: {start_script}")
    print(f"  Log:       {instance_dir}/proxy-{port}.log")
    print("\n  Dashboard (all proxies): http://localhost:%d/" % port)
    print("  Chat UI:        http://localhost:%d/chat" % port)
    print("  Memory:         http://localhost:%d/memory" % port)
    print("  Templates:      http://localhost:%d/templates" % port)
    print("  Prompt editor:  http://localhost:%d/instructions" % port)
    print("  Ollama panel:   http://localhost:%d/ollama" % port)
    print("  OpenAI API:     http://localhost:%d/v1" % port)
    print("  Health:         http://localhost:%d/health" % port)
    if backend == "openrouter":
        print("\n  The API key lives only in ~/mneme/env (chmod 600).")
    print()
    return 0 if started else 1


if __name__ == "__main__":
    if "--add" in sys.argv:
        # Non-interactive: add a proxy instance from a JSON answers file.
        _i = sys.argv.index("--add")
        if _i + 1 >= len(sys.argv):
            print("usage: mneme_setup.py --add <answers.json>", flush=True)
            sys.exit(2)
        try:
            with open(sys.argv[_i + 1], "r", encoding="utf-8") as f:
                _params = json.load(f)
            sys.exit(add_instance_noninteractive(_params))
        except SystemExit:
            raise
        except Exception as e:
            print(f"\n  ✗ Add-instance failed: {type(e).__name__}: {e}", flush=True)
            sys.exit(1)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Cancelled.")
        sys.exit(130)
    except Exception as e:
        print(f"\n  ✗ Setup failed: {type(e).__name__}: {e}")
        sys.exit(1)

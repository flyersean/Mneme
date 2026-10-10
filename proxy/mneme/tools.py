"""Mneme tool system — native bootstrap tools, tool registry, retrieval injection.

Three responsibilities:

  1. Native tools      — a minimal bootstrap toolset (``bash`` + ``write``) owned
     by the proxy so the model can build and run tools with no harness present.
  2. Tool registry      — a persistent store of every tool the model has built,
     which it can find and read on demand (``list_tools`` / ``read_tool``).
  3. Tool injection     — retrieval-gated auto-injection of relevant built tools
     into context, so the model reuses instead of rebuilding.

Dependencies on the orchestrator (``mneme_proxy``) are bound after import:
``tools.db`` (sqlite handle) and ``tools.embed`` (the embed function). This keeps
the module import-cycle-free and unit-testable against a temp DB + a fake embed.
"""

import base64
import os
import json
import shutil
import subprocess
import time
import re as _re
from html import unescape as _unescape

import requests
import numpy as np

from mneme.util import _extract_text, _sniff_mime
from mneme.mcp_client import get_manager

# ─── Config (env-set by mneme_proxy's config loader before first use) ────
NATIVE_TOOLS_MODE = os.environ.get("MNEME_NATIVE_TOOLS", "auto")   # auto | on | off
def _default_tools_dir() -> str:
    # Tools live under the install's chunk dir, NOT a hardcoded ~/mneme path —
    # otherwise a clean install in another directory (e.g. ~/mneme-ox) would still
    # write tools into the old ~/mneme/chunks/tools. MNEME_TOOLS_DIR overrides.
    cd = os.environ.get("MNEME_CHUNK_DIR") or "~/mneme/chunks"
    return os.path.expanduser(os.environ.get("MNEME_TOOLS_DIR") or os.path.join(cd, "tools"))
TOOLS_DIR = _default_tools_dir()
BASH_TIMEOUT = int(os.environ.get("MNEME_TOOLS_BASH_TIMEOUT", "30"))
_BASH_LOG_MAX = int(os.environ.get("MNEME_BASH_LOG_MAX", str(10 * 1024 * 1024)))  # 10 MB cap

# ─── Filesystem scope (enforced by the native bash/write/read tools) ────
# The model may WRITE only under MODEL_SCOPE and TOOLS_DIR (its workspace); the
# rest of the filesystem is read-only to it. read_file is additionally limited
# to BROWSER_ROOT (the user's view). mneme_proxy calls set_scope() after config
# load / hot-reload; the env vars are the fallback defaults.
MODEL_SCOPE = os.path.realpath(os.path.expanduser(
    os.environ.get("MNEME_MODEL_SCOPE", "~/mneme/output")))
BROWSER_ROOT = os.path.realpath(os.path.expanduser(
    os.environ.get("MNEME_BROWSER_ROOT", "~")))
# Harness run workspaces (runs_root/<run_id>/workspace) are also the model's —
# the run planner/executor writes files there via absolute paths.
RUNS_ROOT = os.path.realpath(os.path.expanduser(
    os.environ.get("MNEME_RUNS_DIR")
    or os.path.join(os.environ.get("MNEME_CHUNK_DIR", "~/mneme/chunks"), "runs")))


def set_scope(model_scope=None, browser_root=None):
    global MODEL_SCOPE, BROWSER_ROOT
    if model_scope:
        MODEL_SCOPE = os.path.realpath(os.path.expanduser(str(model_scope)))
    if browser_root:
        BROWSER_ROOT = os.path.realpath(os.path.expanduser(str(browser_root)))


def _within(path, root):
    p = os.path.realpath(os.path.expanduser(str(path)))
    r = os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


def _writable(path):
    """True when `path` is inside the model's writable area (workspace + scope
    + run workspaces)."""
    return (_within(path, TOOLS_DIR) or _within(path, MODEL_SCOPE)
            or _within(path, RUNS_ROOT))


def _writable_roots():
    """Deduplicated list of writable roots (workspace + scope + run workspaces)."""
    roots = []
    for r in (TOOLS_DIR, MODEL_SCOPE, RUNS_ROOT):
        r = os.path.realpath(r)
        if not any(r == x or r.startswith(x + os.sep) for x in roots):
            roots.append(r)
    return roots


# ─── Blocked-write tracking ─────────────────────────────────────
# When the `write` tool refuses a path outside scope, it's recorded here so the
# proxy can surface it (GET /fs/blocked) and the chat UI can offer a one-click
# "grant write access" affordance. Capped + deduped to the last N distinct paths.
_BLOCKED_WRITES = []
_BLOCKED_WRITES_MAX = 50


def record_blocked_write(path):
    p = os.path.realpath(str(path))
    if p not in _BLOCKED_WRITES:
        _BLOCKED_WRITES.append(p)
        if len(_BLOCKED_WRITES) > _BLOCKED_WRITES_MAX:
            _BLOCKED_WRITES.pop(0)


def recent_blocked_writes():
    return list(_BLOCKED_WRITES)


def clear_blocked_writes():
    _BLOCKED_WRITES.clear()


TOOL_INJECT_MIN_SIM = float(os.environ.get("MNEME_TOOL_INJECT_MIN_SIMILARITY", "0.75"))
TOOL_INJECT_MAX = int(os.environ.get("MNEME_TOOL_INJECT_MAX", "3"))
TOOL_INJECT_TOKENS = int(os.environ.get("MNEME_TOOL_INJECT_TOKENS", "600"))

# Bound by mneme_proxy after import (see _apply_config / startup).
db = None          # sqlite3.Connection
embed = None       # callable: str -> np.ndarray (normalized) | None
ledger = None      # bound by mneme_proxy at harness startup (mneme.harness.Ledger | None)
engine = None      # bound by mneme_proxy at harness startup (mneme.harness.RunEngine | None)


def reload_config():
    """Re-read the tools config from env.

    mneme_proxy imports this module BEFORE load_config() applies the config file
    (which sets the MNEME_* env vars), so the module-level defaults above are
    stale. mneme_proxy calls this right after load_config() to refresh them.
    """
    global NATIVE_TOOLS_MODE, TOOLS_DIR, BASH_TIMEOUT
    global TOOL_INJECT_MIN_SIM, TOOL_INJECT_MAX, TOOL_INJECT_TOKENS
    global MODEL_SCOPE, BROWSER_ROOT, RUNS_ROOT
    NATIVE_TOOLS_MODE = os.environ.get("MNEME_NATIVE_TOOLS", "auto")
    TOOLS_DIR = _default_tools_dir()
    BASH_TIMEOUT = int(os.environ.get("MNEME_TOOLS_BASH_TIMEOUT", "30"))
    MODEL_SCOPE = os.path.realpath(os.path.expanduser(
        os.environ.get("MNEME_MODEL_SCOPE", "~/mneme/output")))
    BROWSER_ROOT = os.path.realpath(os.path.expanduser(
        os.environ.get("MNEME_BROWSER_ROOT", "~")))
    RUNS_ROOT = os.path.realpath(os.path.expanduser(
        os.environ.get("MNEME_RUNS_DIR")
        or os.path.join(os.environ.get("MNEME_CHUNK_DIR", "~/mneme/chunks"), "runs")))
    TOOL_INJECT_MIN_SIM = float(os.environ.get("MNEME_TOOL_INJECT_MIN_SIMILARITY", "0.75"))
    TOOL_INJECT_MAX = int(os.environ.get("MNEME_TOOL_INJECT_MAX", "3"))
    TOOL_INJECT_TOKENS = int(os.environ.get("MNEME_TOOL_INJECT_TOKENS", "600"))

# ─── Tool definitions (OpenAI function-calling format) ──────────────────

SEARCH_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search Mneme memory for past conversations, facts, documents, or details. Use when you need more context than the injected memory provides — look up specific topics, API keys, file paths, or conversation details from prior sessions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for — be specific"},
                "top_k": {"type": "integer", "description": "Number of results (default 5)"},
            },
            "required": ["query"],
        },
    },
}

LIST_TOOLS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_tools",
        "description": "List the tools you have previously built and saved (the tool registry). Use to find a tool you can reuse instead of rebuilding. Optionally filter by a semantic query or problem type.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional — find tools relevant to this description"},
                "problem_type": {"type": "string", "description": "Optional — filter by problem type (e.g. live_data)"},
            },
            "required": [],
        },
    },
}

READ_TOOL_TOOL = {
    "type": "function",
    "function": {
        "name": "read_tool",
        "description": "Read the full source of a tool you previously built, by name, so you can re-run or adapt it.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact tool name"},
            },
            "required": ["name"],
        },
    },
}

READ_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_image",
        "description": "View an image that was previously stored in memory, by its hash (or full path). Returns the actual image so you can look at it again. Use when a memory note shows an [IMAGE: ...] attachment and you need to see it.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The image hash or full path, exactly as shown in the [IMAGE: ...] memory note"},
            },
            "required": ["ref"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file on the Mneme host by path, optionally a 1-based line range. Use this to inspect code/config efficiently instead of `bash cat` (which dumps the whole file). Returns the requested lines, capped at 12000 chars.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file, e.g. /path/to/repo/proxy/mneme_proxy.py"},
                "start": {"type": "integer", "description": "Optional 1-based first line to read (default 1)"},
                "end": {"type": "integer", "description": "Optional 1-based last line to read, inclusive (default: end of file)"},
            },
            "required": ["path"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch ONE web page and return its CLEAN text (HTML/CSS/JS stripped) — the actual "
            "page content, not a snippet. USE THIS whenever you need a specific fact (a price, "
            "address, phone number, menu item, version, date, quote) from a page you have a URL "
            "for. This is the ONLY way to read real page content: web_search returns snippets, "
            "which are leads, NOT answers. If your answer would rest on a snippet, you have not "
            "finished — call fetch_url on the best URL first.\n"
            "\n"
            "Use it instead of `bash curl` (curl returns raw HTML with scripts and markup; this "
            "returns readable text). Works on ordinary sites. If it returns empty text or a "
            "JS-only shell (a page that loads its content with JavaScript — React/Streamlit apps, "
            "leaderboards), retry the SAME url with render_js: true, which renders the page in a "
            "headless browser and reads the result. If that still fails (login/bot wall), say so "
            "rather than guessing at the contents.\n"
            "\n"
            "Mneme saves the FULL page text to memory as page:<domain> chunks; you see a bounded "
            "head+tail window, and can retrieve any detail later with search_memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL to fetch, e.g. https://jamopizza.com/menu"},
                "render_js": {"type": "boolean", "description": "Render the page with a headless browser (runs JavaScript) before extracting text. Use only when the plain fetch came back empty or JS-only. Slower (a few seconds)."},
            },
            "required": ["url"],
        },
    },
}

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web and return the top results — each with a title, URL, and a SHORT "
            "SNIPPET. Use this to DISCOVER which pages have an answer, or to find a library, "
            "service, or documentation you don't know the URL of.\n"
            "\n"
            "IMPORTANT — snippets are leads, not answers. A snippet is a fragment the search "
            "engine chose to show; it is frequently truncated, out of date, or missing the exact "
            "number you need. Never answer a question about a specific fact (price, address, "
            "phone, date, version) from a snippet, and never present a snippet as though you "
            "read the page.\n"
            "\n"
            "The web workflow is TWO steps:\n"
            "  1. web_search(\"query\")              -> find candidate URLs\n"
            "  2. fetch_url(\"<best url>\")          -> read the actual page, THEN answer\n"
            "Do step 2 in the SAME turn as step 1. Do not stop after searching and report back "
            "what the snippets said — that is an incomplete answer. Pick the most authoritative "
            "URL (the official site or primary source beats an aggregator or a forum summary) "
            "and fetch it. If the first fetch doesn't contain the answer, fetch a second URL "
            "from the results before giving up."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for — a specific question or keywords"},
            },
            "required": ["query"],
        },
    },
}

RETRACT_MEMORY_TOOL = {
    "type": "function",
    "function": {
        # Named "flag_bad_memory", NOT "retract_memory". The old name promised an
        # action the model cannot take, and the model acted on the promise: it told
        # users "once you confirm, I will proceed with retracting it" and offered to
        # mark chunks false. The name is what the model reads in its tool list every
        # turn, so it has to describe what actually happens — the model raises a
        # suspicion, the user decides.
        "name": "flag_bad_memory",
        "description": (
            "Flag a stored memory chunk as SUSPECTED WRONG so the user can review it.\n"
            "\n"
            "Use this when you have evidence that a memory is wrong — for example a "
            "saved price, date, or claim that contradicts a page you just fetched, or "
            "that the user says is incorrect.\n"
            "\n"
            "IMPORTANT — this only MARKS the chunk. It does not change, remove, "
            "retract, or stop anything. The memory keeps being used exactly as "
            "before until the USER decides. You cannot remove a memory, and there "
            "is no step for you to take afterwards.\n"
            "\n"
            "So when you report this, say that you have flagged it for the user to "
            "review. Do NOT say you have retracted it, marked it false, or that it "
            "will be corrected once they confirm — none of that is yours to do, and "
            "it is not waiting on their confirmation.\n"
            "\n"
            "Pass the chunk id (the mem_XXXX in the injected header or a "
            "search_memory result) and a one-line reason citing your evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "Chunk id to flag, e.g. mem_1789785523339269"},
                "reason": {"type": "string", "description": "Why you believe it is wrong — cite the evidence (a URL, a correction from the user, a contradicting chunk id)"},
            },
            "required": ["chunk_id", "reason"],
        },
    },
}

RESTORE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        # Likewise renamed: this clears a flag, it does not restore a retraction.
        "name": "clear_bad_memory_flag",
        "description": (
            "Clear a 'bad memory' flag you previously raised on a chunk — for "
            "example when new evidence shows the memory was correct after all, or "
            "the user says so.\n"
            "\n"
            "This only removes the marker. Like flag_bad_memory it does not change "
            "what is used; whether a chunk is in use is the user's decision, made "
            "in the memory management page.\n"
            "\n"
            "Pass the chunk id you flagged."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "Flagged chunk id whose flag should be cleared"},
                "reason": {"type": "string", "description": "Why the flag should be cleared"},
            },
            "required": ["chunk_id"],
        },
    },
}

REMOVE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        # Only exposed when curation.allow_model_remove is on. Takes a chunk out
        # of circulation (injection + search skip it) — reversible by the user.
        "name": "remove_memory",
        "description": (
            "Remove a stored memory chunk so it is no longer used.\n"
            "\n"
            "Sets the chunk's 'removed' flag: memory injection and search_memory "
            "skip it from now on. The chunk is NOT deleted — the row and content "
            "stay, and the user can restore it from the memory management page if "
            "removal was a mistake.\n"
            "\n"
            "Use this only when you are CONFIDENT the memory is wrong or harmful — "
            "for example it contradicts a page you just fetched, or the user told "
            "you it is incorrect. If you merely suspect it, use flag_bad_memory "
            "instead so the user can decide.\n"
            "\n"
            "Pass the chunk id (the mem_XXXX in the injected header or a "
            "search_memory result) and a one-line reason citing your evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "Chunk id to remove, e.g. mem_1789785523339269"},
                "reason": {"type": "string", "description": "Why it should be removed — cite the evidence (URL, user correction, contradicting chunk id)"},
            },
            "required": ["chunk_id", "reason"],
        },
    },
}

NATIVE_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command on the Mneme host. Its working directory is the tools directory — the same place the write tool saves relative paths. Use an absolute path inside your writable scope (the model scope, the tools directory, or the run's workspace) to touch files elsewhere.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
            },
            "required": ["command"],
        },
    },
}

NATIVE_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "write",
        "description": "Write a file on the Mneme host. Relative paths are saved into the tools directory — the same directory the bash tool runs in. Use an absolute path inside your writable scope (the model scope, the tools directory, or the run's workspace) to write elsewhere. Returns the full path written.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to write. Relative paths go in the tools directory; absolute paths inside your writable scope go exactly there."},
                "content": {"type": "string", "description": "Full file contents"},
            },
            "required": ["file_path", "content"],
        },
    },
}

INSPECT_RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_run",
        "description": "Inspect the agent harness run ledger. With no run_id, lists recent runs (id, status, goal, created). With a run_id, returns that run's full detail: goal, status, error, usage, plan, tasks (with errors), steps (with errors), tool calls, artifacts, and checkpoints. Use to see what a run did, what failed, and why — e.g. after a run fails, inspect it to learn the exact error before retrying or starting a new run.",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Run to inspect (the run_... id). Omit to list recent runs."},
                "include_events": {"type": "boolean", "description": "Also include the raw event stream (default false)."},
            },
        },
    },
}

START_RUN_TOOL = {
    "type": "function",
    "function": {
        "name": "start_run",
        "description": "Start a harness run in the background. The harness plans the goal and executes it step by step on its own thread (structured planner/verify by default, or a free-form goal session with free_form=true). Returns immediately with the run id; the run keeps going after your reply. Use inspect_run(run_id=...) to check its status, tasks, errors, or result later. Use this to delegate a long multi-step job (e.g. 'write tests for X and run them', 'refactor Y across the repo') instead of doing it inline.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The goal for the run — a self-contained description of what to accomplish."},
                "free_form": {"type": "boolean", "description": "True for a free-form goal session (model drives across turns); false/omit for the structured planner/verify run. Default false."},
            },
            "required": ["goal"],
        },
    },
}

# Read-only server tools that are ALWAYS exposed (never stripped on hard-stop).
READONLY_SERVER_TOOLS = (SEARCH_MEMORY_TOOL, LIST_TOOLS_TOOL, READ_TOOL_TOOL, READ_IMAGE_TOOL, READ_FILE_TOOL, FETCH_URL_TOOL, WEB_SEARCH_TOOL, INSPECT_RUN_TOOL)

# Curation tools (retract/restore memory). Registered separately because they are
# gated by their own config flags rather than MNEME_TOOL_<NAME>, and because they
# mutate memory. The proxy installs the actual DB callbacks via
# set_curation_hooks() at startup; without hooks the tools report unavailable
# rather than silently doing nothing.
CURATION_TOOLS = (RETRACT_MEMORY_TOOL, RESTORE_MEMORY_TOOL)

_curation_hooks = {"retract": None, "restore": None, "remove": None,
                   "propose_allowed": False, "retract_allowed": False, "remove_allowed": False}


def set_curation_hooks(retract_fn, restore_fn, *, retract_allowed: bool, propose_allowed: bool,
                       remove_fn=None, remove_allowed: bool = False) -> None:
    """Install the proxy's curation callbacks + authority level."""
    _curation_hooks["retract"] = retract_fn
    _curation_hooks["restore"] = restore_fn
    _curation_hooks["remove"] = remove_fn
    _curation_hooks["retract_allowed"] = bool(retract_allowed)
    _curation_hooks["propose_allowed"] = bool(propose_allowed)
    _curation_hooks["remove_allowed"] = bool(remove_allowed)


def enabled_curation_tools():
    """The curation tools currently exposed to the model.

    Exposed when the model has ANY authority (direct retract, propose, or
    remove). flag_bad_memory + clear_bad_memory_flag come with propose/retract;
    remove_memory is added only when removal authority is granted.
    """
    if not (_curation_hooks["retract_allowed"] or _curation_hooks["propose_allowed"]
            or _curation_hooks["remove_allowed"]):
        return []
    if not os.environ.get("MNEME_MEMORY_ENABLED", "1") == "1":
        return []
    tools = []
    if _curation_hooks["retract_allowed"] or _curation_hooks["propose_allowed"]:
        flag = RETRACT_MEMORY_TOOL
        if _curation_hooks["remove_allowed"]:
            # flag_bad_memory's stock wording says "you cannot remove a memory",
            # which is wrong once remove_memory is available. Rewrite it so the
            # description never contradicts the model's actual authority.
            flag = json.loads(json.dumps(RETRACT_MEMORY_TOOL))
            flag["function"]["description"] = flag["function"]["description"].replace(
                "You cannot remove a memory, and there is no step for you to take afterwards.",
                "This tool only marks. If you are CONFIDENT the memory is wrong and "
                "want it out of use, call remove_memory instead.",
            )
        tools.append(flag)
        tools.append(RESTORE_MEMORY_TOOL)
    if _curation_hooks["remove_allowed"]:
        tools.append(REMOVE_MEMORY_TOOL)
    return tools


def _exec_retract_memory(args):
    cid = ((args or {}).get("chunk_id") or "").strip()
    reason = ((args or {}).get("reason") or "").strip()
    if not cid:
        return "[flag_bad_memory: chunk_id required]"
    fn = _curation_hooks.get("retract")
    if fn is None:
        return "[flag_bad_memory: memory curation is not available on this proxy]"
    try:
        out = fn(cid, reason)
        return out
    except Exception as e:
        return f"[flag_bad_memory error: {type(e).__name__}: {e}]"


def _exec_restore_memory(args):
    cid = ((args or {}).get("chunk_id") or "").strip()
    reason = ((args or {}).get("reason") or "").strip()
    if not cid:
        return "[clear_bad_memory_flag: chunk_id required]"
    fn = _curation_hooks.get("restore")
    if fn is None:
        return "[clear_bad_memory_flag: memory curation is not available on this proxy]"
    try:
        return fn(cid, reason)
    except Exception as e:
        return f"[clear_bad_memory_flag error: {type(e).__name__}: {e}]"


def _exec_remove_memory(args):
    cid = ((args or {}).get("chunk_id") or "").strip()
    reason = ((args or {}).get("reason") or "").strip()
    if not cid:
        return "[remove_memory: chunk_id required]"
    fn = _curation_hooks.get("remove")
    if fn is None:
        return "[remove_memory: memory removal is not available on this proxy]"
    try:
        return fn(cid, reason)
    except Exception as e:
        return f"[remove_memory error: {type(e).__name__}: {e}]"


# ─── Per-tool enable flags ─────────────────────────────────────────────
# Each read-only server tool can be disabled individually via config
# (tools.<name>: false -> MNEME_TOOL_<NAME>=0). bash/write are gated separately
# by NATIVE_TOOLS_MODE. Default: all enabled.

def _tool_enabled(name: str) -> bool:
    """Whether a read-only server tool is enabled (default on)."""
    if name == "search_memory" and os.environ.get("MNEME_MEMORY_ENABLED", "1") == "0":
        return False  # memory disabled — search_memory has nothing to search
    return os.environ.get(f"MNEME_TOOL_{name.upper()}", "1") == "1"


def enabled_readonly_tools():
    """The read-only server tools currently enabled (per-tool flags applied)."""
    return [t for t in READONLY_SERVER_TOOLS if _tool_enabled(_tool_name(t))]


def enabled_readonly_names():
    """Names of the read-only server tools currently enabled."""
    return {_tool_name(t) for t in enabled_readonly_tools()}


# ─── Tool assembly ───────────────────────────────────────────────────────

def _tool_name(t):
    return (t.get("function", {}) or {}).get("name", "")


def native_exec_names(client_tools):
    """Which native bootstrap tools (bash/write/start_run) to expose, given client tools.

    auto -> fill the gap (inject native bash only if the client lacks a bash,
            native write only if the client lacks a write).
    on   -> always both.  off -> neither (bash/write only).

    start_run is a harness-delegation tool, not shell execution — it is gated by
    its own flag (MNEME_TOOL_START_RUN) and is independent of NATIVE_TOOLS_MODE.
    """
    out = set()
    if NATIVE_TOOLS_MODE == "on":
        out.update({"bash", "write"})
    elif NATIVE_TOOLS_MODE != "off":
        client_names = {_tool_name(t) for t in (client_tools or [])}
        if "bash" not in client_names:
            out.add("bash")
        if "write" not in client_names:
            out.add("write")
    if os.environ.get("MNEME_TOOL_START_RUN", "1") == "1":
        out.add("start_run")
    return out


def assemble_tools(client_tools):
    """Full tool list: read-only server tools + native bootstrap + client passthrough.

    Deduped by name (read-only server tools win; client passthrough is skipped if
    a name is already present). This is what process_chat forwards to the model.
    """
    tools = []
    seen = set()

    def add(t):
        n = _tool_name(t)
        if n and n not in seen:
            tools.append(t)
            seen.add(n)

    for t in enabled_readonly_tools():
        add(t)
    _native_defs = {"bash": NATIVE_BASH_TOOL, "write": NATIVE_WRITE_TOOL, "start_run": START_RUN_TOOL}
    for n in ("bash", "write", "start_run"):
        if n in native_exec_names(client_tools):
            add(_native_defs[n])
    for t in (client_tools or []):
        add(t)
    # MCP tools (dynamic — added/removed at runtime via the MCP manager). Added
    # LAST so a name collision with a proxy or client tool is resolved in their
    # favour.
    for t in get_manager().tools():
        add(t)
    # Curation tools last (same collision policy — built-ins win). Only present
    # when the model has retraction authority (see set_curation_hooks).
    for t in enabled_curation_tools():
        add(t)
    return tools


def mcp_tool_names():
    """Names of tools currently exposed by connected MCP servers."""
    return get_manager().tool_names()


def call_mcp_tool(name, args):
    """Execute an MCP tool call; returns a result string."""
    return get_manager().call_tool(name, args)


def is_native_exec_name(name, client_tools):
    """True if `name` (bash/write) is executed server-side rather than forwarded."""
    return name in ("bash", "write") and name in native_exec_names(client_tools)


# ─── Native execution (server-side) ─────────────────────────────────────

def _bash_log_path() -> str:
    """Persistent log for every bash call. Background processes (servers started
    with ``&``) inherit this fd and keep writing after the foreground exits, so
    their stderr is never lost to a dead pipe. Lives in TOOLS_DIR (a --bind-mounted
    writable root), NOT /tmp — bwrap re-mounts /tmp as a fresh tmpfs per call, so a
    file the model writes to /tmp is invisible to its next command."""
    return os.path.join(TOOLS_DIR, "bash_output.log")


def _bash_log_tail(limit: int = 4000) -> str:
    """Last `limit` chars of the bash output log (for injecting into a failure)."""
    try:
        with open(_bash_log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _exec_bash(command):
    """Run a shell command inside a bubblewrap sandbox: the filesystem is
    read-only except for the model's writable roots (TOOLS_DIR + MODEL_SCOPE).
    Falls back to a plain subprocess when bwrap is unavailable.

    stdout+stderr are redirected to a persistent log file (not a pipe) so that
    background processes — servers the model starts with `&` — keep writing to a
    place the model can `cat` after this call returns (see ``_bash_log_path``)."""
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        os.makedirs(MODEL_SCOPE, exist_ok=True)
        os.makedirs(RUNS_ROOT, exist_ok=True)
        # A writable pip target inside the sandbox so `pip install <lib>` can
        # succeed despite the read-only root (the default site-packages is
        # read-only there). PYTHONPATH lets the model import what it installs.
        pylibs = os.path.join(TOOLS_DIR, "pylibs")
        os.makedirs(pylibs, exist_ok=True)
        env = dict(os.environ)
        env["PIP_TARGET"] = pylibs
        env["PYTHONPATH"] = pylibs + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        bwrap = shutil.which("bwrap")
        if bwrap:
            cmd = [bwrap, "--ro-bind", "/", "/"]
            for r in _writable_roots():
                cmd += ["--bind", r, r]
            cmd += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                    "--chdir", TOOLS_DIR, "bash", "-c", command]
        else:
            cmd = ["bash", "-c", command]

        log_path = _bash_log_path()
        # Bound the log: a runaway command or a leaked background process (a server
        # started with `&` that inherits this fd) can otherwise fill the disk. When
        # the log exceeds the cap, truncate it before this call so the new process
        # starts from a fresh file.
        try:
            if os.path.getsize(log_path) > _BASH_LOG_MAX:
                os.truncate(log_path, 0)
        except OSError:
            pass
        header = ("\n===== bash %s :: %s =====\n" % (time.strftime("%H:%M:%S"), command[:200])).encode("utf-8", "replace")
        with open(log_path, "ab") as logf:
            logf.write(header)
            logf.flush()
            start = logf.tell()
            p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                 cwd=TOOLS_DIR, env=env)
            try:
                p.wait(timeout=BASH_TIMEOUT)
                code = p.returncode
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait()
                except Exception:
                    pass
                code = -1
        with open(log_path, "rb") as f:
            f.seek(start)
            out = f.read().decode("utf-8", errors="replace").rstrip()
        if code == -1:
            return f"[bash timeout after {BASH_TIMEOUT}s — full output in bash_output.log]"
        return f"[exit {code}]\n{out}" if out else f"[exit {code}] (no output)"
    except Exception as e:
        return f"[bash error: {type(e).__name__}: {e}]"


def _exec_write(file_path, content):
    """Write a file on the proxy host, scoped to the model's writable area.
    Relative paths land in the tools dir; absolute paths are rejected if they
    fall outside TOOLS_DIR / MODEL_SCOPE / RUNS_ROOT (shared files stay read-only)."""
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        full = file_path if os.path.isabs(file_path) else os.path.join(TOOLS_DIR, file_path)
        if not _writable(full):
            record_blocked_write(full)
            return (f"[write blocked: {full} is outside the model write scope "
                    f"({MODEL_SCOPE}, {TOOLS_DIR}, and run workspaces under {RUNS_ROOT}). "
                    f"Write inside that scope instead.]")
        os.makedirs(os.path.dirname(full) or TOOLS_DIR, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File written: {full} ({len(content)} bytes)"
    except Exception as e:
        return f"[write error: {type(e).__name__}: {e}]"


def execute_native_tool(name, args):
    """Dispatch a native bash/write/start_run call server-side. Returns a result string."""
    if name == "bash":
        return _exec_bash((args or {}).get("command", ""))
    if name == "write":
        return _exec_write((args or {}).get("file_path", ""), (args or {}).get("content", ""))
    if name == "start_run":
        return _exec_start_run(args)
    return f"[unknown native tool: {name}]"


# ─── Tool registry (list / read / save) ─────────────────────────────────

def _registry_rows(problem_type=None):
    if db is None:
        return []
    q = "SELECT name, description, problem_type, script_path, success_count, last_used_at, script_source FROM tools WHERE retired=0"
    params = []
    if problem_type:
        q += " AND problem_type=?"
        params.append(problem_type)
    q += " ORDER BY success_count DESC, name"
    return db.execute(q, params).fetchall()


def _exec_list_tools(query=None, problem_type=None):
    rows = _registry_rows(problem_type)
    if not rows:
        return "(tool registry is empty — no tools built yet)"
    if query and embed is not None:
        qv = embed(query)
        if qv is not None:
            scored = []
            for name, desc, ptype, path, sc, lu, src in rows:
                tv = _tool_vector(name, desc)
                if tv is None:
                    continue
                sim = float(np.dot(qv, tv) / (np.linalg.norm(tv) + 1e-8))
                scored.append((sim, name, desc, ptype, path, sc, lu, src))
            scored.sort(key=lambda x: -x[0])
            rows = [(n, d, p, path, sc, lu, src) for _, n, d, p, path, sc, lu, src in scored[:TOOL_INJECT_MAX]]
    lines = [f"Tool registry ({len(rows)} tool(s)):"]
    for name, desc, ptype, path, sc, lu, src in rows:
        hostbound = " [host-bound: source not stored]" if not src else ""
        lines.append(f"- {name} [{ptype}] — {desc}")
        lines.append(f"    path: {path} | success: {sc} | last_used: {lu or 'never'}{hostbound}")
    return "\n".join(lines)


def _exec_read_tool(name):
    if db is None:
        return "(no tool database)"
    row = db.execute(
        "SELECT script_source, script_path, description FROM tools WHERE name=? AND retired=0",
        (name,),
    ).fetchone()
    if not row:
        return f"No tool named '{name}' in the registry."
    src, path, desc = row[0], row[1], row[2]
    if not src:
        return (f"Tool '{name}' ({desc}) is host-bound — its source is not stored in the registry, "
                f"only its path: {path}")
    return f"Source of tool '{name}' ({desc}):\n\n{src}"


_SEARCH_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def _ddg_search(query):
    """DuckDuckGo backend via the ddgs package. ddgs performs the vqd-token
    handshake that a raw POST to html.duckduckgo.com omits — that missing token
    is why DDG returns the 202 'anomaly' challenge to naive scrapers. Lazy
    import so the tool still works (Brave fallback) if ddgs isn't installed.
    Returns a results string, or None.

    Note: ddgs can occasionally block in native code (primp/libcurl) holding the
    GIL, so a hung search could freeze the proxy; DDGS(timeout=…) bounds each
    request. If this ever hangs in practice, isolate it in a subprocess (see
    Hermes's plugins/web/ddgs/provider.py for the reference implementation)."""
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    try:
        with DDGS(timeout=15) as client:
            hits = list(client.text(query, max_results=8))
    except Exception:
        return None
    if not hits:
        return None
    lines = [f"web results for '{query}':"]
    for i, h in enumerate(hits, 1):
        t = str(h.get("title") or "").strip()
        url = str(h.get("href") or h.get("url") or "").strip()
        body = str(h.get("body") or "").strip()
        lines.append(f"{i}. {t}\n   {url}\n   {body[:300]}")
    return "\n".join(lines)


def _brave_search(query):
    """Brave Search backend (no key; tolerates bots better than DDG/Bing)."""
    r = requests.get("https://search.brave.com/search", params={"q": query}, headers=_SEARCH_HEADERS, timeout=20)
    html = r.text or ""
    titles = _re.findall(r'<a href="(https?://[^"]+)"[^>]*>\s*<h[1-4][^>]*>(.*?)</h[1-4]>', html, _re.S)
    if not titles:
        return None
    descs = _re.findall(r'<section class="description[^"]*"[^>]*>(.*?)</section>', html, _re.S)
    seen = set()
    lines = [f"web results for '{query}':"]
    for i, (href, title) in enumerate(titles):
        t = _unescape(_re.sub(r'<[^>]+>', '', title)).strip()
        if not t or href in seen:
            continue
        seen.add(href)
        snip = _unescape(_re.sub(r'<[^>]+>', '', descs[i])).strip() if i < len(descs) else ""
        lines.append(f"{len(lines)}. {t}\n   {href}\n   {snip[:300]}")
        if len(lines) > 8:
            break
    return "\n".join(lines) if len(lines) > 1 else None


_SEARCH_CACHE = {}


def _exec_web_search(query):
    """Web search across free backends (DuckDuckGo primary, Brave fallback).

    Free search engines rate-limit bot IPs, so results are best-effort: a failed
    search degrades to a clear message (the model can fall back to bash+curl).
    Repeated queries are served from a small in-process cache to cut load."""
    query = (query or "").strip()
    if not query:
        return "[web_search: empty query]"
    if query in _SEARCH_CACHE:
        return _SEARCH_CACHE[query]
    for backend in (_ddg_search, _brave_search):
        try:
            out = backend(query)
            if out:
                _SEARCH_CACHE[query] = out
                return out
        except Exception:
            continue
    return "[web_search: no results (all backends rate-limited/blocked)]"


def _exec_read_file(path, start=None, end=None):
    """Read a file (optionally a line range), capped at 12000 chars, scoped to
    the browser root (the user's view)."""
    try:
        path = os.path.expanduser(path or "")
        full = os.path.realpath(path)
        if not _within(full, BROWSER_ROOT):
            return f"[read_file blocked: {path} is outside the browser scope ({BROWSER_ROOT})]"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        s = max(1, int(start) if start else 1)
        e = min(total, int(end) if end else total)
        text = "".join(lines[s - 1:e])
        if len(text) > 12000:
            text = (text[:9000]
                    + f"\n\n[... truncated: {len(text)} chars in lines {s}-{e} of {total}. Read a narrower range for the middle.]\n\n"
                    + text[-3000:])
        return text if text.strip() else f"[read_file: empty range (lines {s}-{e} of {total})]"
    except FileNotFoundError:
        return f"[read_file: no such file: {path}]"
    except Exception as e:
        return f"[read_file error: {type(e).__name__}: {e}]"


def _exec_fetch_url(url, render_js=False):
    """Fetch a URL and return clean text (HTML/CSS/JS stripped).

    Returns the FULL page text (generous cap for pathological pages). The proxy
    stages the full text into memory as page:<domain> chunks and forwards only a
    bounded head+tail window to the model, so the model isn't flooded but the
    entire page is retrievable via search_memory.

    With render_js=True the page is rendered in a headless Chromium first, so
    JavaScript-only pages (React/Streamlit apps, leaderboards) yield their real
    content instead of an empty shell.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return f"[fetch_url: invalid URL: {url}]"
    if render_js:
        return _exec_fetch_url_rendered(url)
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        r.raise_for_status()
        html = r.text or ""
        # Drop script/style blocks first, then strip remaining tags, then unescape.
        html = _re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=_re.IGNORECASE | _re.DOTALL)
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _unescape(text)
        text = _re.sub(r"[ \t\r\f\v]+", " ", text)
        text = _re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:300000] if text else "[fetch_url: empty page]"
    except Exception as e:
        return f"[fetch_url error: {type(e).__name__}: {e}]"


def _exec_fetch_url_rendered(url):
    """Render a URL in headless Chromium (playwright) and return its visible text."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "[fetch_url: render_js needs playwright — `pip install playwright && playwright install chromium`]"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass  # best-effort: some pages never go idle
                page.wait_for_timeout(1000)
                text = page.inner_text("body")
            finally:
                browser.close()
        text = _re.sub(r"[ \t\r\f\v]+", " ", text)
        text = _re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:300000] if text else "[fetch_url: rendered page is empty]"
    except Exception as e:
        return f"[fetch_url render error: {type(e).__name__}: {e}]"


def _exec_read_image(ref):
    """Read a stored image and return it as a data URL (so a vision model can see
    it again). `ref` is a hash or a path. Returns a data URL string, or an error."""
    ref = (ref or "").strip()
    if not ref:
        return "[read_image: no image reference]"
    img_dir = os.path.join(
        os.environ.get("MNEME_CHUNK_DIR") or os.path.expanduser("~/mneme/chunks"), "images")
    candidate = None
    if ref.startswith("/"):
        candidate = os.path.expanduser(ref)
    elif "/" in ref:
        candidate = os.path.expanduser(ref)
    else:
        # bare hash — find images/<hash>.*
        if os.path.isdir(img_dir):
            for fn in sorted(os.listdir(img_dir)):
                if fn.startswith(ref + "."):
                    candidate = os.path.join(img_dir, fn)
                    break
    if not candidate or not os.path.isfile(candidate):
        return f"[read_image: image not found: {ref}]"
    try:
        with open(candidate, "rb") as f:
            data = f.read()
    except Exception as e:
        return f"[read_image error: {type(e).__name__}: {e}]"
    mime = _sniff_mime(data) or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _format_run_detail(d):
    """Render a ledger.run_detail() dict into a bounded, readable text block."""
    r = d.get("run") or {}
    u = r.get("usage") or {}
    plan = r.get("plan") or {}
    out = [f"RUN {r.get('run_id')}"]
    out.append(f"  goal: {(r.get('goal') or '')[:200]}")
    out.append(f"  status: {r.get('status')}  attempt={r.get('attempt')}"
               f"  plan_source={(plan.get('source') if isinstance(plan, dict) else None)}")
    if r.get("error"):
        out.append(f"  error: {str(r.get('error'))[:300]}")
    out.append(f"  usage: model_calls={u.get('model_calls', 0)} tool_calls={u.get('tool_calls', 0)}"
               f" steps={u.get('steps', 0)} replans={u.get('replans', 0)}"
               f" failures={u.get('failures', 0)} runtime={u.get('runtime_s', 0)}s")
    out.append(f"  created: {(r.get('created_at') or '')[:19]}  updated: {(r.get('updated_at') or '')[:19]}")
    if isinstance(plan, dict) and plan.get("tasks"):
        out.append(f"  plan v{plan.get('version', 0)}: " + "; ".join(str(t) for t in plan["tasks"][:8]))
    tasks = d.get("tasks") or []
    if tasks:
        out.append(f"TASKS ({len(tasks)}):")
        for t in tasks:
            err = f" — {str(t.get('error') or '')[:140]}" if t.get("error") else ""
            out.append(f"  [{t.get('seq')}] {t.get('status')} att={t.get('attempts')} {(t.get('title') or '')[:70]}{err}")
    steps = d.get("steps") or []
    if steps:
        out.append(f"STEPS ({len(steps)}):")
        for s in steps:
            err = f" — {str(s.get('error') or '')[:140]}" if s.get("error") else ""
            out.append(f"  [{s.get('seq')}] {s.get('kind')} {s.get('status')}{err}")
    tcs = d.get("tool_calls") or []
    if tcs:
        out.append(f"TOOL CALLS ({len(tcs)}):")
        for c in tcs[-15:]:
            argstr = json.dumps(c.get("args") or {}, default=str)[:100]
            out.append(f"  {c.get('tool')} [{c.get('status') or 'ok'}] {argstr}")
    arts = d.get("artifacts") or []
    if arts:
        out.append(f"ARTIFACTS ({len(arts)}):")
        for a in arts:
            out.append(f"  {a.get('path')} ({a.get('size', 0)}B)")
    cps = d.get("checkpoints") or []
    if cps:
        out.append(f"CHECKPOINTS ({len(cps)}):")
        for c in cps:
            out.append(f"  [{c.get('seq')}] {str(c.get('reason') or '')[:60]} {(c.get('created_at') or '')[:19]}")
    if d.get("events"):
        evs = d["events"]
        out.append(f"EVENTS ({len(evs)}):")
        for e in evs[-20:]:
            out.append(f"  {(e.get('created_at') or '')[11:19]} {e.get('type')} "
                       f"{json.dumps(e.get('data') or {}, default=str)[:90]}")
    return "\n".join(out)


def _exec_inspect_run(args):
    """Inspect the harness run ledger (read-only). No run_id -> list recent runs."""
    if ledger is None:
        return "Harness is disabled or not initialized — there is no run ledger to inspect."
    args = args or {}
    run_id = str(args.get("run_id") or "").strip()
    include_events = bool(args.get("include_events"))
    try:
        if not run_id:
            runs = ledger.list_runs(limit=20)
            if not runs:
                return "No runs yet."
            lines = ["Recent runs (newest first):"]
            for r in runs:
                u = r.get("usage") or {}
                lines.append(f"  [{r.get('run_id')}] {r.get('status')} · calls={u.get('model_calls', 0)}"
                             f" failures={u.get('failures', 0)} · {(r.get('created_at') or '')[:19]}"
                             f" · {(r.get('goal') or '')[:80]}")
            return "\n".join(lines)
        d = ledger.run_detail(run_id, include_events=include_events)
        if d is None:
            return f"No such run: {run_id}"
        return _format_run_detail(d)
    except Exception as e:
        return f"[inspect_run error: {type(e).__name__}: {e}]"


def _exec_start_run(args):
    """Start a harness run in the background; return its id immediately."""
    if engine is None:
        return ("Harness is disabled or not initialized — there is no engine to "
                "start a run.")
    args = args or {}
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return "start_run requires a non-empty goal."
    free_form = bool(args.get("free_form"))
    try:
        run = engine.create(goal, free_form=free_form, start=True,
                            created_by="model:start_run")
        rid = run.get("run_id")
        status = run.get("status")
        return (f"Run started in the background: {rid} [{status}]. "
                f"The harness is planning and executing it on its own thread. "
                f"Check progress with inspect_run(run_id=\"{rid}\").")
    except Exception as e:
        return f"[start_run error: {type(e).__name__}: {e}]"


def execute_readonly_tool(name, args):
    """Dispatch a read-only registry tool (list_tools/read_tool/read_image/read_file/fetch_url) or web_search."""
    if name == "list_tools":
        return _exec_list_tools((args or {}).get("query") or None, (args or {}).get("problem_type") or None)
    if name == "read_tool":
        return _exec_read_tool((args or {}).get("name", ""))
    if name == "read_image":
        return _exec_read_image((args or {}).get("ref", ""))
    if name == "read_file":
        return _exec_read_file((args or {}).get("path", ""), (args or {}).get("start"), (args or {}).get("end"))
    if name == "fetch_url":
        return _exec_fetch_url((args or {}).get("url", ""), bool((args or {}).get("render_js")))
    if name == "web_search":
        return _exec_web_search((args or {}).get("query", ""))
    if name == "flag_bad_memory":
        return _exec_retract_memory(args)
    if name == "clear_bad_memory_flag":
        return _exec_restore_memory(args)
    if name == "remove_memory":
        return _exec_remove_memory(args)
    if name == "inspect_run":
        return _exec_inspect_run(args)
    return f"[unknown registry tool: {name}]"


def _tool_vector(name, description):
    """Embedding of a tool's identity (name + description), or None."""
    if embed is None or db is None:
        return None
    key = f"{name} {description}"
    row = db.execute("SELECT embedding FROM tools WHERE name=?", (name,)).fetchone()
    if row and row[0]:
        try:
            return np.frombuffer(row[0], dtype=np.float32)
        except Exception:
            return None
    return None


def save_tool(problem_type, name, description, script_path, db_=None, embed_=None):
    """Persist a built tool into the registry (canonical dir + source + embedding).

    Reads the script at `script_path` when reachable (native write, or same-host
    harness) and stores it as the authoritative ``script_source``; materializes a
    copy into the canonical tools dir. Returns the tool_id or None.

    `db_`/`embed_` override the module-level bindings (used by tests).
    """
    _db = db_ if db_ is not None else db
    _embed = embed_ if embed_ is not None else embed
    if _db is None:
        return None
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        source = ""
        canonical = script_path or ""
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                source = f.read()
            # Materialize a canonical copy so native bash can always re-run it.
            canonical = os.path.join(TOOLS_DIR, name)
            with open(canonical, "w", encoding="utf-8") as f:
                f.write(source)
        except Exception:
            # Cross-host / unreachable path: keep the recorded path, no source.
            canonical = script_path or ""

        vec = None
        if _embed is not None:
            try:
                vec = _embed(f"{name} {description}")
            except Exception:
                vec = None
        emb_blob = vec.astype(np.float32).tobytes() if vec is not None else None

        now = _now()
        existing = _db.execute("SELECT tool_id FROM tools WHERE name=?", (name,)).fetchone()
        if existing:
            _db.execute(
                "UPDATE tools SET problem_type=?, description=?, script_path=?, script_source=?, "
                "embedding=?, last_used_at=? WHERE name=?",
                (problem_type, description, canonical, source, emb_blob, now, name),
            )
            tool_id = existing[0]
        else:
            tool_id = f"tool_{int(__import__('time').time() * 1000)}_{os.urandom(3).hex()}"
            _db.execute(
                "INSERT INTO tools (tool_id, problem_type, name, description, script_path, script_source, "
                "tested_at, success_count, retired, embedding, last_used_at) VALUES (?,?,?,?,?,?,?,1,0,?,?)",
                (tool_id, problem_type, name, description, canonical, source, now, emb_blob, now),
            )
        _db.commit()
        print(f"  [TOOL-SAVED] {name} -> {canonical} (for '{problem_type}')", flush=True)
        return tool_id
    except Exception as e:
        print(f"  [TOOL-SAVED][ERR] {e}", flush=True)
        return None


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ─── Retrieval-gated injection ──────────────────────────────────────────

def _retrieve_relevant_tools(query):
    """Top tools whose embedding is close enough to `query`. List of tuples."""
    if embed is None or db is None:
        return []
    qv = embed(query)
    if qv is None:
        return []
    rows = db.execute(
        "SELECT name, description, problem_type, script_path, embedding FROM tools WHERE retired=0 AND embedding IS NOT NULL"
    ).fetchall()
    scored = []
    for name, desc, ptype, path, emb in rows:
        if not emb:
            continue
        try:
            v = np.frombuffer(emb, dtype=np.float32)
        except Exception:
            continue
        if v.shape[0] == 0:
            continue
        sim = float(np.dot(qv, v) / (np.linalg.norm(v) + 1e-8))  # qv already normalized
        if sim >= TOOL_INJECT_MIN_SIM:
            scored.append((sim, name, desc, ptype, path))
    scored.sort(key=lambda x: -x[0])
    return scored[:TOOL_INJECT_MAX]


def inject_relevant_tools(query):
    """Injection text for relevant built tools ('' if none above threshold)."""
    tools = _retrieve_relevant_tools(query)
    if not tools:
        return ""
    lines = ["\n[Built tools you can reuse]"]
    budget = TOOL_INJECT_TOKENS
    for sim, name, desc, ptype, path in tools:
        line = f"- {name} ({ptype}): {desc} — bash {path}"
        # rough token cost (~1.3 tok/word); stop before blowing the budget
        if int(len(line.split()) * 1.3) > budget:
            break
        lines.append(line)
        budget -= int(len(line.split()) * 1.3)
    return "\n".join(lines)

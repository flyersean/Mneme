"""Mneme tool system — native bootstrap tools, tool registry, retrieval injection.

Three responsibilities, per docs/tool-system.md:

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

import os
import json
import subprocess
import re as _re
from html import unescape as _unescape

import requests
import numpy as np

from mneme.util import _extract_text

# ─── Config (env-set by mneme_proxy's config loader before first use) ────
NATIVE_TOOLS_MODE = os.environ.get("MNEME_NATIVE_TOOLS", "auto")   # auto | on | off
TOOLS_DIR = os.path.expanduser(os.environ.get("MNEME_TOOLS_DIR", "~/mneme/chunks/tools"))
BASH_TIMEOUT = int(os.environ.get("MNEME_TOOLS_BASH_TIMEOUT", "30"))
TOOL_INJECT_MIN_SIM = float(os.environ.get("MNEME_TOOL_INJECT_MIN_SIMILARITY", "0.75"))
TOOL_INJECT_MAX = int(os.environ.get("MNEME_TOOL_INJECT_MAX", "3"))
TOOL_INJECT_TOKENS = int(os.environ.get("MNEME_TOOL_INJECT_TOKENS", "600"))

# Bound by mneme_proxy after import (see _apply_config / startup).
db = None          # sqlite3.Connection
embed = None       # callable: str -> np.ndarray (normalized) | None


def reload_config():
    """Re-read the tools config from env.

    mneme_proxy imports this module BEFORE load_config() applies the config file
    (which sets the MNEME_* env vars), so the module-level defaults above are
    stale. mneme_proxy calls this right after load_config() to refresh them.
    """
    global NATIVE_TOOLS_MODE, TOOLS_DIR, BASH_TIMEOUT
    global TOOL_INJECT_MIN_SIM, TOOL_INJECT_MAX, TOOL_INJECT_TOKENS
    NATIVE_TOOLS_MODE = os.environ.get("MNEME_NATIVE_TOOLS", "auto")
    TOOLS_DIR = os.path.expanduser(os.environ.get("MNEME_TOOLS_DIR", "~/mneme/chunks/tools"))
    BASH_TIMEOUT = int(os.environ.get("MNEME_TOOLS_BASH_TIMEOUT", "30"))
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

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information, techniques, or tools. Use to research how to solve a problem you cannot solve from memory — find a library, a service, a known solution, or documentation. Returns the top results with titles, URLs, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for — a specific question or keywords"},
            },
            "required": ["query"],
        },
    },
}

NATIVE_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command on the Mneme host. Use to run a tool you built (e.g. python3 script.py) or inspect the environment.",
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
        "description": "Write a file on the Mneme host. Use to save a script you are building. Relative paths land in the tools directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to write (absolute, or relative to the tools dir)"},
                "content": {"type": "string", "description": "Full file contents"},
            },
            "required": ["file_path", "content"],
        },
    },
}

# Read-only server tools that are ALWAYS exposed (never stripped on hard-stop).
READONLY_SERVER_TOOLS = (SEARCH_MEMORY_TOOL, LIST_TOOLS_TOOL, READ_TOOL_TOOL, WEB_SEARCH_TOOL)


# ─── Tool assembly ───────────────────────────────────────────────────────

def _tool_name(t):
    return (t.get("function", {}) or {}).get("name", "")


def native_exec_names(client_tools):
    """Which native bootstrap tools (bash/write) to expose, given client tools.

    auto -> fill the gap (inject native bash only if the client lacks a bash,
            native write only if the client lacks a write).
    on   -> always both.  off -> neither.
    """
    if NATIVE_TOOLS_MODE == "on":
        return {"bash", "write"}
    if NATIVE_TOOLS_MODE == "off":
        return set()
    client_names = {_tool_name(t) for t in (client_tools or [])}
    out = set()
    if "bash" not in client_names:
        out.add("bash")
    if "write" not in client_names:
        out.add("write")
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

    for t in READONLY_SERVER_TOOLS:
        add(t)
    for n in ("bash", "write"):
        if n in native_exec_names(client_tools):
            add(NATIVE_BASH_TOOL if n == "bash" else NATIVE_WRITE_TOOL)
    for t in (client_tools or []):
        add(t)
    return tools


def is_native_exec_name(name, client_tools):
    """True if `name` (bash/write) is executed server-side rather than forwarded."""
    return name in ("bash", "write") and name in native_exec_names(client_tools)


# ─── Native execution (server-side) ─────────────────────────────────────

def _exec_bash(command):
    """Run a shell command on the proxy host. Returns a single result string."""
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        p = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=BASH_TIMEOUT, cwd=TOOLS_DIR,
        )
        out = (p.stdout or "").rstrip()
        if p.stderr:
            out += ("\n[stderr] " + p.stderr.rstrip()) if out else ("[stderr] " + p.stderr.rstrip())
        return f"[exit {p.returncode}]\n{out}" if out else f"[exit {p.returncode}] (no output)"
    except subprocess.TimeoutExpired:
        return f"[bash timeout after {BASH_TIMEOUT}s]"
    except Exception as e:
        return f"[bash error: {type(e).__name__}: {e}]"


def _exec_write(file_path, content):
    """Write a file on the proxy host. Relative paths land in the tools dir."""
    try:
        os.makedirs(TOOLS_DIR, exist_ok=True)
        full = file_path if os.path.isabs(file_path) else os.path.join(TOOLS_DIR, file_path)
        os.makedirs(os.path.dirname(full) or TOOLS_DIR, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File written: {full} ({len(content)} bytes)"
    except Exception as e:
        return f"[write error: {type(e).__name__}: {e}]"


def execute_native_tool(name, args):
    """Dispatch a native bash/write call server-side. Returns a result string."""
    if name == "bash":
        return _exec_bash((args or {}).get("command", ""))
    if name == "write":
        return _exec_write((args or {}).get("file_path", ""), (args or {}).get("content", ""))
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
    """DuckDuckGo HTML backend (no key). Returns a results string, or None."""
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=_SEARCH_HEADERS, timeout=20)
    html = r.text or ""
    titles = _re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, _re.S)
    if not titles:
        return None
    snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, _re.S)
    lines = [f"web results for '{query}':"]
    for i, (href, title) in enumerate(titles[:8]):
        t = _unescape(_re.sub(r'<[^>]+>', '', title)).strip()
        snip = _unescape(_re.sub(r'<[^>]+>', '', snippets[i])).strip() if i < len(snippets) else ""
        lines.append(f"{i + 1}. {t}\n   {_unescape(href)}\n   {snip}")
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


def execute_readonly_tool(name, args):
    """Dispatch a read-only registry tool (list_tools/read_tool) or web_search."""
    if name == "list_tools":
        return _exec_list_tools((args or {}).get("query") or None, (args or {}).get("problem_type") or None)
    if name == "read_tool":
        return _exec_read_tool((args or {}).get("name", ""))
    if name == "web_search":
        return _exec_web_search((args or {}).get("query", ""))
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

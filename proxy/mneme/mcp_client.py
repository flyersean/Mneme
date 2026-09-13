"""Minimal MCP client + manager for the Mneme proxy.

Connects to MCP servers (stdio + streamable-HTTP) and exposes their tools to the
model, so "install an MCP tool and it just works" holds for any language/runtime
— the server speaks JSON-RPC, the proxy doesn't care what it's written in.

Each server runs its asyncio session in a dedicated daemon thread; the synchronous
Flask request path calls tools via asyncio.run_coroutine_threadsafe. Servers are
added/removed at RUNTIME (no restart): `get_manager().add(...)` / `.remove(...)`
/ `.reconcile(configs)`. The tool list is re-read per request by assemble_tools,
so a newly-added server's tools appear on the next turn.
"""

import asyncio
import threading
import time

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _HAS_MCP = True
except ImportError:  # pragma: no cover — exercised only when mcp isn't installed
    _HAS_MCP = False


def _to_openai_tool(tool):
    """Convert an MCP Tool to OpenAI function-calling format."""
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    # OpenAI requires a JSON Schema object here; MCP already provides one.
    if not isinstance(schema, dict) or "type" not in schema:
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": schema,
        },
    }


def _content_to_text(result):
    """Flatten an MCP call result's content blocks into one string."""
    parts = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "type", None)
        if t == "text":
            parts.append(getattr(c, "text", "") or "")
        elif t == "image":
            parts.append("[image]")
        elif t == "resource":
            parts.append("[resource: %s]" % getattr(c, "resource", ""))
        else:
            parts.append(str(c))
    body = "\n".join(p for p in parts if p) if parts else "(no output)"
    if getattr(result, "isError", False):
        return "[mcp error] " + (body or "unknown error")
    return body


class MCPServer:
    """One connected MCP server, hosted in its own asyncio worker thread."""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = dict(cfg or {})
        self._loop = None
        self._thread = None
        self._session = None
        self._stop = None
        self._tools = []
        self._ready = False
        self._error = None
        self._call_lock = threading.Lock()

    @property
    def ready(self):
        return self._ready

    @property
    def tools(self):
        return list(self._tools)

    @property
    def error(self):
        return self._error

    def start(self):
        """Spawn the worker thread and begin connecting (non-blocking)."""
        if not _HAS_MCP:
            self._error = "mcp SDK not installed"
            return False
        self._thread = threading.Thread(target=self._bg, name=f"mcp-{self.name}", daemon=True)
        self._thread.start()
        return True

    def wait_ready(self, timeout=20.0):
        """Block until connected (or errored), up to `timeout` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready or self._error:
                return self._ready
            time.sleep(0.05)
        return self._ready

    # ── worker thread + event loop ─────────────────────────────────────────
    def _bg(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # pragma: no cover — safety net
            self._error = f"{type(e).__name__}: {e}"
            self._ready = False

    async def _serve(self):
        try:
            if self.cfg.get("command"):
                params = StdioServerParameters(
                    command=self.cfg["command"],
                    args=list(self.cfg.get("args") or []),
                    env=self.cfg.get("env") or None,
                )
                cm = stdio_client(params)
            else:
                from mcp.client.streamable_http import streamablehttp_client
                cm = streamablehttp_client(self.cfg["url"])
            async with cm as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tools = [_to_openai_tool(t) for t in (listed.tools or [])]
                    self._session = session
                    self._ready = True
                    await self._stop.wait()  # hold the session open until stop()
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self._ready = False
        finally:
            self._session = None

    # ── called from the Flask request threads ──────────────────────────────
    def call_tool(self, name, args, timeout=120):
        if not (self._ready and self._loop and self._session):
            return "[mcp:%s] server not ready%s" % (
                self.name, (" (" + self._error + ")" if self._error else ""))

        async def _call():
            return await self._session.call_tool(name, args or {})

        with self._call_lock:  # serialize calls per server (single stdio pipe)
            try:
                fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
                result = fut.result(timeout=timeout)
                return _content_to_text(result)
            except Exception as e:
                return "[mcp:%s] call failed: %s: %s" % (self.name, type(e).__name__, e)

    def stop(self):
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


class MCPManager:
    """Runtime registry of connected MCP servers, safe for hot add/remove."""

    def __init__(self):
        self._servers = {}
        self._lock = threading.Lock()

    def add(self, name, cfg):
        """Add (or replace) a server. Returns the MCPServer. Non-blocking."""
        with self._lock:
            self._remove_locked(name)
            srv = MCPServer(name, cfg)
            srv.start()
            self._servers[name] = srv
            return srv

    def remove(self, name):
        with self._lock:
            return self._remove_locked(name)

    def _remove_locked(self, name):
        srv = self._servers.pop(name, None)
        if srv:
            srv.stop()
        return srv

    def names(self):
        with self._lock:
            return list(self._servers)

    def status(self):
        with self._lock:
            out = {}
            for n, s in self._servers.items():
                out[n] = {
                    "ready": s.ready,
                    "error": s.error,
                    "tools": [t["function"]["name"] for t in s.tools],
                }
            return out

    def tools(self):
        """All ready servers' tools, in OpenAI format (for assemble_tools)."""
        out = []
        with self._lock:
            for srv in self._servers.values():
                if srv.ready:
                    out.extend(srv.tools)
        return out

    def tool_names(self):
        return {t["function"]["name"] for t in self.tools()}

    def call_tool(self, name, args):
        with self._lock:
            for srv in self._servers.values():
                if name in {t["function"]["name"] for t in srv.tools}:
                    target = srv
                    break
            else:
                return "[mcp] unknown tool: %s" % name
        return target.call_tool(name, args)

    def reconcile(self, configs):
        """Bring the running set in line with `configs` (list of {name, ...}).

        Adds new servers, updates changed ones, removes ones no longer listed.
        Unchanged servers are left running.
        """
        configs = [c for c in (configs or []) if isinstance(c, dict) and c.get("name")]
        with self._lock:
            wanted = {c["name"]: c for c in configs}
            for name in list(self._servers):
                if name not in wanted:
                    self._remove_locked(name)
            for name, cfg in wanted.items():
                cur = self._servers.get(name)
                if cur is not None and cur.cfg == dict(cfg):
                    continue  # unchanged
                self._remove_locked(name)
                srv = MCPServer(name, dict(cfg))
                srv.start()
                self._servers[name] = srv

    def shutdown(self):
        with self._lock:
            for name in list(self._servers):
                self._remove_locked(name)


_manager = None


def get_manager():
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager

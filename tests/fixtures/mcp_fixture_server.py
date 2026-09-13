"""A tiny stdio MCP server used as a test fixture for the Mneme MCP client.

Spawns via `python tests/fixtures/mcp_fixture_server.py`; speaks MCP over stdio.
Exposes two tools: echo (returns its text) and add (returns a + b).
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("mneme-fixture")


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def add(a: int, b: int) -> int:
    """Return a + b."""
    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")

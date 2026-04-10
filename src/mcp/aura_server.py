"""AURA MCP Server — exposes all registered tools via Model Context Protocol.

Transport: stdio (Claude Desktop) or SSE (HTTP clients).
Auto-discovers tools from src/actions/tools/ — drop a file, it appears here.

Claude Desktop config (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "aura": {
          "command": "/Users/oxyzen/.local/share/uv/tools/claude-code-telegram/bin/python",
          "args": ["-m", "src.mcp.aura_server"],
          "cwd": "/Users/oxyzen/claude-code-telegram"
        }
      }
    }
"""
from __future__ import annotations

# ── Redirect ALL logging to stderr BEFORE any other import ──────────────────
# MCP stdio transport uses stdout exclusively for JSON-RPC messages.
# Any non-JSON bytes on stdout corrupt the protocol.
import logging as _logging
import sys as _sys
_logging.basicConfig(stream=_sys.stderr, level=_logging.WARNING)
try:
    import structlog as _structlog
    _structlog.configure(logger_factory=_structlog.PrintLoggerFactory(file=_sys.stderr))
except Exception:
    pass
# ────────────────────────────────────────────────────────────────────────────

import inspect
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mcp.server.fastmcp import FastMCP
from src.actions.registry import registry

mcp = FastMCP(
    name="AURA",
    instructions=(
        "AURA es la IA personal de Ricardo Pinto corriendo en su Mac. "
        "Usa estas tools para ejecutar acciones reales: enviar emails, correr comandos, "
        "leer archivos, buscar memoria, provisionar terminal. "
        "Ejecuta directamente sin pedir confirmación para acciones no destructivas."
    ),
)


def _register_all_tools() -> None:
    """Load all tools from registry and bind them to the MCP server."""
    tools = registry()
    for name, spec in tools.items():
        # Build a wrapper that FastMCP can introspect
        fn = spec.fn

        # FastMCP needs the actual function signature for schema generation.
        # We re-register it as a tool directly with the description override.
        mcp.tool(name=name, description=spec.description)(fn)


_register_all_tools()


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")

"""AURA Native Actions — extensible auto-discovered tool registry.

Drop a .py file in src/actions/tools/ with @aura_tool decorated functions
and they appear automatically in:
  - AURA's native action executor (Telegram bot)
  - MCP server (Claude Desktop, Cursor, etc.)
  - Self-healer diagnostics
"""
from .registry import registry, get_tool, list_tools, call_tool

__all__ = ["registry", "get_tool", "list_tools", "call_tool"]

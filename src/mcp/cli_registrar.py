"""Auto-register AURA MCP server with all available CLI tools.

Called at bot startup. Idempotent — safe to run multiple times.
Supports: Claude Desktop, Claude Code, OpenCode, Gemini CLI.

When a new CLI adds MCP support, add a registration function here.
No other files need to change.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List

import structlog

logger = structlog.get_logger()

_AURA_MCP_COMMAND = "/Users/oxyzen/.local/share/uv/tools/claude-code-telegram/bin/python"
_AURA_MCP_ARGS = ["-m", "src.mcp.aura_server"]
_AURA_MCP_CWD = "/Users/oxyzen/claude-code-telegram"
_AURA_MCP_ENV = {"PYTHONPATH": _AURA_MCP_CWD}
_AURA_DESCRIPTION = "AURA personal tools: email, bash, files, git, memory, terminal"


def _register_claude_desktop() -> str:
    """Register with Claude Desktop app."""
    cfg_path = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if not cfg_path.exists():
        return "skip: Claude Desktop not installed"

    cfg = json.loads(cfg_path.read_text())
    existing = cfg.get("mcpServers", {}).get("aura", {})
    if existing.get("command") == _AURA_MCP_COMMAND:
        return "already registered"

    cfg.setdefault("mcpServers", {})["aura"] = {
        "command": _AURA_MCP_COMMAND,
        "args": _AURA_MCP_ARGS,
        "cwd": _AURA_MCP_CWD,
        "env": _AURA_MCP_ENV,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return "registered"


def _register_opencode() -> str:
    """Register with OpenCode CLI."""
    cfg_path = Path.home() / ".config/opencode/opencode.json"
    if not cfg_path.exists():
        return "skip: OpenCode not installed"

    cfg = json.loads(cfg_path.read_text())
    existing = cfg.get("mcp", {}).get("aura", {})
    if existing.get("command") == _AURA_MCP_COMMAND:
        return "already registered"

    cfg.setdefault("mcp", {})["aura"] = {
        "type": "local",
        "command": _AURA_MCP_COMMAND,
        "args": _AURA_MCP_ARGS,
        "cwd": _AURA_MCP_CWD,
        "environment": _AURA_MCP_ENV,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return "registered"


def _register_gemini_cli() -> str:
    """Register with Gemini CLI via 'gemini mcp add'."""
    # Check if already registered
    settings_path = Path.home() / ".gemini/settings.json"
    if settings_path.exists():
        try:
            s = json.loads(settings_path.read_text())
            servers = s.get("mcpServers", [])
            if isinstance(servers, dict) and "aura" in servers:
                return "already registered"
            if isinstance(servers, list):
                for srv in servers:
                    if isinstance(srv, dict) and srv.get("name") == "aura":
                        return "already registered"
        except Exception:
            pass

    gemini_bin = subprocess.run(
        ["which", "gemini"], capture_output=True, text=True
    ).stdout.strip()
    if not gemini_bin:
        return "skip: gemini CLI not found"

    try:
        result = subprocess.run(
            [
                gemini_bin, "mcp", "add", "aura",
                _AURA_MCP_COMMAND, *_AURA_MCP_ARGS,
                "--scope", "user",
                "--trust",
                "--description", _AURA_DESCRIPTION,
                "-e", f"PYTHONPATH={_AURA_MCP_CWD}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return "registered"
        return f"error: {result.stderr.strip()[:100]}"
    except Exception as e:
        return f"error: {e}"


def _register_claude_code() -> str:
    """Register with Claude Code CLI settings."""
    settings_path = Path.home() / ".claude/settings.json"
    if not settings_path.exists():
        return "skip: Claude Code settings not found"

    cfg = json.loads(settings_path.read_text())
    existing = cfg.get("mcpServers", {}).get("aura", {})
    if existing.get("command") == _AURA_MCP_COMMAND:
        return "already registered"

    cfg.setdefault("mcpServers", {})["aura"] = {
        "command": _AURA_MCP_COMMAND,
        "args": _AURA_MCP_ARGS + ["stdio"],
        "cwd": _AURA_MCP_CWD,
        "env": _AURA_MCP_ENV,
    }
    settings_path.write_text(json.dumps(cfg, indent=2))
    return "registered"


# ── Registry of all CLI registrars ────────────────────────────────────────────
# Add new CLIs here — no other file needs to change.
_REGISTRARS = {
    "Claude Desktop": _register_claude_desktop,
    "OpenCode":       _register_opencode,
    "Gemini CLI":     _register_gemini_cli,
    "Claude Code":    _register_claude_code,
}


def register_all() -> dict[str, str]:
    """Register AURA MCP with all available CLIs. Returns {cli: status}."""
    results = {}
    for name, fn in _REGISTRARS.items():
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = f"error: {e}"
    logger.info("mcp_cli_registration", results=results)
    return results

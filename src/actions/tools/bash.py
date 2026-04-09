"""Bash execution tool — runs commands on Ricardo's Mac."""
from __future__ import annotations
import asyncio
import os
from src.actions.registry import aura_tool

_APPROVED_DIR = os.path.expanduser("~")
_TIMEOUT = 30


@aura_tool(
    name="bash_run",
    description="Execute a bash command on Ricardo's Mac and return stdout/stderr.",
    category="system",
    parameters={
        "command": {"type": "str", "description": "Bash command to execute"},
        "timeout": {"type": "int", "description": "Timeout in seconds (default 30)"},
    },
)
async def bash_run(command: str, timeout: int = _TIMEOUT) -> str:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_APPROVED_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        result = out or err or "(no output)"
        return result[:3800]
    except asyncio.TimeoutError:
        proc.kill()
        return f"⏱ Timeout after {timeout}s"

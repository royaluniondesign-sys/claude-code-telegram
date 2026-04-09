"""Termora terminal provisioning tool."""
from __future__ import annotations
from src.actions.registry import aura_tool


@aura_tool(
    name="get_terminal_url",
    description="Get a one-click terminal URL for Ricardo's Mac via Termora.",
    category="system",
    parameters={},
)
async def get_terminal_url() -> str:
    import asyncio
    proc = await asyncio.create_subprocess_shell(
        "curl -s http://localhost:4030/api/info 2>/dev/null",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        import json
        data = json.loads(out.decode())
        auth_url = data.get("authUrl")
        if auth_url:
            return f"Terminal: {auth_url}"
        return "Termora activo pero sin tunnelUrl. Verifica ngrok/ssh tunnel."
    except Exception:
        return "Termora no disponible en puerto 4030. Inícialo con: cd ~/Projects/termora && npm run dev"

"""Mesh Broadcaster — formats and delivers AURA↔Hermes exchanges to Ricardo's Telegram.

Single responsibility: take an exchange (who said what, reply, timing) and send it
to Ricardo in a readable format. All mesh-visible communication goes through here.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_MESH_LOG = Path.home() / ".aura" / "memory" / "mesh-log.md"


def _log(entry: str) -> None:
    try:
        _MESH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_MESH_LOG, "a") as f:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            f.write(f"\n[{ts}] {entry}\n")
    except Exception:
        pass


async def broadcast_exchange(
    *,
    sender: str,
    receiver: str,
    message: str,
    reply: str,
    elapsed_s: float,
    important: bool = False,
) -> None:
    """Send a formatted AURA↔Hermes exchange to Ricardo's Telegram."""
    from src.api.mesh_state import get_mesh_bot, get_owner_chat_id

    bot = get_mesh_bot()
    chat_id = get_owner_chat_id()
    if not bot or not chat_id:
        return

    icon = "⚠️" if important else "🕸"
    elapsed_str = f"{int(elapsed_s)}s" if elapsed_s >= 1 else f"{int(elapsed_s * 1000)}ms"

    sender_icon = {"aura": "✨", "hermes": "⚡"}.get(sender.lower(), "🤖")
    recv_icon   = {"aura": "✨", "hermes": "⚡"}.get(receiver.lower(), "🤖")

    msg_preview  = message[:300] + ("…" if len(message) > 300 else "")
    reply_preview = reply[:600] + ("…" if len(reply) > 600 else "")

    text = (
        f"{icon} <b>{sender_icon} {sender.capitalize()} → {recv_icon} {receiver.capitalize()}:</b>\n"
        f"<i>{msg_preview}</i>\n\n"
        f"<b>{recv_icon} {receiver.capitalize()} ({elapsed_str}):</b>\n"
        f"{reply_preview}"
    )

    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        _log(f"{sender.upper()}→{receiver.upper()}: {message[:60]} | reply: {reply[:60]}")
    except Exception as e:
        logger.warning("mesh_broadcast_error", error=str(e))


async def broadcast_alert(
    *,
    from_agent: str,
    message: str,
    hint: str = "",
) -> None:
    """Send an important alert from any agent to Ricardo."""
    from src.api.mesh_state import get_mesh_bot, get_owner_chat_id

    bot = get_mesh_bot()
    chat_id = get_owner_chat_id()
    if not bot or not chat_id:
        _queue_to_file(from_agent, message)
        return

    agent_icon = {"aura": "✨", "hermes": "⚡"}.get(from_agent.lower(), "🤖")
    text = f"⚠️ <b>ALERTA — {agent_icon} {from_agent.capitalize()}:</b>\n\n{message}"
    if hint:
        text += f"\n\n<i>{hint}</i>"

    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        _log(f"ALERT from {from_agent.upper()}: {message[:80]}")
    except Exception as e:
        logger.warning("mesh_alert_error", error=str(e))


def _queue_to_file(from_agent: str, message: str) -> None:
    import json
    inbox = Path.home() / ".aura" / "mesh" / "inbox.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    items: list = []
    if inbox.exists():
        try:
            items = json.loads(inbox.read_text())
        except Exception:
            items = []
    items.append({"from": from_agent, "message": message, "ts": datetime.now(UTC).isoformat()})
    inbox.write_text(json.dumps(items, ensure_ascii=False, indent=2))

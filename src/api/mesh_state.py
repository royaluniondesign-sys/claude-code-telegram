"""Shared mesh state — singleton wiring telegram bot into API endpoints.

Set once at startup (in main.py run_application), read anywhere in api/.
"""
from __future__ import annotations

from typing import Any, Optional

# Telegram Bot instance (set after bot.initialize())
_telegram_bot: Optional[Any] = None
_owner_chat_id: int = 0


def set_mesh_bot(bot: Any, owner_chat_id: int) -> None:
    global _telegram_bot, _owner_chat_id
    _telegram_bot = bot
    _owner_chat_id = owner_chat_id


def get_mesh_bot() -> Optional[Any]:
    return _telegram_bot


def get_owner_chat_id() -> int:
    return _owner_chat_id

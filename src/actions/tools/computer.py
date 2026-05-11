"""Computer control tool — full keyboard/mouse automation via PyAutoGUI."""
from __future__ import annotations

import asyncio
import os
from src.actions.registry import aura_tool


@aura_tool(
    name="computer_control",
    description=(
        "Full keyboard/mouse control of Ricardo's Mac. "
        "Actions: type, smart_type, click, double_click, right_click, move, drag, "
        "hotkey (e.g. 'command+c'), press (single key), scroll, copy, paste, "
        "screenshot, wait, clear_field, focus_window, open_app, "
        "screen_find (AI element finder), screen_click (AI find+click), random_data."
    ),
    category="system",
    parameters={
        "action": {"type": "str", "description": "Action: type/click/hotkey/scroll/focus_window/open_app/screen_click/etc"},
        "text": {"type": "str", "description": "Text to type or paste", "optional": True},
        "x": {"type": "int", "description": "Screen X coordinate", "optional": True},
        "y": {"type": "int", "description": "Screen Y coordinate", "optional": True},
        "keys": {"type": "str", "description": "Hotkey combo e.g. 'command+c'", "optional": True},
        "key": {"type": "str", "description": "Single key name e.g. 'enter'", "optional": True},
        "direction": {"type": "str", "description": "Scroll direction: up/down/left/right", "optional": True},
        "amount": {"type": "int", "description": "Scroll amount (default 3)", "optional": True},
        "title": {"type": "str", "description": "Window title for focus_window", "optional": True},
        "app": {"type": "str", "description": "App name for open_app", "optional": True},
        "description": {"type": "str", "description": "Element description for screen_find/screen_click", "optional": True},
        "seconds": {"type": "float", "description": "Seconds to wait", "optional": True},
        "type": {"type": "str", "description": "Data type for random_data (name/email/phone/password/etc)", "optional": True},
    },
)
async def computer_control(
    action: str,
    text: str | None = None,
    x: int | None = None,
    y: int | None = None,
    keys: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    amount: int | None = None,
    title: str | None = None,
    app: str | None = None,
    description: str | None = None,
    seconds: float | None = None,
    type: str | None = None,
) -> str:
    try:
        from src.voice.computer_control import computer_control as _cc

        # Build params dict from non-None kwargs
        params: dict = {}
        if text is not None:     params["text"] = text
        if x is not None:        params["x"] = x
        if y is not None:        params["y"] = y
        if keys is not None:     params["keys"] = keys
        if key is not None:      params["key"] = key
        if direction is not None: params["direction"] = direction
        if amount is not None:   params["amount"] = amount
        if title is not None:    params["title"] = title
        if app is not None:      params["app"] = app
        if description is not None: params["description"] = description
        if seconds is not None:  params["seconds"] = seconds
        if type is not None:     params["type"] = type

        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        return await asyncio.to_thread(_cc, action, params, gemini_key)
    except Exception as e:
        return f"❌ computer_control error: {e}"

"""Tool Bridge — converts AURA registry + Hermes + computer tools → Gemini FunctionDeclarations.

Exposes everything to the Gemini Live voice agent:
  • All 40+ AURA MCP tools (bash, files, email, social, memory, git, browser…)
  • Screen capture + screen_find_and_click
  • Full computer control (mouse, keyboard, window focus)
  • Hermes bridge (ask Hermes, delegate tasks)
  • Claude escalation (for complex code/analysis — min usage)
  • Telegram send (notify Ricardo on phone)
"""
from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Dict, List, Optional

# ── Gemini types ──────────────────────────────────────────────────────────────
try:
    from google.genai import types as gtypes  # type: ignore[import]
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False


# ── Type mapping AURA → Gemini ────────────────────────────────────────────────
_TYPE_MAP = {
    "str": "STRING",
    "string": "STRING",
    "int": "INTEGER",
    "integer": "INTEGER",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "float": "NUMBER",
    "number": "NUMBER",
}


def _aura_params_to_gemini(params: Dict[str, Any]) -> "gtypes.Schema":
    """Convert AURA parameter spec dict → Gemini Schema object."""
    properties = {}
    required = []

    for name, spec in params.items():
        type_str = spec.get("type", "str").lower()
        gemini_type = _TYPE_MAP.get(type_str, "STRING")
        desc = spec.get("description", name)
        optional = spec.get("optional", False) or "optional" in desc.lower()

        if _GEMINI_OK:
            properties[name] = gtypes.Schema(
                type=getattr(gtypes.Type, gemini_type),
                description=desc,
            )
        if not optional:
            required.append(name)

    if _GEMINI_OK:
        return gtypes.Schema(
            type=gtypes.Type.OBJECT,
            properties=properties,
            required=required if required else None,
        )
    return {}


def _make_decl(name: str, description: str, params: Dict[str, Any]) -> Any:
    """Create a Gemini FunctionDeclaration."""
    if not _GEMINI_OK:
        return None
    return gtypes.FunctionDeclaration(
        name=name,
        description=description,
        parameters=_aura_params_to_gemini(params),
    )


# ── Extra tools not in AURA registry ─────────────────────────────────────────

_EXTRA_TOOLS = [
    # Screen capture
    {
        "name": "screen_capture",
        "description": "Capture Ricardo's Mac screen and analyze it visually. Returns description of what's on screen. Use when asked 'what's on my screen', 'what do you see', etc.",
        "parameters": {
            "question": {"type": "str", "description": "What to analyze or look for on screen"},
            "monitor": {"type": "int", "description": "Monitor index (1=primary, optional)", "optional": True},
        },
    },
    {
        "name": "screen_find_and_click",
        "description": "Find a UI element on screen by description and click it. Use for 'click on X', 'press the Save button', etc.",
        "parameters": {
            "description": {"type": "str", "description": "Natural language description of element to click"},
        },
    },
    # Computer control
    {
        "name": "computer_control",
        "description": "Full keyboard/mouse control. Actions: type, smart_type, click, double_click, right_click, move, drag, hotkey, press, scroll, copy, paste, screenshot, wait, clear_field, focus_window, open_app, screen_find, screen_click, random_data.",
        "parameters": {
            "action": {"type": "str", "description": "Action name (type/click/hotkey/scroll/focus_window/open_app/etc)"},
            "text": {"type": "str", "description": "Text to type or paste (for type/smart_type/paste)", "optional": True},
            "x": {"type": "int", "description": "Screen X coordinate", "optional": True},
            "y": {"type": "int", "description": "Screen Y coordinate", "optional": True},
            "keys": {"type": "str", "description": "Hotkey combo e.g. 'command+c' (for hotkey action)", "optional": True},
            "key": {"type": "str", "description": "Single key name e.g. 'enter' (for press action)", "optional": True},
            "direction": {"type": "str", "description": "Scroll direction: up/down/left/right", "optional": True},
            "amount": {"type": "int", "description": "Scroll amount (default 3)", "optional": True},
            "title": {"type": "str", "description": "Window title for focus_window", "optional": True},
            "app": {"type": "str", "description": "App name for open_app", "optional": True},
            "description": {"type": "str", "description": "Element description for screen_find/screen_click", "optional": True},
            "seconds": {"type": "float", "description": "Seconds to wait (for wait action)", "optional": True},
            "type": {"type": "str", "description": "Data type for random_data (name/email/phone/etc)", "optional": True},
        },
    },
    # Hermes bridge
    {
        "name": "hermes_ask",
        "description": "Ask Hermes (AURA's sibling AI agent, @rudserverbot) a question or delegate a task. Hermes has GPT-oss-120b and its own tool set. Use for tasks better suited for Hermes or to coordinate between agents.",
        "parameters": {
            "message": {"type": "str", "description": "Message or task for Hermes"},
        },
    },
    # Claude escalation
    {
        "name": "claude_task",
        "description": "Escalate a complex task to Claude (Haiku/Sonnet). Use ONLY for: complex code generation, deep analysis, multi-step reasoning. NOT for simple questions — Gemini handles those. Costs Claude subscription tokens.",
        "parameters": {
            "task": {"type": "str", "description": "The complex task for Claude to handle"},
            "model": {"type": "str", "description": "claude-haiku (default, faster) or claude-sonnet (deeper)", "optional": True},
        },
    },
    # Telegram notification
    {
        "name": "telegram_send",
        "description": "Send a message to Ricardo's Telegram. Use to notify him of completed tasks, send results, or share files when he's away from his Mac.",
        "parameters": {
            "message": {"type": "str", "description": "Message text to send"},
            "parse_mode": {"type": "str", "description": "Markdown or HTML (optional)", "optional": True},
        },
    },
    # Camera
    {
        "name": "camera_capture",
        "description": "Capture a photo from Mac webcam and analyze it.",
        "parameters": {
            "question": {"type": "str", "description": "What to look for or analyze in the camera image"},
        },
    },
]


def build_gemini_tools() -> List[Any]:
    """Build list of Gemini Tool objects containing all AURA + extra function declarations."""
    if not _GEMINI_OK:
        return []

    decls = []

    # 1. AURA registry tools
    try:
        from src.actions.registry import registry
        for name, spec in registry().items():
            d = _make_decl(name, spec.description, spec.parameters or {})
            if d:
                decls.append(d)
    except Exception as e:
        print(f"[ToolBridge] Warning: could not load AURA registry: {e}")

    # 2. Extra tools (screen, computer, hermes, claude, telegram, camera)
    for t in _EXTRA_TOOLS:
        d = _make_decl(t["name"], t["description"], t["parameters"])
        if d:
            decls.append(d)

    return [gtypes.Tool(function_declarations=decls)]


# ── Tool executor ─────────────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls from Gemini, routing to AURA registry or special handlers."""

    def __init__(
        self,
        gemini_api_key: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self._gemini_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        self._bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = telegram_chat_id or os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
        self._aura_registry: Optional[Dict] = None

    def _get_registry(self) -> Dict:
        if self._aura_registry is None:
            from src.actions.registry import registry
            self._aura_registry = registry()
        return self._aura_registry

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Dispatch a tool call. Always returns a string result."""
        try:
            # Special handlers first
            if tool_name == "screen_capture":
                return await self._screen_capture(args)
            if tool_name == "camera_capture":
                return await self._camera_capture(args)
            if tool_name == "screen_find_and_click":
                return await self._screen_find_and_click(args)
            if tool_name == "computer_control":
                return await self._computer_control(args)
            if tool_name == "hermes_ask":
                return await self._hermes_ask(args)
            if tool_name == "claude_task":
                return await self._claude_task(args)
            if tool_name == "telegram_send":
                return await self._telegram_send(args)

            # AURA registry tools
            reg = self._get_registry()
            if tool_name in reg:
                return await self._call_aura_tool(reg[tool_name], args)

            return f"Unknown tool: {tool_name}"

        except Exception as e:
            return f"Tool '{tool_name}' error: {e}"

    async def _call_aura_tool(self, spec: Any, args: Dict[str, Any]) -> str:
        """Call an AURA tool function, handling both sync and async."""
        fn = spec.fn
        if inspect.iscoroutinefunction(fn):
            result = await fn(**args)
        else:
            result = await asyncio.to_thread(fn, **args)
        return str(result) if result is not None else "Done."

    async def _screen_capture(self, args: Dict) -> str:
        """Capture screen and analyze with Gemini vision."""
        question = args.get("question", "Describe what's on screen")
        monitor = int(args.get("monitor", 1))
        try:
            from src.voice.screen_capture import capture_screen
            img_bytes, mime = await asyncio.to_thread(capture_screen, monitor)
        except Exception as e:
            return f"Screen capture failed: {e}"

        # Analyze with Gemini Flash (cheap vision model)
        try:
            import base64
            from google import genai  # type: ignore[import]
            from google.genai import types as gtypes  # type: ignore[import]

            client = genai.Client(api_key=self._gemini_key)
            b64 = base64.b64encode(img_bytes).decode()
            resp = client.models.generate_content(
                model="gemini-2.5-flash-lite-preview-06-17",
                contents=[
                    gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                    question,
                ],
            )
            return resp.text or "Could not analyze screen"
        except Exception as e:
            return f"Screen analysis failed: {e} (captured {len(img_bytes)} bytes)"

    async def _camera_capture(self, args: Dict) -> str:
        question = args.get("question", "What do you see?")
        try:
            from src.voice.screen_capture import capture_camera
            img_bytes, mime = await asyncio.to_thread(capture_camera, 0)
        except Exception as e:
            return f"Camera capture failed: {e}"

        try:
            from google import genai  # type: ignore[import]
            from google.genai import types as gtypes  # type: ignore[import]
            client = genai.Client(api_key=self._gemini_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash-lite-preview-06-17",
                contents=[
                    gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                    question,
                ],
            )
            return resp.text or "Could not analyze camera image"
        except Exception as e:
            return f"Camera analysis failed: {e}"

    async def _screen_find_and_click(self, args: Dict) -> str:
        from src.voice.computer_control import screen_click
        desc = args.get("description", "")
        return await asyncio.to_thread(screen_click, desc, self._gemini_key)

    async def _computer_control(self, args: Dict) -> str:
        from src.voice.computer_control import computer_control
        action = args.pop("action", "")
        return await asyncio.to_thread(computer_control, action, args, self._gemini_key)

    async def _hermes_ask(self, args: Dict) -> str:
        """Delegate to Hermes via openclaw CLI."""
        message = args.get("message", "")
        if not message:
            return "No message provided"
        try:
            import subprocess
            proc = await asyncio.create_subprocess_exec(
                "/opt/homebrew/bin/openclaw", "agent", "--json", message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            raw = stdout.decode(errors="replace").strip()

            # Parse JSON response from openclaw
            import json
            lines = [l for l in raw.splitlines() if l.strip().startswith("{")]
            for line in reversed(lines):
                try:
                    data = json.loads(line)
                    # Extract text from payloads
                    for key in ("text", "reply", "content", "message"):
                        if isinstance(data.get(key), str) and data[key].strip():
                            return f"[Hermes]: {data[key].strip()}"
                    payloads = data.get("result", {}).get("payloads", [])
                    texts = [p.get("text", "") for p in payloads if isinstance(p, dict) and p.get("text")]
                    if texts:
                        return f"[Hermes]: {' '.join(texts)}"
                except Exception:
                    continue
            return f"[Hermes raw]: {raw[:500]}" if raw else "Hermes no response"
        except asyncio.TimeoutError:
            return "Hermes timeout (60s)"
        except Exception as e:
            return f"Hermes error: {e}"

    async def _claude_task(self, args: Dict) -> str:
        """Escalate to Claude via bash_run aura CLI — minimal usage."""
        task = args.get("task", "")
        model = args.get("model", "haiku")
        if not task:
            return "No task provided"
        try:
            # Use AURA's existing bash_run tool
            reg = self._get_registry()
            if "bash_run" in reg:
                cmd = f'claude -p "{task.replace(chr(34), chr(39))}" --model claude-{model}'
                return await self._call_aura_tool(reg["bash_run"], {"command": cmd, "timeout": 120})
            return "bash_run not available for Claude escalation"
        except Exception as e:
            return f"Claude escalation error: {e}"

    async def _telegram_send(self, args: Dict) -> str:
        """Send message to Ricardo's Telegram via bot API."""
        message = args.get("message", "")
        if not message or not self._bot_token or not self._chat_id:
            return "Telegram not configured or empty message"
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            payload = {
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": args.get("parse_mode", "Markdown"),
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return "Sent to Telegram" if resp.status == 200 else f"Telegram error: {resp.status}"
        except Exception as e:
            return f"Telegram send error: {e}"

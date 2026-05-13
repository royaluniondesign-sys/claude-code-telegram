"""Screen capture tools — exposes Mac screen/camera to AURA MCP and voice agent."""
from __future__ import annotations

import base64
import asyncio
from src.actions.registry import aura_tool


@aura_tool(
    name="screen_capture",
    description="Capture Ricardo's Mac screen and return base64 image. Use to see what's on screen, debug UI, verify visual results.",
    category="system",
    parameters={
        "monitor": {"type": "int", "description": "Monitor index: 1=primary (default), 0=all monitors"},
        "analyze": {"type": "str", "description": "Optional question to analyze the screenshot (uses Gemini vision)", "optional": True},
    },
)
async def screen_capture(monitor: int = 1, analyze: str | None = None) -> str:
    try:
        from src.voice.screen_capture import capture_screen
        img_bytes, mime = await asyncio.to_thread(capture_screen, monitor)
        b64 = base64.b64encode(img_bytes).decode()

        if analyze:
            # Use Gemini Flash Lite for analysis (free, fast)
            import os
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if api_key:
                try:
                    from google import genai  # type: ignore[import]
                    from google.genai import types as gtypes  # type: ignore[import]
                    client = genai.Client(api_key=api_key)
                    resp = client.models.generate_content(
                        model="gemini-2.5-flash-lite-preview-06-17",
                        contents=[
                            gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                            analyze,
                        ],
                    )
                    return resp.text or "Could not analyze"
                except Exception as e:
                    return f"Capture OK but analysis failed: {e}"

        return f"data:{mime};base64,{b64}"
    except Exception as e:
        return f"❌ screen_capture error: {e}"


@aura_tool(
    name="camera_capture",
    description="Capture a photo from Mac webcam. Useful for checking physical environment.",
    category="system",
    parameters={
        "analyze": {"type": "str", "description": "Question to analyze the camera image (optional)", "optional": True},
    },
)
async def camera_capture(analyze: str | None = None) -> str:
    try:
        from src.voice.screen_capture import capture_camera
        img_bytes, mime = await asyncio.to_thread(capture_camera, 0)

        if analyze:
            import os
            api_key = os.environ.get("GEMINI_API_KEY", "")
            if api_key:
                try:
                    from google import genai  # type: ignore[import]
                    from google.genai import types as gtypes  # type: ignore[import]
                    client = genai.Client(api_key=api_key)
                    resp = client.models.generate_content(
                        model="gemini-2.5-flash-lite-preview-06-17",
                        contents=[
                            gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                            analyze,
                        ],
                    )
                    return resp.text or "Could not analyze"
                except Exception as e:
                    return f"Camera OK but analysis failed: {e}"

        b64 = base64.b64encode(img_bytes).decode()
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        return f"❌ camera_capture error: {e}"

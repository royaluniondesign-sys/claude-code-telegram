"""Social publishing tools — Instagram & Facebook via Graph API.

Exposes AURA's social publishing capabilities as MCP tools so Hermes
and other agents can trigger publications without knowing the API details.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.actions.registry import aura_tool

_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"
_SCHEDULED_DIR = Path.home() / ".aura" / "social_scheduled"


@aura_tool(
    name="instagram_publish",
    description=(
        "Publish an image to Instagram for IDNT.ES / RUD Studio. "
        "Provide either an image path (from ~/.aura/social_drafts/) or describe the post "
        "and AURA will generate the image. Caption is required. "
        "Returns the post URL on success."
    ),
    category="social",
    parameters={
        "caption": {"type": "str", "description": "Post caption with hashtags"},
        "image_path": {"type": "str", "description": "Path to image file in social_drafts (optional — if omitted, generates one)"},
        "prompt": {"type": "str", "description": "Image generation prompt (used only if image_path not provided)"},
    },
)
async def instagram_publish(
    caption: str,
    image_path: Optional[str] = None,
    prompt: Optional[str] = None,
) -> str:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

    from src.workflows.instagram_direct import post_image

    # 1. Get image bytes
    png_bytes: Optional[bytes] = None

    if image_path:
        p = Path(image_path)
        if not p.is_absolute():
            p = _DRAFTS_DIR / image_path
        if p.exists():
            png_bytes = p.read_bytes()
        else:
            return f"Error: image not found at {p}"

    if png_bytes is None and prompt:
        # Try to generate via image_brain
        try:
            from src.brains.image_brain import ImageBrain
            brain = ImageBrain()
            result = await brain.generate(prompt, style="photorealistic", format="1:1")
            if result and result.get("images"):
                img_url = result["images"][0]
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            png_bytes = await resp.read()
        except Exception as e:
            return f"Image generation failed: {e}. Provide an image_path instead."

    if png_bytes is None:
        # List available drafts to help the caller
        drafts = sorted(_DRAFTS_DIR.glob("*.jpg")) + sorted(_DRAFTS_DIR.glob("*.png"))
        recent = [d.name for d in drafts[-5:]]
        return (
            "No image provided and no prompt given. "
            f"Recent drafts available: {recent}. "
            "Pass image_path=<filename> or prompt=<description>."
        )

    # 2. Publish
    result = await post_image(png_bytes, caption, save_draft_on_error=True)

    if result.get("ok"):
        return f"✅ Published: {result['url']}"
    else:
        return f"❌ Failed: {result.get('error')} | Draft saved: {result.get('draft', 'no')}"


@aura_tool(
    name="social_list_drafts",
    description=(
        "List available image drafts in ~/.aura/social_drafts/ ready to publish on Instagram. "
        "Returns filenames with dates."
    ),
    category="social",
    parameters={},
)
async def social_list_drafts() -> str:
    if not _DRAFTS_DIR.exists():
        return "No drafts directory found."

    files = sorted(_DRAFTS_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    images = [f for f in files if f.suffix.lower() in (".jpg", ".jpeg", ".png")]

    if not images:
        return "No drafts available."

    lines = []
    for img in images[:15]:
        size_kb = img.stat().st_size // 1024
        lines.append(f"• {img.name} ({size_kb}KB)")

    return "Drafts disponibles:\n" + "\n".join(lines)


@aura_tool(
    name="social_schedule_post",
    description=(
        "Schedule an Instagram post for a future time. "
        "Creates a pending job in ~/.aura/social_scheduled/ that AURA's scheduler will pick up."
    ),
    category="social",
    parameters={
        "caption": {"type": "str", "description": "Post caption with hashtags"},
        "image_path": {"type": "str", "description": "Path or filename from social_drafts"},
        "scheduled_at": {"type": "str", "description": "ISO datetime string e.g. '2026-05-01T18:00:00'"},
        "platform": {"type": "str", "description": "Platform: 'instagram', 'facebook', or 'all' (default: instagram)"},
    },
)
async def social_schedule_post(
    caption: str,
    image_path: str,
    scheduled_at: str,
    platform: str = "instagram",
) -> str:
    import time
    _SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)

    p = Path(image_path)
    if not p.is_absolute():
        p = _DRAFTS_DIR / image_path
    if not p.exists():
        drafts = [f.name for f in sorted(_DRAFTS_DIR.glob("*.jpg"))[-5:]]
        return f"Image not found: {image_path}. Recent drafts: {drafts}"

    job = {
        "caption": caption,
        "image_path": str(p),
        "scheduled_at": scheduled_at,
        "platform": platform,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    fname = f"{platform}_scheduled_{int(time.time())}.json"
    job_path = _SCHEDULED_DIR / fname
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False))

    return f"✅ Scheduled for {scheduled_at} → {fname}"


@aura_tool(
    name="social_generate_and_publish",
    description=(
        "Generate social media content (AI caption + FLUX.1 image) and publish or schedule it. "
        "Fully autonomous: takes a topic, generates brand-voice caption, creates image, "
        "then publishes immediately or schedules. "
        "Use when you want AURA to create and post content with no manual steps."
    ),
    category="social",
    parameters={
        "description": {"type": "str", "description": "Topic or brief for the post"},
        "platform": {"type": "str", "description": "'instagram', 'facebook', or 'social' (both). Default: instagram"},
        "schedule_for": {"type": "str", "description": "ISO8601 UTC datetime e.g. '2026-05-07T18:00:00Z'. Omit to publish now."},
        "custom_caption": {"type": "str", "description": "Override AI-generated caption (optional)"},
    },
)
async def social_generate_and_publish(
    description: str,
    platform: str = "instagram",
    schedule_for: Optional[str] = None,
    custom_caption: Optional[str] = None,
) -> str:
    """Autonomous social post: generate caption + image, then publish or schedule."""
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    platforms = ["instagram", "facebook"] if platform in ("social", "all", "ambas") else [platform]

    if schedule_for:
        # Schedule: write JSON for the scheduler loop to pick up
        _SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(_time.time())
        fname = f"scheduled_{ts}.json"
        job = {
            "description": description,
            "caption": custom_caption or description,
            "platforms": platforms,
            "scheduled_for": schedule_for,
            "status": "pending",
            "created_at": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "auto_generate": True,
        }
        (_SCHEDULED_DIR / fname).write_text(json.dumps(job, indent=2, ensure_ascii=False))
        return f"✅ Programado para {schedule_for} en {', '.join(platforms)} — {fname}"

    # Publish immediately
    try:
        from src.workflows.social_publisher import publish_social
        result = await publish_social(
            description=description,
            platforms=platforms,
            custom_caption=custom_caption or None,
        )
        if result.get("ok"):
            urls = [
                r.get("url", "") for r in result.get("platforms", {}).values() if r.get("ok")
            ]
            return "✅ Publicado: " + " | ".join(urls) if urls else "✅ Publicado"
        errors = [
            f"{p}: {r.get('error', '?')[:80]}"
            for p, r in result.get("platforms", {}).items() if not r.get("ok")
        ]
        draft = result.get("image_url", "")
        return f"⚠️ {'; '.join(errors)}" + (f" | Draft: {draft}" if draft else "")
    except Exception as e:
        return f"❌ Error: {str(e)[:200]}"

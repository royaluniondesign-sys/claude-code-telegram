"""Content Executor — AURA/Hermes worker that executes content plans.

Claude wrote the plan. This module executes it using cheap models + existing pipelines:
  - FLUX.1 for photo posts
  - open-design for editorial posts + carousels
  - Remotion for Reels/TikTok/Shorts
  - Instagram/TikTok/YouTube APIs for publishing

Zero expensive LLM calls here — just orchestration + API calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from .content_memory import mark_published, mark_failed

log = logging.getLogger("content.executor")

API_BASE = "http://localhost:3002"
API_TOKEN = os.environ.get("API_SERVER_SECRET", "i8HjKCDoKqVEyYxlEM7t2X6FbkmvylRzHkyragoVdsE")
HEADERS = {"X-Dashboard-Token": API_TOKEN, "Content-Type": "application/json"}

PLANS_DIR = Path.home() / ".aura" / "content_plans"


# ── Platform router ────────────────────────────────────────────────────────

async def execute_plan(plan: dict) -> dict:
    """Execute a single content plan. Returns result dict."""
    fmt = plan.get("format", "post_4_5")
    platforms = plan.get("platforms", ["instagram"])
    headline = plan.get("headline", "")
    memory_id = plan.get("memory_id", 0)

    log.info("execute_plan fmt=%s platforms=%s headline=%s", fmt, platforms, headline[:40])

    result: dict = {"ok": False, "format": fmt, "platforms": {}}

    try:
        if fmt in ("post_4_5", "post_1_1"):
            image_result = await _generate_photo_post(plan)
            result.update(image_result)

        elif fmt == "carousel":
            carousel_result = await _generate_carousel(plan)
            result.update(carousel_result)

        elif fmt == "reel":
            reel_result = await _generate_reel(plan)
            result.update(reel_result)

        elif fmt == "story":
            story_result = await _generate_story(plan)
            result.update(story_result)

        elif fmt == "text_post":
            # LinkedIn — no image needed
            result["ok"] = True
            result["text_only"] = True
            result["copy"] = plan.get("linkedin_copy") or plan.get("body_copy", "")

        # Publish to platforms
        if result.get("ok"):
            for platform in platforms:
                pub = await _publish_to_platform(platform, result, plan)
                result["platforms"][platform] = pub

            if memory_id:
                mark_published(memory_id)

    except Exception as e:
        log.error("execute_plan_error: %s", e)
        result["error"] = str(e)
        if memory_id:
            mark_failed(memory_id, str(e))

    return result


# ── Content generators ─────────────────────────────────────────────────────

async def _generate_photo_post(plan: dict) -> dict:
    """Generate a 4:5 photo post using FLUX.1 via existing social generate API."""
    visual_brief = plan.get("visual_brief", plan.get("headline", ""))
    caption = plan.get("body_copy", "")
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in plan.get("hashtags", [])[:15])
    full_caption = f"{caption}\n\n{hashtags}".strip()

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{API_BASE}/api/social/generate2",
            headers=HEADERS,
            json={
                "topic": visual_brief,
                "format": "4:5",
                "style": "photorealistic",
                "count": 1,
                "brain": "auto",
                "direct_prompt": visual_brief[:300],
                "direct_caption": full_caption,
            },
        )
        data = r.json()

    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "generate failed")}

    return {
        "ok": True,
        "image_url": data.get("image_url"),
        "carousel_urls": data.get("carousel_urls", []),
        "caption": full_caption,
        "format": "4:5",
    }


async def _generate_carousel(plan: dict) -> dict:
    """Generate a multi-slide carousel using open-design."""
    slides_data = plan.get("slides", {})
    n_slides = slides_data.get("count", 4)
    slide_texts = slides_data.get("texts", [])
    brief = plan.get("visual_brief", plan.get("headline", ""))
    caption = plan.get("body_copy", "")
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in plan.get("hashtags", [])[:15])

    # Build brief for open-design
    slides_desc = ""
    if slide_texts:
        slides_desc = "\n".join(f"Slide {i+1}: {t}" for i, t in enumerate(slide_texts))
    else:
        slides_desc = f"Generate {n_slides} slides on: {brief}"

    full_brief = f"{plan.get('headline', '')}\n\n{slides_desc}\n\nVisual: {brief}"

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            f"{API_BASE}/api/social/design/generate",
            headers=HEADERS,
            json={
                "brief": full_brief[:800],
                "format": "4:5",
                "slides": min(n_slides, 5),
            },
        )
        data = r.json()

    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "design generate failed")}

    full_caption = f"{caption}\n\n{hashtags}".strip()
    return {
        "ok": True,
        "design_task_id": data.get("taskId"),
        "preview_url": data.get("previewUrl"),
        "caption": full_caption,
        "format": "carousel",
        "slides": n_slides,
    }


async def _generate_reel(plan: dict) -> dict:
    """Generate a Reel using Remotion (kinetic text) or FLUX fallback."""
    remotion_script = plan.get("remotion_script")
    headline = plan.get("headline", "")
    caption = plan.get("body_copy", "")
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in plan.get("hashtags", [])[:15])
    full_caption = f"{caption}\n\n{hashtags}".strip()

    # Check if Remotion is available
    remotion_available = await _check_remotion()

    if remotion_available and remotion_script:
        render_result = await _render_remotion(headline, remotion_script, plan)
        if render_result.get("ok"):
            render_result["caption"] = full_caption
            return render_result

    # Fallback: generate a 9:16 image for static "reel cover"
    log.info("remotion_unavailable, falling back to 9:16 image")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{API_BASE}/api/social/generate2",
            headers=HEADERS,
            json={
                "topic": plan.get("visual_brief", headline),
                "format": "9:16",
                "style": "bold",
                "count": 1,
                "brain": "auto",
                "direct_prompt": plan.get("visual_brief", headline)[:300],
                "direct_caption": full_caption,
            },
        )
        data = r.json()

    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "reel generate failed")}

    return {
        "ok": True,
        "image_url": data.get("image_url"),
        "caption": full_caption,
        "format": "9:16",
        "type": "reel_cover",
        "note": "Static reel cover (Remotion not available)",
    }


async def _generate_story(plan: dict) -> dict:
    """Generate a 9:16 story."""
    headline = plan.get("headline", "")
    visual = plan.get("visual_brief", headline)
    caption = plan.get("cta", headline)

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{API_BASE}/api/social/generate2",
            headers=HEADERS,
            json={
                "topic": visual,
                "format": "9:16",
                "style": "minimal",
                "count": 1,
                "direct_prompt": visual[:300],
                "direct_caption": caption,
            },
        )
        data = r.json()

    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "story generate failed")}

    return {
        "ok": True,
        "image_url": data.get("image_url"),
        "caption": caption,
        "format": "9:16",
        "type": "story",
    }


# ── Platform publishing ────────────────────────────────────────────────────

async def _publish_to_platform(platform: str, content: dict, plan: dict) -> dict:
    """Route to platform-specific publisher."""
    if platform == "instagram":
        return await _publish_instagram(content, plan)
    elif platform == "tiktok":
        return {"ok": False, "note": "TikTok API — Phase 2"}
    elif platform == "youtube_shorts":
        return {"ok": False, "note": "YouTube API — Phase 2"}
    elif platform == "linkedin":
        return await _publish_linkedin(content, plan)
    return {"ok": False, "error": f"Unknown platform: {platform}"}


async def _publish_instagram(content: dict, plan: dict) -> dict:
    """Publish to Instagram via existing publish endpoint."""
    image_url = content.get("image_url")
    if not image_url:
        return {"ok": False, "error": "No image URL for Instagram"}

    caption = content.get("caption", "")

    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(
            f"{API_BASE}/api/social/publish",
            headers=HEADERS,
            json={
                "image_url": image_url,
                "caption": caption,
                "platforms": ["instagram"],
            },
        )
        data = r.json()

    return {"ok": data.get("ok", False), "result": data}


async def _publish_linkedin(content: dict, plan: dict) -> dict:
    """LinkedIn text post — Phase 2."""
    return {"ok": False, "note": "LinkedIn — Phase 2"}


# ── Remotion helpers ──────────────────────────────────────────────────────

async def _check_remotion() -> bool:
    """Check if Remotion render server is available."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://localhost:4000/api/health")
            return r.status_code == 200
    except Exception:
        return False


async def _render_remotion(headline: str, script: str, plan: dict) -> dict:
    """Trigger Remotion render for kinetic text Reel."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                "http://localhost:4000/api/render",
                json={
                    "composition": "KineticText",
                    "props": {
                        "headline": headline,
                        "script": script,
                        "style": "rud_editorial",
                        "colors": {"bg": "#0d0d0d", "accent": "#c9a84c", "text": "#f5f0e8"},
                    },
                },
            )
            data = r.json()
            if data.get("ok"):
                return {"ok": True, "video_url": data["url"], "format": "9:16"}
    except Exception as e:
        log.warning("remotion_render_error: %s", e)
    return {"ok": False, "error": "Remotion render failed"}


# ── Batch executor ─────────────────────────────────────────────────────────

async def execute_todays_plans(notify_fn=None) -> list[dict]:
    """Execute all of today's pending plans. Optionally notify via Telegram."""
    from .content_brain import load_today_plans
    plans = load_today_plans()

    if not plans:
        log.info("no_plans_today")
        if notify_fn:
            await notify_fn("📋 Content Agent: sin planes para hoy aún. Corre /content plan para generar.")
        return []

    results = []
    for plan in plans:
        if plan.get("status") == "published":
            continue
        result = await execute_plan(plan)
        results.append(result)

        # Update plan file with result
        _update_plan_status(plan.get("topic_key", ""), result)

        if notify_fn:
            status = "✅" if result.get("ok") else "❌"
            platforms_str = ", ".join(result.get("platforms", {}).keys()) or "—"
            msg = (
                f"{status} **{plan.get('headline', '?')}**\n"
                f"Formato: {plan.get('format')} · {platforms_str}\n"
            )
            if not result.get("ok"):
                msg += f"Error: {result.get('error', '?')}"
            await notify_fn(msg)

    return results


def _update_plan_status(topic_key: str, result: dict) -> None:
    """Mark plan as published/failed in today's plan file."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_file = PLANS_DIR / f"{today}.json"
    if not plan_file.exists():
        return
    try:
        data = json.loads(plan_file.read_text())
        for p in data.get("plans", []):
            if p.get("topic_key") == topic_key:
                p["status"] = "published" if result.get("ok") else "failed"
                p["execution_result"] = {
                    "ok": result.get("ok"),
                    "platforms": result.get("platforms", {}),
                    "error": result.get("error"),
                }
        plan_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("update_plan_status_error: %s", e)

"""Publish router — /api/publish/* endpoints for blog, Instagram, Facebook, scheduling.

Used by Dashboard chat and external triggers.
Auth: handled by app-level middleware (X-Dashboard-Token or cookie).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_SCHEDULED_DIR = Path.home() / ".aura" / "social_scheduled"

logger = structlog.get_logger()
router = APIRouter(prefix="/api/publish", tags=["publish"])


# ─── REQUEST MODELS ───────────────────────────────────────────────────────────

class BlogPublishRequest(BaseModel):
    topic: str = Field(..., description="Topic or title for the blog post")


class SocialPublishRequest(BaseModel):
    description: str = Field(..., description="What the post is about")
    platforms: List[str] = Field(
        default=["instagram"],
        description="Platforms: instagram, facebook",
    )
    caption: Optional[str] = Field(None, description="Override AI-generated caption")
    image_urls: Optional[List[str]] = Field(
        None,
        description="Pre-generated local draft URLs (/api/social/drafts/...). "
                    "2+ URLs → Instagram carousel automatically.",
    )


class ScheduleRequest(BaseModel):
    caption: str = Field(..., description="Post caption / text content")
    platforms: List[str] = Field(default=["instagram"])
    scheduled_for: str = Field(..., description="ISO 8601 datetime (e.g. 2026-04-26T18:00:00)")


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@router.post("/blog")
async def publish_blog(req: BlogPublishRequest) -> dict:
    """Generate and publish a blog post to rud-web.vercel.app.

    Steps: Gemini generates content → GitHub API commit → Vercel auto-deploys (~60s).
    """
    from ...workflows.blog_publisher import publish_blog_from_topic
    logger.info("api_publish_blog", topic=req.topic[:60])
    result = await publish_blog_from_topic(req.topic)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result


@router.post("/social")
async def publish_social_post(req: SocialPublishRequest) -> dict:
    """Generate and publish a social media post (Instagram / Facebook).

    Steps: Gemini caption + FLUX.1 image → upload → Meta Graph API post.
    """
    from ...workflows.social_publisher import publish_social
    logger.info("api_publish_social", platforms=req.platforms, desc=req.description[:60])

    valid_platforms = {"instagram", "facebook"}
    platforms = [p for p in req.platforms if p in valid_platforms]
    if not platforms:
        raise HTTPException(status_code=400, detail=f"Platforms must be one of: {valid_platforms}")

    return await publish_social(
        description=req.description,
        platforms=platforms,
        custom_caption=req.caption,
        image_urls=req.image_urls or None,
    )


@router.get("/status")
async def publish_status() -> dict:
    """Check publishing capabilities: token validity, account connections, etc."""
    import asyncio
    from ...workflows.social_publisher import get_social_status
    try:
        return await asyncio.wait_for(get_social_status(), timeout=10.0)
    except (asyncio.TimeoutError, Exception) as e:
        return {"ok": False, "error": str(e), "instagram": {"valid": False}, "twitter": {"valid": False}}


@router.post("/instagram")
async def publish_instagram(req: SocialPublishRequest) -> dict:
    """Shortcut: publish to Instagram only."""
    from ...workflows.social_publisher import publish_social
    return await publish_social(
        description=req.description,
        platforms=["instagram"],
        custom_caption=req.caption,
    )


@router.post("/facebook")
async def publish_facebook(req: SocialPublishRequest) -> dict:
    """Shortcut: publish to Facebook only."""
    from ...workflows.social_publisher import publish_social
    return await publish_social(
        description=req.description,
        platforms=["facebook"],
        custom_caption=req.caption,
    )


@router.post("/schedule")
async def schedule_social_post(req: ScheduleRequest) -> dict:
    """Save a scheduled post to ~/.aura/social_scheduled/ for deferred publishing.

    The scheduler background task checks this dir and publishes when due.
    """
    try:
        scheduled_dt = datetime.fromisoformat(req.scheduled_for.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid scheduled_for datetime: {e}")

    if scheduled_dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="scheduled_for must be in the future")

    _SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    filename = f"scheduled_{ts}.json"
    payload = {
        "caption": req.caption,
        "platforms": req.platforms,
        "scheduled_for": req.scheduled_for,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    (_SCHEDULED_DIR / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("social_scheduled", file=filename, platforms=req.platforms, scheduled_for=req.scheduled_for)
    return {"ok": True, "file": filename, "scheduled_for": req.scheduled_for}


@router.get("/scheduled")
async def list_scheduled_posts() -> dict:
    """List all pending scheduled posts."""
    if not _SCHEDULED_DIR.exists():
        return {"posts": []}
    posts = []
    for f in sorted(_SCHEDULED_DIR.glob("scheduled_*.json")):
        try:
            data = json.loads(f.read_text())
            data["file"] = f.name
            posts.append(data)
        except Exception:
            pass
    return {"posts": posts}

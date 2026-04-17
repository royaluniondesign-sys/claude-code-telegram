"""Publish router — /api/publish/* endpoints for blog, Instagram, Facebook.

Used by Dashboard chat and external triggers.
Auth: handled by app-level middleware (X-Dashboard-Token or cookie).
"""

from __future__ import annotations

from typing import List, Optional

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

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
    )


@router.get("/status")
async def publish_status() -> dict:
    """Check publishing capabilities: token validity, account connections, etc."""
    from ...workflows.social_publisher import get_social_status
    return await get_social_status()


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

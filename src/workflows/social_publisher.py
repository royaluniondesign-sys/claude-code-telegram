"""Social Publisher — unified interface for Instagram + Facebook posting.

Instagram: Meta Graph API (System User token, instagram_content_publish scope)
Facebook: Meta Graph API (Page access token via same System User)

IMPORTANT — One-time manual setup needed (M3):
  The INSTAGRAM_ACCOUNT_ID in .env may be wrong/disconnected.
  To fix: Meta Business Manager → Business Settings → Instagram Accounts
  → Add account → assign to System User AURA.
  Then get the correct IG Business Account ID and update INSTAGRAM_ACCOUNT_ID.

Architecture:
  1. Generate caption with AI
  2. Generate image with FLUX.1 (pollinations.ai, free, no API key)
  3. Upload image to temp host (0x0.st)
  4. POST to Instagram/Facebook Graph API
  5. Return post URL or save as draft on error

Commands:
  - /post instagram [description]
  - /post facebook [description]
  - /post social [description]  ← posts to both
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

# Sub-modules — split from this file for maintainability
from .social_content import (  # noqa: F401
    _BRAIN_CMDS,
    _DEFAULT_HASHTAG_SETS,
    _GEMINI_FAST_ENV,
    _STYLE_DIRECTIVES,
    _STYLE_MOOD,
    generate_caption_concept,
    generate_social_content,
    refine_caption_with_claude,
)
from .social_image import (  # noqa: F401
    _COMFY_DIMS,
    _COMFYUI_URL,
    _NV_IMG_SIZE,
    _POLLS_BASE,
    _is_comfyui_running,
    _sanitize_flux_prompt,
    generate_image_bfl,
    generate_image_bytes,
    generate_image_comfyui,
    generate_image_nvidia,
    generate_image_public_url,
)
from .social_platforms import (  # noqa: F401
    _GRAPH_BASE,
    _fb_page_id,
    _ig_account_id,
    _ig_token,
    _ig_verify_account,
    post_carousel_to_instagram,
    post_to_facebook,
    post_to_instagram,
    upload_image_to_host,
)

logger = structlog.get_logger()

_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"


@dataclass
class SocialPost:
    description: str
    caption: str
    image_prompt: str
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    platform: str = "instagram"


def _get_tunnel_url() -> str:
    """Get the active Cloudflare tunnel base URL."""
    url_file = Path.home() / ".aura" / "dashboard_url.txt"
    if url_file.exists():
        url = url_file.read_text().strip()
        if url.startswith("https://"):
            return url
    return ""


def _save_draft_meta(caption: str, image_url: str, error: str, platform: str) -> str:
    """Save draft metadata (URL + caption) when publishing fails."""
    import json as _json
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    meta_path = _DRAFTS_DIR / f"{platform}_draft_{ts}.json"
    meta_path.write_text(_json.dumps({
        "caption": caption,
        "image_url": image_url,
        "error": error,
        "platform": platform,
        "saved_at": ts,
        "status": "draft",
    }, ensure_ascii=False, indent=2))
    return str(meta_path)


async def publish_social(
    description: str,
    platforms: list[str] | None = None,
    custom_caption: str | None = None,
    image_urls: list[str] | None = None,
) -> dict:
    """Full pipeline: generate → image(s) → upload → post.

    Args:
        description: What the post is about
        platforms: ["instagram", "facebook"] or ["instagram"] etc.
        custom_caption: Override AI-generated caption
        image_urls: Pre-generated public image URLs (skip generation step).
                    If 2+ URLs → Instagram carousel.

    Returns dict with results per platform.
    """
    if platforms is None:
        platforms = ["instagram"]

    results: dict = {"ok": True, "platforms": {}, "caption": "", "image_url": ""}

    primary = platforms[0]
    logger.info("social_publish_start", description=description[:60], platforms=platforms)

    if custom_caption:
        caption = custom_caption
        image_prompt = f"Professional editorial photo for: {description}"
    else:
        caption, flux_prompts = await generate_social_content(description, primary)
        image_prompt = flux_prompts[0] if flux_prompts else f"Professional photo for: {description}"

    results["caption"] = caption
    results["image_prompt"] = image_prompt

    display_url: str = ""
    if image_urls:
        public_urls: list[str] = []
        for local_url in image_urls:
            try:
                img_bytes = await generate_image_bytes(image_prompt, local_url=local_url)
                pub_url = await upload_image_to_host(img_bytes)
                public_urls.append(pub_url)
            except Exception as e:
                logger.warning("carousel_upload_failed", url=local_url, error=str(e))
        if not public_urls:
            return {"ok": False, "error": "No se pudieron subir las imágenes a CDN público", "platforms": {}}
        display_url = image_urls[0]
        results["image_url"] = display_url
        results["public_urls"] = public_urls
    else:
        polls_url = generate_image_public_url(image_prompt)
        try:
            img_bytes = await generate_image_bytes(image_prompt)
            public_urls = [await upload_image_to_host(img_bytes)]
            logger.info("social_image_uploaded", url=public_urls[0][:80])
        except Exception as e:
            logger.warning("social_image_upload_failed", error=str(e))
            public_urls = [polls_url]
        display_url = polls_url
        results["image_url"] = display_url

    any_ok = False
    for platform in platforms:
        try:
            if platform == "instagram":
                if len(public_urls) >= 2:
                    r = await post_carousel_to_instagram(public_urls, caption)
                else:
                    r = await post_to_instagram(public_urls[0], caption)
            elif platform == "facebook":
                r = await post_to_facebook(public_urls[0], caption)
            else:
                r = {"ok": False, "error": f"Plataforma desconocida: {platform}"}

            results["platforms"][platform] = r
            if r.get("ok"):
                any_ok = True
            elif not r.get("ok") and r.get("action_required"):
                _save_draft_meta(caption, display_url, r["error"], platform)
                r["draft_image_url"] = display_url

        except Exception as e:
            results["platforms"][platform] = {"ok": False, "error": str(e)}

    results["ok"] = any_ok
    if not any_ok:
        _save_draft_meta(caption, display_url, "all platforms failed", "all")

    logger.info("social_publish_done", results=str(results)[:200])
    return results


async def get_social_status() -> dict:
    """Check status of social publishing capabilities."""
    ig_valid, ig_info = await _ig_verify_account()
    fb_has_id = bool(_fb_page_id())
    gh_has_token = bool(os.environ.get("GITHUB_TOKEN"))
    tw_token = bool(os.environ.get("TWITTER_BEARER_TOKEN"))
    li_token = bool(os.environ.get("LINKEDIN_ACCESS_TOKEN"))
    tt_token = bool(os.environ.get("TIKTOK_ACCESS_TOKEN"))

    return {
        "instagram": {
            "token_valid": bool(_ig_token()),
            "account_connected": ig_valid,
            "account_info": ig_info,
            "ready": ig_valid,
            "action_if_not_ready": "M3: conectar cuenta Instagram en Meta Business Manager",
        },
        "facebook": {
            "token_valid": bool(_ig_token()),
            "page_id_set": fb_has_id,
            "ready": fb_has_id,
            "action_if_not_ready": "Añadir FACEBOOK_PAGE_ID en .env (ID de la página de Facebook de RUD)",
        },
        "twitter": {
            "token_valid": tw_token,
            "ready": tw_token,
            "action_if_not_ready": "Añadir TWITTER_BEARER_TOKEN en .env (Twitter API v2)",
            "setup_url": "https://developer.twitter.com/en/portal/apps",
        },
        "linkedin": {
            "token_valid": li_token,
            "ready": li_token,
            "action_if_not_ready": "Añadir LINKEDIN_ACCESS_TOKEN en .env",
            "setup_url": "https://www.linkedin.com/developers/apps",
        },
        "tiktok": {
            "token_valid": tt_token,
            "ready": tt_token,
            "action_if_not_ready": "Añadir TIKTOK_ACCESS_TOKEN en .env",
            "setup_url": "https://developers.tiktok.com/",
        },
        "blog": {
            "github_token": gh_has_token,
            "repo": os.environ.get("GITHUB_REPO_BLOG", "royaluniondesign-sys/RUD-WEB"),
            "ready": gh_has_token,
        },
        "image_gen": {
            "provider": "pollinations.ai FLUX.1",
            "ready": True,
            "cost": "FREE",
        },
    }

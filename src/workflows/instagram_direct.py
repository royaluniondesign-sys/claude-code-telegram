"""Instagram Graph API — direct posting without N8N.

Flow:
  1. Upload PNG bytes to a temp public host (0x0.st) → public URL
  2. POST /media  → create container (creation_id)
  3. POST /media_publish → publish post
  Returns post URL on success.

No login required — uses the long-lived System User token from .env.
Token: INSTAGRAM_ACCESS_TOKEN
Account: INSTAGRAM_ACCOUNT_ID
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiohttp
import structlog

logger = structlog.get_logger()

_GRAPH_BASE = "https://graph.facebook.com/v22.0"
_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID", "17841402166866335")
_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"


def _token() -> str:
    """Fetch token from env at call time (supports reload)."""
    return os.environ.get(
        "INSTAGRAM_ACCESS_TOKEN",
        "EAAVzNUmRsBEBRG9ZC7TChZBfiyR4VhRxTKNTtFFS9ntgbPOjj2LZA2uMT7ECXtaRpUfCwqkm1f6RZAb9qSoNKr2kRVyEzim5mMb80ztvQi9ZAYradiuMU44pJBeaxVXaZB2URKZB3lIEqA9zpb4HH7PZB7FfrDQbdi7u0k7rVBwXIuGffkDQOPCndIw4jWun4XXLGgZDZD",
    )


async def upload_image_public(png_bytes: bytes, filename: str = "post.png") -> str:
    """Upload PNG bytes to 0x0.st → returns public HTTPS URL.

    0x0.st: anonymous file host, no auth, files expire after ~3 months,
    max 512MB. Good enough for Instagram Graph API image ingestion.
    """
    data = aiohttp.FormData()
    data.add_field("file", png_bytes, filename=filename, content_type="image/png")

    async with aiohttp.ClientSession() as session:
        # Try 0x0.st first
        try:
            async with session.post(
                "https://0x0.st",
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("http"):
                        logger.info("image_uploaded_0x0", url=url[:80])
                        return url
        except Exception as e:
            logger.warning("0x0_failed", error=str(e))

        # Fallback: transfer.sh
        data2 = aiohttp.FormData()
        data2.add_field("file", png_bytes, filename=filename, content_type="image/png")
        try:
            async with session.put(
                f"https://transfer.sh/{filename}",
                data=png_bytes,
                headers={"Max-Days": "3"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("http"):
                        logger.info("image_uploaded_transfer_sh", url=url[:80])
                        return url
        except Exception as e:
            logger.warning("transfer_sh_failed", error=str(e))

    raise RuntimeError("No se pudo subir la imagen a un host público (0x0.st, transfer.sh)")


async def create_media_container(image_url: str, caption: str) -> str:
    """POST /media → returns creation_id."""
    url = f"{_GRAPH_BASE}/{_ACCOUNT_ID}/media"
    params = {
        "image_url": image_url,
        "caption": caption,
        "access_token": _token(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, params=params, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(f"Graph API media error: {data['error'].get('message', data['error'])}")
            creation_id = data.get("id", "")
            if not creation_id:
                raise RuntimeError(f"No creation_id in response: {data}")
            logger.info("ig_container_created", creation_id=creation_id)
            return creation_id


async def publish_container(creation_id: str) -> str:
    """POST /media_publish → returns post ID."""
    url = f"{_GRAPH_BASE}/{_ACCOUNT_ID}/media_publish"
    params = {
        "creation_id": creation_id,
        "access_token": _token(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, params=params, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(f"Graph API publish error: {data['error'].get('message', data['error'])}")
            post_id = data.get("id", "")
            if not post_id:
                raise RuntimeError(f"No post_id in response: {data}")
            logger.info("ig_post_published", post_id=post_id)
            return post_id


async def post_image(
    png_bytes: bytes,
    caption: str,
    save_draft_on_error: bool = True,
) -> dict:
    """Full pipeline: upload → container → publish.

    Args:
        png_bytes: PNG image bytes (brand image from image_gen.py)
        caption: Instagram caption with hashtags
        save_draft_on_error: Save locally if posting fails

    Returns:
        {"ok": True, "url": str, "post_id": str}
        or {"ok": False, "error": str, "draft": str}
    """
    try:
        # 1. Upload image to public host
        image_url = await upload_image_public(png_bytes, filename="aura_post.png")

        # 2. Create media container
        creation_id = await create_media_container(image_url, caption)

        # 3. Publish
        post_id = await publish_container(creation_id)
        post_url = f"https://www.instagram.com/p/{post_id}/"

        return {"ok": True, "url": post_url, "post_id": post_id, "image_url": image_url}

    except Exception as e:
        logger.error("ig_post_failed", error=str(e))

        if save_draft_on_error:
            draft_path = _save_draft_bytes(png_bytes, caption, str(e))
            return {
                "ok": False,
                "error": str(e),
                "draft": str(draft_path) if draft_path else "",
            }
        return {"ok": False, "error": str(e)}


def _save_draft_bytes(png_bytes: bytes, caption: str, error: str) -> Optional[Path]:
    """Save image + caption locally as draft fallback."""
    import json
    import time

    try:
        _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        img_path = _DRAFTS_DIR / f"ig_draft_{ts}.png"
        meta_path = _DRAFTS_DIR / f"ig_draft_{ts}.json"

        img_path.write_bytes(png_bytes)
        meta_path.write_text(json.dumps({
            "caption": caption,
            "error": error,
            "saved_at": ts,
            "status": "draft",
        }, ensure_ascii=False, indent=2))

        logger.info("ig_draft_saved", path=str(img_path))
        return img_path
    except Exception as exc:
        logger.error("ig_draft_save_error", error=str(exc))
        return None


async def get_account_info() -> dict:
    """Fetch basic account info to verify token works."""
    url = f"{_GRAPH_BASE}/{_ACCOUNT_ID}"
    params = {
        "fields": "id,username,followers_count,media_count",
        "access_token": _token(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if "error" in data:
                return {"error": data["error"].get("message", str(data["error"]))}
            return data

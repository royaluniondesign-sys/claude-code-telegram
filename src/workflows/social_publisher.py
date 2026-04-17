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

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import aiohttp
import structlog

logger = structlog.get_logger()

_GRAPH_BASE = "https://graph.facebook.com/v22.0"
_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"
_POLLS_BASE = "https://image.pollinations.ai/prompt"


def _ig_token() -> str:
    return os.environ.get(
        "INSTAGRAM_ACCESS_TOKEN",
        "EAAVzNUmRsBEBRG9ZC7TChZBfiyR4VhRxTKNTtFFS9ntgbPOjj2LZA2uMT7ECXtaRpUfCwqkm1f6RZAb9qSoNKr2kRVyEzim5mMb80ztvQi9ZAYradiuMU44pJBeaxVXaZB2URKZB3lIEqA9zpb4HH7PZB7FfrDQbdi7u0k7rVBwXIuGffkDQOPCndIw4jWun4XXLGgZDZD",
    )


def _ig_account_id() -> str:
    return os.environ.get("INSTAGRAM_ACCOUNT_ID", "")


def _fb_page_id() -> str:
    return os.environ.get("FACEBOOK_PAGE_ID", "")


@dataclass
class SocialPost:
    description: str
    caption: str
    image_prompt: str
    image_url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    platform: str = "instagram"


# ─── AI CONTENT GENERATION ────────────────────────────────────────────────────

async def generate_social_content(
    description: str,
    platform: str = "instagram",
) -> tuple[str, str]:
    """Generate caption + image prompt using Gemini.

    Returns: (caption, image_prompt)
    """
    if platform == "facebook":
        tone_note = "más texto, más informativo, puede tener link al blog"
        char_limit = "400-600 caracteres"
    else:
        tone_note = "conciso, visual, con emojis naturales y hashtags al final"
        char_limit = "150-220 caracteres + hashtags"

    prompt = f"""Crea contenido para {platform} para RUD Studio (agencia branding+web+IA en Barcelona).

Descripción del post: {description}
Tono: {tone_note}
Longitud caption: {char_limit}

Responde SOLO en JSON sin markdown:
{{
  "caption": "caption listo para publicar",
  "image_prompt": "prompt en inglés para generación de imagen IA (estilo: professional, high quality, agency aesthetic, Barcelona, modern design studio)"
}}"""

    try:
        proc = await asyncio.create_subprocess_exec(
            "gemini", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        raw = stdout.decode().strip()

        # Parse JSON
        json_match = re.search(r'\{.*"caption".*"image_prompt".*\}', raw, re.DOTALL)
        if json_match:
            import json
            data = json.loads(json_match.group(0))
            return data["caption"], data["image_prompt"]
    except Exception as e:
        logger.warning("social_ai_gen_failed", error=str(e))

    # Fallback
    caption = f"✨ {description}\n\n#RUDStudio #Branding #DiseñoWeb #Barcelona #IA"
    image_prompt = f"Professional agency photo related to: {description}, modern design studio, Barcelona aesthetic"
    return caption, image_prompt


# ─── IMAGE GENERATION ─────────────────────────────────────────────────────────

async def generate_image_bytes(image_prompt: str) -> bytes:
    """Generate image via pollinations.ai FLUX.1 (free, no API key)."""
    import urllib.parse
    encoded_prompt = urllib.parse.quote(image_prompt)
    # Add quality params for better output
    url = (
        f"{_POLLS_BASE}/{encoded_prompt}"
        f"?width=1080&height=1080&model=flux&seed={int(time.time())}&nologo=true"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                data = await resp.read()
                logger.info("image_generated", size=len(data), url=url[:80])
                return data
            raise RuntimeError(f"pollinations.ai returned {resp.status}")


# ─── IMAGE UPLOAD ──────────────────────────────────────────────────────────────

async def upload_image_to_host(png_bytes: bytes) -> str:
    """Upload image to public host. Tries 0x0.st → imgbb → transfer.sh."""
    async with aiohttp.ClientSession() as session:
        # 1. Try 0x0.st
        try:
            form = aiohttp.FormData()
            form.add_field("file", png_bytes, filename="post.jpg", content_type="image/jpeg")
            async with session.post(
                "https://0x0.st", data=form, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("http"):
                        return url
        except Exception as e:
            logger.debug("0x0_failed", error=str(e))

        # 2. Try transfer.sh
        try:
            async with session.put(
                "https://transfer.sh/post.jpg",
                data=png_bytes,
                headers={"Max-Days": "3"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("http"):
                        return url
        except Exception as e:
            logger.debug("transfer_sh_failed", error=str(e))

    raise RuntimeError("No se pudo subir la imagen (0x0.st y transfer.sh fallaron)")


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

async def _ig_verify_account() -> tuple[bool, str]:
    """Check if Instagram account ID is valid and accessible."""
    account_id = _ig_account_id()
    if not account_id:
        return False, "INSTAGRAM_ACCOUNT_ID no está configurado en .env"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_GRAPH_BASE}/{account_id}",
            params={"fields": "id,username,name", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                return False, f"Account error: {err_msg}"
            return True, data.get("username", data.get("id", "ok"))


async def post_to_instagram(image_url: str, caption: str) -> dict:
    """Post image to Instagram via Graph API."""
    account_id = _ig_account_id()
    if not account_id:
        return {
            "ok": False,
            "error": "INSTAGRAM_ACCOUNT_ID no configurado. Acción manual M3: conectar cuenta en Meta Business Manager.",
            "action_required": "M3",
        }

    try:
        async with aiohttp.ClientSession() as session:
            # Create media container
            async with session.post(
                f"{_GRAPH_BASE}/{account_id}/media",
                params={
                    "image_url": image_url,
                    "caption": caption,
                    "access_token": _ig_token(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    err = data["error"]
                    if err.get("code") == 100 and "does not exist" in err.get("message", ""):
                        return {
                            "ok": False,
                            "error": "Instagram account ID inválido. Necesitas conectar la cuenta en Meta Business Manager (Acción M3).",
                            "action_required": "M3",
                        }
                    raise RuntimeError(f"Media container error: {err.get('message', data)}")
                creation_id = data.get("id")
                if not creation_id:
                    raise RuntimeError(f"No creation_id: {data}")

            # Publish
            async with session.post(
                f"{_GRAPH_BASE}/{account_id}/media_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": _ig_token(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Publish error: {data['error'].get('message', data)}")
                post_id = data.get("id", "")
                return {
                    "ok": True,
                    "platform": "instagram",
                    "post_id": post_id,
                    "url": f"https://www.instagram.com/p/{post_id}/",
                    "image_url": image_url,
                }

    except RuntimeError:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "instagram"}


# ─── FACEBOOK ─────────────────────────────────────────────────────────────────

async def post_to_facebook(image_url: str, caption: str) -> dict:
    """Post image to Facebook Page via Graph API."""
    page_id = _fb_page_id()
    if not page_id:
        return {
            "ok": False,
            "error": "FACEBOOK_PAGE_ID no configurado en .env. Necesitas añadir el Page ID de la página de Facebook de RUD.",
            "action_required": "Set FACEBOOK_PAGE_ID in .env",
        }

    try:
        async with aiohttp.ClientSession() as session:
            # Post photo to page feed
            async with session.post(
                f"{_GRAPH_BASE}/{page_id}/photos",
                params={
                    "url": image_url,
                    "caption": caption,
                    "access_token": _ig_token(),  # System User token works for pages too
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Facebook error: {data['error'].get('message', data)}")
                post_id = data.get("post_id", data.get("id", ""))
                return {
                    "ok": True,
                    "platform": "facebook",
                    "post_id": post_id,
                    "url": f"https://www.facebook.com/{page_id}/posts/{post_id}",
                    "image_url": image_url,
                }
    except RuntimeError:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "facebook"}


# ─── UNIFIED PUBLISH ──────────────────────────────────────────────────────────

def _save_draft(image_bytes: bytes, caption: str, image_url: str, error: str, platform: str) -> str:
    """Save post as draft when publishing fails."""
    import json as _json
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    img_path = _DRAFTS_DIR / f"{platform}_draft_{ts}.jpg"
    meta_path = _DRAFTS_DIR / f"{platform}_draft_{ts}.json"
    img_path.write_bytes(image_bytes)
    meta_path.write_text(_json.dumps({
        "caption": caption,
        "image_url": image_url,
        "error": error,
        "platform": platform,
        "saved_at": ts,
        "status": "draft",
    }, ensure_ascii=False, indent=2))
    return str(img_path)


async def publish_social(
    description: str,
    platforms: list[str] | None = None,
    custom_caption: str | None = None,
) -> dict:
    """Full pipeline: generate → image → upload → post.

    Args:
        description: What the post is about
        platforms: ["instagram", "facebook"] or ["instagram"] etc.
        custom_caption: Override AI-generated caption

    Returns dict with results per platform.
    """
    if platforms is None:
        platforms = ["instagram"]

    results: dict = {"ok": True, "platforms": {}, "caption": "", "image_url": ""}

    # 1. Generate content (use first platform for style)
    primary = platforms[0]
    logger.info("social_publish_start", description=description[:60], platforms=platforms)

    if custom_caption:
        caption = custom_caption
        image_prompt = f"Professional photo for: {description}"
    else:
        caption, image_prompt = await generate_social_content(description, primary)

    results["caption"] = caption
    results["image_prompt"] = image_prompt

    # 2. Generate image
    try:
        image_bytes = await generate_image_bytes(image_prompt)
    except Exception as e:
        logger.error("social_image_gen_failed", error=str(e))
        results["ok"] = False
        results["error"] = f"Error generando imagen: {e}"
        return results

    # 3. Upload image
    try:
        image_url = await upload_image_to_host(image_bytes)
        results["image_url"] = image_url
    except Exception as e:
        logger.error("social_upload_failed", error=str(e))
        draft = _save_draft(image_bytes, caption, "", str(e), primary)
        results["ok"] = False
        results["error"] = f"Error subiendo imagen: {e}"
        results["draft"] = draft
        return results

    # 4. Post to each platform
    any_ok = False
    for platform in platforms:
        try:
            if platform == "instagram":
                r = await post_to_instagram(image_url, caption)
            elif platform == "facebook":
                r = await post_to_facebook(image_url, caption)
            else:
                r = {"ok": False, "error": f"Plataforma desconocida: {platform}"}

            results["platforms"][platform] = r
            if r.get("ok"):
                any_ok = True
            elif not r.get("ok") and r.get("action_required"):
                # Save draft for manual posting
                draft = _save_draft(image_bytes, caption, image_url, r["error"], platform)
                r["draft_saved"] = draft

        except Exception as e:
            results["platforms"][platform] = {"ok": False, "error": str(e)}

    results["ok"] = any_ok
    if not any_ok:
        # Save unified draft
        draft = _save_draft(image_bytes, caption, image_url, "all platforms failed", "all")
        results["draft_saved"] = str(draft)

    logger.info("social_publish_done", results=str(results)[:200])
    return results


async def get_social_status() -> dict:
    """Check status of social publishing capabilities."""
    ig_valid, ig_info = await _ig_verify_account()
    fb_has_id = bool(_fb_page_id())
    gh_has_token = bool(os.environ.get("GITHUB_TOKEN"))

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
            "action_if_not_ready": "Añadir FACEBOOK_PAGE_ID en .env (ID de la página de RUD en Facebook)",
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

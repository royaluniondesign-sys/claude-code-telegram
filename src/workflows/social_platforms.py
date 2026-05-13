"""Meta Graph API operations — Instagram, Facebook posting, and CDN image upload."""
from __future__ import annotations

import os
import time

import aiohttp
import structlog

logger = structlog.get_logger()

_GRAPH_BASE = "https://graph.facebook.com/v22.0"


def _ig_token() -> str:
    return os.environ.get(
        "INSTAGRAM_ACCESS_TOKEN",
        "EAAVzNUmRsBEBRG9ZC7TChZBfiyR4VhRxTKNTtFFS9ntgbPOjj2LZA2uMT7ECXtaRpUfCwqkm1f6RZAb9qSoNKr2kRVyEzim5mMb80ztvQi9ZAYradiuMU44pJBeaxVXaZB2URKZB3lIEqA9zpb4HH7PZB7FfrDQbdi7u0k7rVBwXIuGffkDQOPCndIw4jWun4XXLGgZDZD",
    )


def _ig_account_id() -> str:
    return os.environ.get("INSTAGRAM_ACCOUNT_ID", "")


def _fb_page_id() -> str:
    return os.environ.get("FACEBOOK_PAGE_ID", "")


async def upload_image_to_host(png_bytes: bytes) -> str:
    """Upload image to a public CDN that Meta Graph API can fetch.

    Primary: GitHub raw content (fast, reliable, no expiry issues).
    Fallback: litterbox.catbox.moe (24h).
    """
    import base64 as _b64

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_SOCIAL_CDN", "royaluniondesign-sys/social-cdn")
    if gh_token and gh_repo:
        try:
            ts = int(time.time())
            filename = f"social/img_{ts}.jpg"
            b64_content = _b64.b64encode(png_bytes).decode()
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"https://api.github.com/repos/{gh_repo}/contents/{filename}",
                    headers={"Authorization": f"Bearer {gh_token}", "Content-Type": "application/json"},
                    json={"message": "social draft", "content": b64_content},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (200, 201):
                        raw_url = f"https://raw.githubusercontent.com/{gh_repo}/main/{filename}"
                        logger.info("image_uploaded_github", url=raw_url[:80])
                        return raw_url
                    body = await resp.text()
                    logger.debug("github_upload_failed", status=resp.status, body=body[:100])
        except Exception as e:
            logger.debug("github_upload_exception", error=str(e))

    async with aiohttp.ClientSession() as session:
        try:
            form = aiohttp.FormData()
            form.add_field("reqtype", "fileupload")
            form.add_field("time", "24h")
            form.add_field("fileToUpload", png_bytes, filename="post.jpg", content_type="image/jpeg")
            async with session.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data=form,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status == 200:
                    url = (await resp.text()).strip()
                    if url.startswith("https://"):
                        logger.info("image_uploaded_litterbox", url=url[:60])
                        return url
        except Exception as e:
            logger.debug("litterbox_failed", error=str(e))

    raise RuntimeError("No se pudo subir la imagen (GitHub y litterbox fallaron)")


async def _ig_verify_account() -> tuple[bool, str]:
    """Check if Instagram account ID is valid and accessible."""
    account_id = _ig_account_id()
    if not account_id:
        return False, "INSTAGRAM_ACCOUNT_ID no está configurado en .env"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_GRAPH_BASE}/{account_id}",
            params={"fields": "id,username,name", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                return False, f"Account error: {err_msg}"
            return True, data.get("username", data.get("id", "ok"))


async def _ig_create_single_container(
    session: "aiohttp.ClientSession",
    account_id: str,
    image_url: str,
    caption: str | None = None,
    is_carousel_item: bool = False,
) -> str:
    """Create one Instagram media container. Returns creation_id."""
    params: dict = {
        "image_url": image_url,
        "access_token": _ig_token(),
    }
    if caption:
        params["caption"] = caption
    if is_carousel_item:
        params["is_carousel_item"] = "true"

    async with session.post(
        f"{_GRAPH_BASE}/{account_id}/media",
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if "error" in data:
            err = data["error"]
            if err.get("code") == 100 and "does not exist" in err.get("message", ""):
                raise RuntimeError("M3:account_invalid")
            raise RuntimeError(f"Media container: {err.get('message', data)}")
        creation_id = data.get("id")
        if not creation_id:
            raise RuntimeError(f"No creation_id: {data}")
        return creation_id


async def _ig_wait_ready(
    session: "aiohttp.ClientSession",
    creation_id: str,
    max_wait: int = 30,
) -> None:
    """Poll container status until FINISHED. Raises if ERROR or timeout."""
    import asyncio
    for attempt in range(max_wait // 3):
        await asyncio.sleep(3)
        async with session.get(
            f"{_GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            status = data.get("status_code", "")
            logger.debug("ig_container_status", creation_id=creation_id, status=status)
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError(f"Meta container processing failed: {data}")
    raise RuntimeError(f"Meta container not ready after {max_wait}s")


async def _ig_publish(
    session: "aiohttp.ClientSession",
    account_id: str,
    creation_id: str,
) -> str:
    """Wait for container to be ready, then publish. Returns post_id."""
    await _ig_wait_ready(session, creation_id)
    async with session.post(
        f"{_GRAPH_BASE}/{account_id}/media_publish",
        params={"creation_id": creation_id, "access_token": _ig_token()},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"Publish: {data['error'].get('message', data)}")
        return data.get("id", "")


async def post_to_instagram(image_url: str, caption: str) -> dict:
    """Post single image to Instagram via Graph API."""
    account_id = _ig_account_id()
    if not account_id:
        return {
            "ok": False,
            "error": "INSTAGRAM_ACCOUNT_ID no configurado. Acción manual M3: conectar cuenta en Meta Business Manager.",
            "action_required": "M3",
        }

    try:
        async with aiohttp.ClientSession() as session:
            creation_id = await _ig_create_single_container(session, account_id, image_url, caption)
            post_id = await _ig_publish(session, account_id, creation_id)
            return {
                "ok": True,
                "platform": "instagram",
                "post_id": post_id,
                "url": f"https://www.instagram.com/p/{post_id}/",
                "image_url": image_url,
            }
    except RuntimeError as e:
        if "M3:account_invalid" in str(e):
            return {
                "ok": False,
                "error": "Instagram account ID inválido (Acción M3).",
                "action_required": "M3",
            }
        return {"ok": False, "error": str(e), "platform": "instagram"}
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "instagram"}


async def post_carousel_to_instagram(image_urls: list[str], caption: str) -> dict:
    """Post carousel (2-10 images) to Instagram via Graph API."""
    import asyncio

    account_id = _ig_account_id()
    if not account_id:
        return {
            "ok": False,
            "error": "INSTAGRAM_ACCOUNT_ID no configurado (M3).",
            "action_required": "M3",
        }
    if len(image_urls) < 2:
        return await post_to_instagram(image_urls[0], caption)
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    try:
        async with aiohttp.ClientSession() as session:
            item_ids: list[str] = []
            for url in image_urls:
                item_id = await _ig_create_single_container(
                    session, account_id, url, is_carousel_item=True
                )
                item_ids.append(item_id)
                await asyncio.sleep(0.5)

            async with session.post(
                f"{_GRAPH_BASE}/{account_id}/media",
                params={
                    "media_type": "CAROUSEL",
                    "children": ",".join(item_ids),
                    "caption": caption,
                    "access_token": _ig_token(),
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"Carousel container: {data['error'].get('message', data)}")
                carousel_id = data.get("id")
                if not carousel_id:
                    raise RuntimeError(f"No carousel_id: {data}")

            post_id = await _ig_publish(session, account_id, carousel_id)
            return {
                "ok": True,
                "platform": "instagram",
                "type": "carousel",
                "post_id": post_id,
                "url": f"https://www.instagram.com/p/{post_id}/",
                "images_count": len(image_urls),
            }

    except RuntimeError as e:
        if "M3:account_invalid" in str(e):
            return {"ok": False, "error": "Instagram account ID inválido (M3).", "action_required": "M3"}
        return {"ok": False, "error": str(e), "platform": "instagram"}
    except Exception as e:
        return {"ok": False, "error": str(e), "platform": "instagram"}


async def _get_page_access_token(page_id: str) -> str:
    """Exchange System User token for a Page Access Token (required for posting)."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_GRAPH_BASE}/{page_id}",
            params={"fields": "access_token", "access_token": _ig_token()},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if "access_token" not in data:
                raise RuntimeError(f"Could not get Page token: {data}")
            return data["access_token"]


async def post_to_facebook(image_url: str, caption: str) -> dict:
    """Post image to Facebook Page via Graph API using Page Access Token."""
    page_id = _fb_page_id()
    if not page_id:
        return {
            "ok": False,
            "error": "FACEBOOK_PAGE_ID no configurado en .env.",
            "action_required": "Set FACEBOOK_PAGE_ID in .env",
        }

    try:
        page_token = await _get_page_access_token(page_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_GRAPH_BASE}/{page_id}/photos",
                params={
                    "url": image_url,
                    "caption": caption,
                    "access_token": page_token,
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

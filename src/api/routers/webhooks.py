"""Webhooks router: /webhooks/*, /auth/instagram/*, /api/instagram/*,
/api/social/*."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = structlog.get_logger()

router = APIRouter()

# Instagram OAuth state (module-level, shared across requests)
_ig_oauth_state: Dict[str, str] = {}


def _update_env_instagram_token(token: str, user_id: str) -> None:
    """Update INSTAGRAM_ACCESS_TOKEN in .env file and runtime env."""
    env_file = (Path(__file__).parent.parent.parent.parent / ".env").resolve()
    if not env_file.exists():
        return
    lines = env_file.read_text().splitlines()
    new_lines, token_written, uid_written = [], False, False
    for line in lines:
        if line.startswith("INSTAGRAM_ACCESS_TOKEN="):
            new_lines.append(f"INSTAGRAM_ACCESS_TOKEN={token}")
            token_written = True
        elif line.startswith("INSTAGRAM_ACCOUNT_ID=") and user_id:
            new_lines.append(f"INSTAGRAM_ACCOUNT_ID={user_id}")
            uid_written = True
        else:
            new_lines.append(line)
    if not token_written:
        new_lines.append(f"INSTAGRAM_ACCESS_TOKEN={token}")
    if not uid_written and user_id:
        new_lines.append(f"INSTAGRAM_ACCOUNT_ID={user_id}")
    env_file.write_text("\n".join(new_lines) + "\n")
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = token
    if user_id:
        os.environ["INSTAGRAM_ACCOUNT_ID"] = user_id


async def _notify_ig_auth_success(user_id: str, expires_in: int) -> None:
    """Send Telegram message when Instagram OAuth completes."""
    import aiohttp as _aio
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (os.environ.get("NOTIFICATION_CHAT_IDS", "") or "").split(",")[0].strip()
    if not bot_token or not chat_id:
        return
    days = expires_in // 86400
    msg = (
        f"✅ <b>Instagram OAuth completado!</b>\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Token válido por <b>{days} días</b> — auto-refresh activado."
    )
    async with _aio.ClientSession() as sess:
        await sess.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
        )


def _make_receive_webhook(event_bus: Any, settings: Any, db_manager: Any):
    """Factory: build the webhook endpoint bound to shared state."""

    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        from ...api.auth import verify_github_signature, verify_shared_secret
        from ...events.types import WebhookEvent

        body = await request.body()

        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(status_code=500, detail="GitHub webhook secret not configured")
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning("GitHub webhook signature verification failed", delivery_id=x_github_delivery)
                raise HTTPException(status_code=401, detail="Invalid signature")
            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())
        else:
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="Webhook API secret not configured. Set WEBHOOK_API_SECRET.",
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        if db_manager and delivery_id:
            from ...storage.database import DatabaseManager
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info("Duplicate webhook delivery ignored", provider=provider, delivery_id=delivery_id)
                return {"status": "duplicate", "delivery_id": delivery_id}

        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )
        await event_bus.publish(event)
        logger.info("Webhook received and published", provider=provider, event_type=event_type_name)
        return {"status": "accepted", "event_id": event.id}

    return receive_webhook


async def _try_record_webhook(
    db_manager: Any,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload, processed)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (event_id, provider, event_type, delivery_id, json.dumps(payload)),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


def make_webhooks_router(event_bus: Any, settings: Any, db_manager: Any) -> APIRouter:
    """Build and return the webhooks router with injected dependencies."""
    r = APIRouter()

    r.add_api_route(
        "/webhooks/{provider}",
        _make_receive_webhook(event_bus, settings, db_manager),
        methods=["POST"],
    )

    @r.get("/auth/instagram")
    async def instagram_auth_redirect(request: Request) -> Any:
        """Redirect to Instagram OAuth. Called by /ig-auth Telegram command.
        Query params: app_id, app_secret, scope (optional)
        """
        from urllib.parse import urlencode

        app_id = request.query_params.get("app_id") or os.environ.get("META_APP_ID", "")
        # Store secret in memory for callback (short-lived, localhost only)
        _app_secret = request.query_params.get("app_secret", "")
        if _app_secret:
            _ig_oauth_state["app_secret"] = _app_secret
            _ig_oauth_state["app_id"] = app_id

        scope = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments,instagram_business_manage_insights"
        redirect_uri = f"http://localhost:{settings.api_server_port}/auth/instagram/callback"

        params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "enable_fb_login": "0",
        }
        auth_url = f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"
        return RedirectResponse(url=auth_url)

    @r.get("/auth/instagram/callback")
    async def instagram_oauth_callback(request: Request) -> Any:
        """Handle Instagram OAuth callback. Exchanges code for long-lived token."""
        import aiohttp as _aiohttp

        code = request.query_params.get("code", "")
        error = request.query_params.get("error", "")

        if error:
            return HTMLResponse(f"<h2>❌ Error: {error}</h2><p>Cierra esta ventana y vuelve a intentar.</p>")

        if not code:
            return HTMLResponse("<h2>❌ No se recibió código</h2>")

        app_id = _ig_oauth_state.get("app_id") or os.environ.get("META_APP_ID", "")
        app_secret = _ig_oauth_state.get("app_secret", "")
        redirect_uri = f"http://localhost:{settings.api_server_port}/auth/instagram/callback"

        if not app_secret:
            return HTMLResponse("<h2>❌ App secret no configurado</h2><p>Usa /ig-auth con el secret del app.</p>")

        try:
            # Step 1: Exchange code → short-lived token
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://api.instagram.com/oauth/access_token",
                    data={
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "grant_type": "authorization_code",
                        "redirect_uri": redirect_uri,
                        "code": code,
                    },
                ) as resp:
                    token_data = await resp.json()

            if "error_type" in token_data or "access_token" not in token_data:
                return HTMLResponse(f"<h2>❌ Token exchange failed</h2><pre>{token_data}</pre>")

            short_token = token_data["access_token"]
            ig_user_id = str(token_data.get("user_id", ""))

            # Step 2: Exchange short-lived → long-lived (60 days)
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    "https://graph.instagram.com/access_token",
                    params={
                        "grant_type": "ig_exchange_token",
                        "client_secret": app_secret,
                        "access_token": short_token,
                    },
                ) as resp:
                    long_token_data = await resp.json()

            long_token = long_token_data.get("access_token", short_token)
            expires_in = long_token_data.get("expires_in", 5183944)  # ~60 days

            # Save token + credentials to .env and token file
            import json as _json
            from datetime import datetime as _dt, timezone as _tz

            token_info = {
                "access_token": long_token,
                "user_id": ig_user_id,
                "app_id": app_id,
                "app_secret": app_secret,
                "expires_in": expires_in,
                "created_at": _dt.now(_tz.utc).isoformat(),
                "type": "instagram_login",
                "scopes": ["instagram_business_basic", "instagram_business_content_publish",
                           "instagram_business_manage_comments", "instagram_business_manage_insights"],
            }
            token_path = Path.home() / ".aura" / "instagram_token.json"
            token_path.write_text(_json.dumps(token_info, indent=2))

            # Update .env file
            _update_env_instagram_token(long_token, ig_user_id)

            # Store in state for immediate use
            _ig_oauth_state["token"] = long_token
            _ig_oauth_state["user_id"] = ig_user_id

            logger.info("instagram_oauth_complete", user_id=ig_user_id, expires_in=expires_in)

            # Notify via Telegram if bot is available
            asyncio.create_task(_notify_ig_auth_success(ig_user_id, expires_in))

            return HTMLResponse(f"""
            <html><body style="font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center;">
            <h1>✅ Instagram conectado</h1>
            <p>Token guardado. AURA puede publicar en Instagram.</p>
            <p style="color:#888">User ID: {ig_user_id}</p>
            <p style="color:#888">Expira en: {expires_in // 86400} días</p>
            <p><b>Cierra esta ventana.</b></p>
            </body></html>
            """)

        except Exception as exc:
            logger.error("instagram_oauth_error", error=str(exc))
            return HTMLResponse(f"<h2>❌ Error</h2><pre>{exc}</pre>")

    @r.get("/auth/instagram/refresh")
    async def instagram_token_refresh() -> Dict[str, Any]:
        """Refresh the Instagram long-lived token (call before expiry)."""
        import aiohttp as _aiohttp
        import json as _json

        token_path = Path.home() / ".aura" / "instagram_token.json"
        if not token_path.exists():
            return {"ok": False, "error": "No token saved"}

        info = _json.loads(token_path.read_text())
        token = info.get("access_token", "")

        async with _aiohttp.ClientSession() as sess:
            async with sess.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token},
            ) as resp:
                data = await resp.json()

        if "access_token" in data:
            info["access_token"] = data["access_token"]
            info["expires_in"] = data.get("expires_in", 5183944)
            from datetime import datetime as _dt, timezone as _tz
            info["refreshed_at"] = _dt.now(_tz.utc).isoformat()
            token_path.write_text(_json.dumps(info, indent=2))
            _update_env_instagram_token(data["access_token"], info.get("user_id", ""))
            return {"ok": True, "expires_in": data.get("expires_in"), "message": "Token refreshed"}
        return {"ok": False, "error": str(data)}

    @r.post("/api/social/generate")
    async def social_generate(request: Request) -> Dict[str, Any]:
        """Generate social media content (caption + AI photorealistic images) without posting.
        Body: {topic, platforms, format, width, height, count, style}
        Uses Gemini for caption + Pollinations FLUX.1 for photorealistic images.
        Supports carousel: count=1..10 generates multiple image variations.
        Downloads and saves images locally to ~/.aura/social_drafts/.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        import time
        import urllib.parse
        import aiohttp
        from datetime import datetime, timezone
        from src.workflows.social_publisher import generate_social_content

        topic = (body.get("topic") or "Claude AI y diseño web").strip()
        platforms = body.get("platforms", ["instagram"])
        fmt = body.get("format", "1:1")
        platform = platforms[0] if platforms else "instagram"
        count = max(1, min(10, int(body.get("count", 1))))
        style = (body.get("style") or "photorealistic").strip()

        # Format → dimensions map
        fmt_dims = {
            "1:1": (1080, 1080), "4:5": (1080, 1350),
            "9:16": (1080, 1920), "16:9": (1920, 1080),
        }
        w, h = fmt_dims.get(fmt, (1080, 1080))

        # Style → quality modifier for FLUX prompt
        style_modifiers = {
            "photorealistic": "photorealistic, editorial photography, cinematic lighting, 8K, ultra-detailed",
            "bold": "bold graphic design, high contrast, vibrant colors, modern poster style, professional",
            "minimal": "minimalist, clean white space, elegant typography, subtle gradient, modern studio",
            "dark": "dark moody atmosphere, deep shadows, neon accents, luxury brand aesthetic, dramatic",
        }
        quality = style_modifiers.get(style, style_modifiers["photorealistic"])

        # Local drafts directory
        drafts_dir = Path.home() / ".aura" / "social_drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)

        async def _bg_download(poll_url: str, filename: str) -> None:
            """Background task: download FLUX.1 image and save locally."""
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        poll_url, timeout=aiohttp.ClientTimeout(total=120)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            (drafts_dir / filename).write_bytes(data)
                            logger.info("flux_saved", filename=filename, kb=len(data)//1024)
            except Exception as e:
                logger.debug("flux_bg_download_failed", filename=filename, error=str(e))

        try:
            # 1. Generate SEO caption + image prompt via Gemini
            caption, image_prompt_base = await generate_social_content(topic, platform)

            # 2. Build FLUX.1 prompt with style and quality modifiers
            flux_prompt = (
                f"{image_prompt_base}, {quality}, "
                f"RUD Studio Barcelona creative agency branding, no text, no watermark"
            )

            # 3. Build N Pollinations URLs with different seeds — returned immediately
            base_seed = int(time.time())
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            carousel_urls = []
            for i in range(count):
                seed = base_seed + i * 137
                encoded = urllib.parse.quote(flux_prompt)
                poll_url = (
                    f"https://image.pollinations.ai/prompt/{encoded}"
                    f"?width={w}&height={h}&model=flux&seed={seed}&nologo=true"
                )
                carousel_urls.append(poll_url)
                # Fire-and-forget background save (doesn't block the response)
                filename = f"{platform}_{fmt.replace(':','')}_{style}_{ts}_{i+1}.jpg"
                asyncio.create_task(_bg_download(poll_url, filename))

            return {
                "ok": True,
                "caption": caption,
                "image_url": carousel_urls[0],
                "carousel_urls": carousel_urls,
                "local_dir": str(drafts_dir),
                "count": count,
                "topic": topic,
                "platform": platform,
                "format": fmt,
                "style": style,
            }
        except Exception as e:
            logger.warning("social_generate_error", error=str(e))
            return {"ok": False, "caption": None, "image_url": None, "carousel_urls": [], "error": str(e)}

    @r.get("/api/social/drafts/{filename}")
    async def serve_social_draft(filename: str) -> Any:
        """Serve a locally saved social draft image."""
        from fastapi.responses import FileResponse as _FR
        # Sanitize — no path traversal
        if ".." in filename or "/" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path.home() / ".aura" / "social_drafts" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Draft not found")
        media_type = "image/jpeg" if filename.endswith(".jpg") else "image/png"
        return _FR(str(path), media_type=media_type)

    @r.get("/api/social/drafts")
    async def list_social_drafts() -> Dict[str, Any]:
        """List all saved draft images."""
        drafts_dir = Path.home() / ".aura" / "social_drafts"
        if not drafts_dir.exists():
            return {"drafts": [], "dir": str(drafts_dir)}
        files = sorted(drafts_dir.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
        files += sorted(drafts_dir.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
        return {
            "drafts": [
                {
                    "filename": f.name,
                    "url": f"/api/social/drafts/{f.name}",
                    "size_kb": f.stat().st_size // 1024,
                    "created": f.stat().st_mtime,
                }
                for f in files[:50]
            ],
            "dir": str(drafts_dir),
        }

    @r.post("/api/social/post")
    async def social_post_content(request: Request) -> Dict[str, Any]:
        """Generate image + caption and post via N8N (or save draft).
        Body: {text, platforms, format, width, height, topic?}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        text = (body.get("text") or "").strip()
        topic = body.get("topic") or text or "social media post"
        platforms = body.get("platforms", ["instagram"])
        fmt = body.get("format", "1:1")
        width = body.get("width", 1080)
        height = body.get("height", 1080)
        if not text and not topic:
            raise HTTPException(status_code=400, detail="text or topic required")
        try:
            from src.workflows.social_post import (
                generate_images_for_post, generate_captions,
                post_to_social, build_n8n_payload,
            )
            platform = platforms[0] if platforms else "instagram"
            style = f"social media {fmt}, dark background #141413, orange accent #d97757, professional, {width}x{height}"
            count = 1
            images = await generate_images_for_post(topic, count, style)
            captions_list = await generate_captions(topic, images, platform, style)
            caption = captions_list[0] if captions_list else text
            ok_images = [img for img in images if not img.get("error")]
            image_url = ok_images[0]["url"] if ok_images else None
            n8n_url = os.environ.get("RUD_N8N_URL", "")
            result = {"success": False, "error": "N8N not configured", "draft_saved": ""}
            if n8n_url:
                result = await post_to_social(platform, "post", topic, ok_images, [caption], n8n_url)
            else:
                # Save draft locally
                import json as _json
                from datetime import datetime, timezone
                drafts_dir = Path.home() / ".aura" / "social_drafts"
                drafts_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                draft_file = drafts_dir / f"{platform}_{ts}.json"
                draft_data = {"platform": platform, "type": "post", "caption": caption, "image_url": image_url, "topic": topic, "format": fmt, "timestamp": ts}
                draft_file.write_text(_json.dumps(draft_data, ensure_ascii=False, indent=2))
                result = {"success": False, "error": f"N8N no configurado — borrador guardado en {draft_file}", "draft_saved": str(draft_file)}
            return {
                "ok": result["success"],
                "image_url": image_url,
                "caption": caption,
                "platform": platform,
                "draft_saved": result.get("draft_saved", ""),
                "post_url": result.get("post_url", ""),
                "error": result.get("error", "") if not result["success"] else "",
            }
        except Exception as e:
            return {"ok": False, "image_url": None, "error": str(e)}

    return r

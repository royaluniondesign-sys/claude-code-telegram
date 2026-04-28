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

    @r.post("/api/social/enhance-prompt")
    async def social_enhance_prompt(request: Request) -> Dict[str, Any]:
        """Enhance a plain-text description into a quality FLUX.1 image prompt.

        Body: {text: str, format: "1:1"|"4:5"|"9:16"|"16:9"}
        Returns: {ok: bool, prompt: str}
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        import asyncio as _asyncio
        from src.workflows.social_publisher import _GEMINI_FAST_ENV

        text = (body.get("text") or "").strip()
        fmt = (body.get("format") or "1:1").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")

        fmt_hint = {
            "1:1": "square 1:1 composition",
            "4:5": "vertical 4:5 portrait",
            "9:16": "tall vertical 9:16 story",
            "16:9": "wide 16:9 landscape",
        }.get(fmt, "square composition")

        enhance_prompt = (
            f"You are a FLUX.1 image generation expert. "
            f"Improve the following user description into a high-quality FLUX.1 prompt. "
            f"Rules: keep the user's intent exactly, describe a SPECIFIC scene or object "
            f"(not a generic stock photo), {fmt_hint}, cool or neutral tones, "
            f"~60 words in English, no prohibited content, no people unless explicitly mentioned. "
            f"Output ONLY the improved prompt text — no explanation, no quotes, no markdown.\n\n"
            f"User description: {text}"
        )

        try:
            proc = await _asyncio.create_subprocess_exec(
                "gemini", "-o", "json", "-p", enhance_prompt,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                env=_GEMINI_FAST_ENV,
                cwd="/tmp",
            )
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=30)
            raw = stdout.decode().strip()
            # Unwrap gemini -o json envelope
            import json as _json
            try:
                outer = _json.loads(raw)
                if isinstance(outer, dict) and "response" in outer:
                    raw = str(outer["response"]).strip()
            except Exception:
                pass
            # Strip markdown code fences if present
            raw = re.sub(r"^```[^\n]*\n?", "", raw.strip())
            raw = re.sub(r"\n?```$", "", raw.strip())
            enhanced = raw.strip()
            if enhanced:
                return {"ok": True, "prompt": enhanced}
        except Exception as e:
            logger.warning("enhance_prompt_failed", error=str(e))

        # Fallback: return original text + basic style suffix
        fallback = f"{text}, editorial photography, cool neutral tones, {fmt_hint}, professional quality, no text, no watermark"
        return {"ok": True, "prompt": fallback}

    @r.post("/api/social/generate")
    async def social_generate(request: Request) -> Dict[str, Any]:
        """Generate social media content: SEO caption + FLUX.1 image(s).

        Rate limits (Pollinations free tier):
          - 1 concurrent request per IP, queue=1
          - Images are fetched SEQUENTIALLY for carousel to avoid 429s
          - Each image: ~15-30s generation time
          - Max resolution returned: ~768-1080px (Pollinations enforced)

        Body: {topic, platforms, format, count, style}
          style: photorealistic | bold | minimal | dark
          count: 1-5 (capped at 3 for free tier — sequential, ~30s each)
          format: 1:1 | 4:5 | 9:16 | 16:9
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        import time
        import urllib.parse
        import aiohttp
        from datetime import datetime, timezone
        from src.workflows.social_publisher import (
            generate_social_content,
            generate_caption_concept,
            refine_caption_with_claude,
            _STYLE_MOOD,
            generate_image_nvidia,
        )

        topic = (body.get("topic") or "diseño y creatividad Barcelona").strip()
        direct_prompt = (body.get("direct_prompt") or "").strip()   # skip LLM → straight to FLUX
        direct_caption = (body.get("caption") or "").strip()        # use caption as-is
        platforms = body.get("platforms", ["instagram"])
        fmt = body.get("format", "1:1")
        platform = platforms[0] if platforms else "instagram"
        _count_raw = int(body.get("count", 1))
        caption_only = _count_raw == 0  # count=0 means caption generation only, no images
        count = max(1, min(10, _count_raw)) if not caption_only else 1
        style = (body.get("style") or "photorealistic").strip()
        brain = (body.get("brain") or "auto").strip()

        # Format → FLUX composition context
        fmt_composition = {
            "1:1":  "square 1:1 centered composition, perfect symmetry, Instagram feed format, subject centered with breathing room",
            "4:5":  "vertical 4:5 portrait, editorial close-up, magazine cover crop, subject fills upper two-thirds",
            "9:16": "tall 9:16 full-bleed mobile story, immersive vertical, subject dominates frame, cinematic crop",
            "16:9": "wide 16:9 cinematic landscape, panoramic depth, rule of thirds, widescreen film aesthetic",
        }
        composition = fmt_composition.get(fmt, "square centered composition")

        def _flux_fallback_prompt(concept_text: str) -> str:
            """Last-resort fallback FLUX prompt when AI generation fails."""
            core = concept_text.strip() if concept_text and len(concept_text) > 20 else topic
            mood = _STYLE_MOOD.get(style, _STYLE_MOOD["photorealistic"])
            return f"{core}, {mood}, {composition}, cool neutral tones, no text, no watermark"

        # Brain → caption + flux_prompts generation
        # claude/auto: Gemini Flash concept → Claude refines caption (quality pipeline, ~8s)
        # gemini-flash/gemini/codex: direct call to that model only (fast, ~3-5s)
        # Returns (caption, flux_prompts_list) — N prompts for N carousel images
        async def _generate_caption_with_brain(t: str, p: str, n: int = 1) -> tuple[str, list[str]]:
            if brain in ("claude", "auto"):
                concept = await generate_caption_concept(t, p, count=n, style=style, composition=composition)
                caption = await refine_caption_with_claude(concept, t, p)
                flux_prompts: list[str] = concept.get("flux_prompts") or []
                if not flux_prompts:
                    flux_prompts = [_flux_fallback_prompt(concept.get("image_prompt", t))]
                while len(flux_prompts) < n:
                    flux_prompts.append(flux_prompts[-1])
                return caption, flux_prompts[:n]
            # Explicit brain — route directly to that model (no cascade)
            caption, flux_prompts = await generate_social_content(
                t, p, brain=brain, count=n, style=style, composition=composition
            )
            while len(flux_prompts) < n:
                flux_prompts.append(flux_prompts[-1])
            return caption, flux_prompts[:n]

        # Local drafts directory
        drafts_dir = Path.home() / ".aura" / "social_drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)

        # Format → pixel dimensions (NVIDIA FLUX.1-schnell valid: 768,832,896,960,1024,1088,1152,1216,1280,1344)
        _fmt_dims: dict[str, tuple[int, int]] = {
            "1:1":  (1024, 1024),
            "4:5":  (1024, 1280),
            "9:16": (768,  1344),  # Story vertical (was 576×1024 — invalid for NVIDIA)
            "16:9": (1344, 768),
        }
        img_w, img_h = _fmt_dims.get(fmt, (1024, 1024))

        async def _fetch_hf(prompt: str, seed: int) -> bytes | None:
            """HuggingFace FLUX.1-schnell via Inference Providers API (2025)."""
            hf_token = os.environ.get("HF_TOKEN", "")
            if not hf_token:
                return None
            endpoint = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {hf_token}", "Content-Type": "application/json"},
                        json={"inputs": prompt, "parameters": {"width": img_w, "height": img_h, "num_inference_steps": 4, "seed": seed}},
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data[:2] in (b'\xff\xd8', b'\x89P'):
                                logger.info("hf_flux_ok", res=f"{img_w}x{img_h}", kb=len(data)//1024)
                                return data
                        body = await resp.text() if resp.status != 200 else ""
                        logger.warning("hf_flux_error", status=resp.status, body=body[:150])
            except Exception as e:
                logger.warning("hf_flux_exception", error=str(e))
            return None

        async def _fetch_pollinations(prompt: str, seed: int) -> bytes | None:
            """Pollinations.ai fallback (no token, lower quality)."""
            encoded = urllib.parse.quote(prompt)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={img_w}&height={img_h}&seed={seed}&nologo=true"
            )
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data[:2] in (b'\xff\xd8', b'\x89P'):
                                return data
            except Exception as e:
                logger.warning("pollinations_error", error=str(e))
            return None

        async def _fetch_one(prompt: str, seed: int, filename: str) -> str | None:
            """Fetch ONE image: NVIDIA FLUX.1-schnell → HuggingFace → Pollinations fallback."""
            data: bytes | None = None
            source = "nvidia"
            try:
                data = await generate_image_nvidia(prompt, width=img_w, height=img_h)
            except Exception as e:
                logger.warning("nvidia_gen_failed_webhook", error=str(e)[:80])
            if not data:
                data = await _fetch_hf(prompt, seed)
                source = "hf-flux"
            if not data:
                data = await _fetch_pollinations(prompt, seed)
                source = "pollinations"
            if not data:
                logger.warning("image_fetch_failed", filename=filename)
                return None
            (drafts_dir / filename).write_bytes(data)
            logger.info("image_saved", filename=filename, source=source, kb=len(data)//1024)
            return f"/api/social/drafts/{filename}"

        try:
            base_seed = int(time.time())
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            first_filename = f"{platform}_{fmt.replace(':','')}_{style}_{ts}_1.jpg"

            # DIRECT MODE: skip all AI caption/prompt generation, use provided values directly
            if direct_prompt:
                caption = direct_caption or topic
                flux_prompts = [direct_prompt] * count
            else:
                # STAGE 1 (~3-8s): AI generates caption + N FLUX prompts (one per image)
                # Each brain routes exclusively to its model; claude/auto uses quality pipeline
                caption, flux_prompts = await _generate_caption_with_brain(topic, platform, n=count)

            # CAPTION-ONLY MODE: return caption without generating any image
            if caption_only:
                return {"ok": True, "caption": caption, "image_url": None, "carousel_urls": []}

            # STAGE 2: Generate images — each image uses its own AI-crafted prompt
            # (AI owns the creative direction per image; no rigid template override)
            first_flux_prompt = flux_prompts[0] if flux_prompts else _flux_fallback_prompt(topic)
            first_img_url = await _fetch_one(first_flux_prompt, base_seed, first_filename)

            if not first_img_url:
                return {
                    "ok": False,
                    "caption": caption,
                    "image_url": None,
                    "carousel_urls": [],
                    "error": "FLUX.1 rate limit — espera 30s y reintenta",
                    "rate_limit_info": "HuggingFace free tier: ~1 req/30s. Espera 30s entre generaciones.",
                }

            carousel_urls: list[str] = [first_img_url]

            # Additional carousel images — each gets its own narrative flux prompt
            for i in range(1, count):
                seed = base_seed + i * 3001
                filename = f"{platform}_{fmt.replace(':','')}_{style}_{ts}_{i+1}.jpg"
                await asyncio.sleep(2)
                # Use per-image prompt if AI provided N prompts, else reuse first
                prompt_i = flux_prompts[i] if i < len(flux_prompts) else first_flux_prompt
                local_url = await _fetch_one(prompt_i, seed, filename)
                if local_url:
                    carousel_urls.append(local_url)
                else:
                    break

            brain_label = {
                "claude": "Claude (Creative Director)",
                "auto": "Claude (Creative Director)",
                "gemini-flash": "Gemini Flash",
                "gemini": "Gemini",
                "codex": "ChatGPT",
            }.get(brain, "Claude")

            return {
                "ok": True,
                "caption": caption,
                "image_url": carousel_urls[0],
                "carousel_urls": carousel_urls,
                "local_dir": str(drafts_dir),
                "count": len(carousel_urls),
                "topic": topic,
                "platform": platform,
                "format": fmt,
                "style": style,
                "brain": brain,
                "brain_label": brain_label,
                "flux_prompt": first_flux_prompt[:200],
                "flux_info": {
                    "model": "FLUX.1-schnell (HuggingFace free → Pollinations fallback)",
                    "resolution": f"{img_w}×{img_h}px",
                    "time_per_image": "10-30s",
                    "images_generated": len(carousel_urls),
                },
            }
        except Exception as e:
            logger.warning("social_generate_error", error=str(e))
            return {"ok": False, "caption": None, "image_url": None, "carousel_urls": [], "error": str(e)}

    @r.post("/api/social/upload-image")
    async def social_upload_image(request: Request) -> Dict[str, Any]:
        """Upload an image file directly to drafts (multipart/form-data, field: 'file').

        Returns: {ok: bool, url: str, filename: str}
        """
        from fastapi import UploadFile
        import mimetypes
        from datetime import datetime, timezone

        try:
            form = await request.form()
            upload: UploadFile = form.get("file")  # type: ignore[assignment]
            if not upload or not upload.filename:
                raise HTTPException(status_code=400, detail="No file provided")

            # Validate mime type — images only
            content_type = upload.content_type or mimetypes.guess_type(upload.filename)[0] or ""
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="Only image files allowed")

            data = await upload.read()
            if len(data) > 20 * 1024 * 1024:  # 20MB max
                raise HTTPException(status_code=400, detail="File too large (max 20MB)")

            # Sanitize filename and save
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            ext = ".jpg" if "jpeg" in content_type or "jpg" in upload.filename.lower() else ".png"
            safe_name = f"upload_{ts}{ext}"
            drafts_dir = Path.home() / ".aura" / "social_drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)
            (drafts_dir / safe_name).write_bytes(data)
            logger.info("image_uploaded", filename=safe_name, kb=len(data) // 1024)
            return {"ok": True, "url": f"/api/social/drafts/{safe_name}", "filename": safe_name}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("upload_image_error", error=str(e))
            return {"ok": False, "error": str(e)}

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

    @r.delete("/api/social/drafts/{filename}")
    async def delete_social_draft(filename: str) -> Dict[str, Any]:
        """Delete a locally saved social draft image."""
        if ".." in filename or "/" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path.home() / ".aura" / "social_drafts" / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Draft not found")
        path.unlink()
        return {"deleted": filename}

    @r.post("/api/social/drafts/open-folder")
    async def open_drafts_folder() -> Dict[str, Any]:
        """Open the social_drafts folder in Finder."""
        import subprocess as _sp
        drafts_dir = Path.home() / ".aura" / "social_drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        _sp.Popen(["open", str(drafts_dir)])
        return {"opened": str(drafts_dir)}

    # ── SERVER-SIDE HISTORY (survives tunnel URL changes) ───────────────────
    _HISTORY_FILE = Path.home() / ".aura" / "social_history.json"

    @r.get("/api/social/history")
    async def social_history_get() -> Dict[str, Any]:
        """Load published post history from server (persists across tunnel restarts)."""
        import json as _json
        try:
            if _HISTORY_FILE.exists():
                posts = _json.loads(_HISTORY_FILE.read_text())
                return {"ok": True, "posts": posts}
        except Exception:
            pass
        return {"ok": True, "posts": []}

    @r.post("/api/social/history")
    async def social_history_save(request: Request) -> Dict[str, Any]:
        """Append a post record to server-side history."""
        import json as _json
        try:
            post = await request.json()
            existing: list = []
            if _HISTORY_FILE.exists():
                existing = _json.loads(_HISTORY_FILE.read_text())
            existing.append(post)
            # Keep last 200 posts
            existing = existing[-200:]
            _HISTORY_FILE.write_text(_json.dumps(existing, ensure_ascii=False))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @r.delete("/api/social/history/{post_id}")
    async def social_history_delete(post_id: str) -> Dict[str, Any]:
        """Delete one post from history by id."""
        import json as _json
        try:
            if _HISTORY_FILE.exists():
                posts = _json.loads(_HISTORY_FILE.read_text())
                posts = [p for p in posts if p.get("id") != post_id]
                _HISTORY_FILE.write_text(_json.dumps(posts, ensure_ascii=False))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── INSTAGRAM STORY ─────────────────────────────────────────────────────
    @r.post("/api/social/story")
    async def social_post_story(request: Request) -> Dict[str, Any]:
        """Post an image as Instagram Story (9:16 format, media_type=STORIES).

        Body: {image_url: str (public HTTPS), caption?: str}
        The image_url must be publicly accessible (GitHub CDN or similar).
        """
        import aiohttp as _aiohttp
        from src.workflows.social_publisher import (
            _ig_token, _ig_account_id, _ig_wait_ready,
            generate_image_nvidia, upload_image_to_host,
        )
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        image_url: str = body.get("image_url", "")
        # If no public URL provided, generate a 9:16 story image
        if not image_url:
            prompt = body.get("prompt", "cinematic vertical story, bold visual impact, 9:16 format, no text")
            try:
                img_bytes = await generate_image_nvidia(prompt, width=768, height=1344)
                image_url = await upload_image_to_host(img_bytes)
            except Exception as e:
                return {"ok": False, "error": f"Image generation failed: {e}"}

        ig_token = _ig_token()
        ig_id = _ig_account_id()
        if not ig_id:
            return {"ok": False, "error": "INSTAGRAM_ACCOUNT_ID not configured"}

        _graph = "https://graph.facebook.com/v22.0"
        try:
            async with _aiohttp.ClientSession() as session:
                # Step 1: Create media container (STORIES)
                async with session.post(
                    f"{_graph}/{ig_id}/media",
                    params={
                        "image_url": image_url,
                        "media_type": "STORIES",
                        "access_token": ig_token,
                    },
                    timeout=_aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    creation_id = data.get("id")
                    if not creation_id:
                        return {"ok": False, "error": f"Container creation failed: {data}"}

                # Step 2: Wait for processing
                await _ig_wait_ready(session, creation_id, max_wait=30)

                # Step 3: Publish
                async with session.post(
                    f"{_graph}/{ig_id}/media_publish",
                    params={"creation_id": creation_id, "access_token": ig_token},
                    timeout=_aiohttp.ClientTimeout(total=30),
                ) as resp:
                    pub = await resp.json()
                    post_id = pub.get("id")
                    if not post_id:
                        return {"ok": False, "error": f"Publish failed: {pub}"}
                    return {"ok": True, "post_id": post_id, "image_url": image_url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

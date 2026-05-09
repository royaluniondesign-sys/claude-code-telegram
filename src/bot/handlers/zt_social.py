"""Zero-token social media and video commands — post, video, design."""

import os as _os

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenSocialMixin:
    """Mixin: social media pipeline and video generation zero-token commands."""

    async def _zt_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Social media content pipeline — generates images + captions → N8N.

        Usage:
          /post instagram carrusel 5 sobre claude code
          /post twitter hilo sobre ia y automatización
          /post linkedin post sobre productividad
          /post instagram 3 sobre diseño minimalista
        """
        args_text = (update.message.text or "").split(maxsplit=1)
        if len(args_text) < 2:
            await update.message.reply_text(
                "📱 <b>/post — Publicación de Contenido</b>\n\n"
                "<b>Blog (rud-web.vercel.app):</b>\n"
                "  <code>/post blog IA local en agencias creativas</code>\n"
                "  <code>/post blog tendencias branding 2026</code>\n\n"
                "<b>Instagram:</b>\n"
                "  <code>/post instagram carrusel sobre claude code</code>\n"
                "  <code>/post instagram post sobre diseño minimalista</code>\n\n"
                "<b>Facebook:</b>\n"
                "  <code>/post facebook sobre automatización con IA</code>\n\n"
                "<b>Ambas redes sociales:</b>\n"
                "  <code>/post social sobre branding Barcelona</code>\n\n"
                "Plataformas: blog · instagram · facebook · social\n"
                "💡 También funciona desde el chat del Dashboard.",
                parse_mode="HTML",
            )
            return

        raw_prompt = args_text[1].strip()
        parts = raw_prompt.split(maxsplit=1)
        platform_hint = parts[0].lower() if parts else ""

        # Route blog posts to the new blog publisher
        if platform_hint in ("blog", "articulo", "artículo", "post-blog"):
            topic = parts[1] if len(parts) > 1 else raw_prompt
            await self._handle_blog_post(update, context, topic)
        elif platform_hint in ("facebook", "fb"):
            rest = parts[1] if len(parts) > 1 else raw_prompt
            await self._route_social_or_schedule(update, context, "facebook", rest)
        elif platform_hint in ("social", "ambas", "both", "all"):
            rest = parts[1] if len(parts) > 1 else raw_prompt
            await self._handle_social_direct(update, context, rest, ["instagram", "facebook"])
        elif platform_hint in ("instagram", "ig"):
            rest = parts[1] if len(parts) > 1 else raw_prompt
            await self._route_social_or_schedule(update, context, "instagram", rest)
        else:
            # Legacy: instagram/twitter/linkedin via brand image pipeline
            await self._handle_social_post(update, context, raw_prompt)

    async def _route_social_or_schedule(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        platform: str,
        rest: str,
    ) -> None:
        """Detect 'schedule' keyword and route accordingly."""
        parts = rest.split(maxsplit=1)
        if parts and parts[0].lower() in ("schedule", "programar", "programa", "alas", "en"):
            # /post instagram schedule <time_and_topic>
            # Separate time tokens from topic using "sobre" as delimiter
            payload = rest if parts[0].lower() not in ("schedule", "programar", "programa") else (parts[1] if len(parts) > 1 else "")
            if "sobre" in payload.lower():
                idx = payload.lower().index("sobre")
                time_str = payload[:idx].strip()
                topic = payload[idx + 5:].strip()
            else:
                # fallback: first 3 words = time, rest = topic
                words = payload.split(maxsplit=3)
                time_str = " ".join(words[:3])
                topic = words[3] if len(words) > 3 else payload
            await self._zt_social_schedule(update, context, platform, time_str, topic)
        else:
            await self._handle_social_direct(update, context, rest, [platform])

    async def _handle_blog_post(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
        topic: str,
    ) -> None:
        """Generate and publish a blog post to rud-web.vercel.app."""
        from telegram import Update
        from telegram.ext import ContextTypes

        progress = await update.message.reply_text(
            f"📝 <b>Generando artículo:</b> {topic[:60]}...",
            parse_mode="HTML",
        )
        try:
            await progress.edit_text(
                "✍️ <b>AURA escribiendo...</b> (Gemini generando contenido)",
                parse_mode="HTML",
            )
            from src.workflows.blog_publisher import publish_blog_from_topic
            result = await publish_blog_from_topic(topic)

            if result.get("ok"):
                post = result.get("post", {})
                await progress.edit_text(
                    f"✅ <b>Artículo publicado en el blog</b>\n\n"
                    f"📄 <b>{post.get('title', topic)}</b>\n"
                    f"📂 {post.get('category', '')} · {post.get('date', '')}\n\n"
                    f"🔗 <a href=\"{result['url']}\">{result['url']}</a>\n\n"
                    f"⏱ Vercel desplegará en ~60s\n"
                    f"📦 Commit: <code>{result.get('commit_sha', '')}</code>",
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
            else:
                await progress.edit_text(
                    f"❌ <b>Error publicando artículo</b>\n\n{result.get('error', 'Error desconocido')}",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("blog_post_handler_error", error=str(e))
            try:
                await progress.edit_text(f"❌ Error: {e}", parse_mode="HTML")
            except Exception:
                pass

    async def _handle_social_direct(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
        topic: str,
        platforms: list,
    ) -> None:
        """Publish directly to Instagram/Facebook via Meta Graph API."""
        platforms_str = " + ".join(p.capitalize() for p in platforms)
        progress = await update.message.reply_text(
            f"📱 <b>Publicando en {platforms_str}...</b>\n"
            f"🎨 Generando imagen con FLUX.1...",
            parse_mode="HTML",
        )
        try:
            from src.workflows.social_publisher import publish_social
            result = await publish_social(description=topic, platforms=platforms)

            lines = [f"{'✅' if result.get('ok') else '⚠️'} <b>Resultado {platforms_str}</b>\n"]

            if result.get("caption"):
                lines.append(f"📝 Caption: <i>{result['caption'][:120]}...</i>\n")

            for platform, pr in result.get("platforms", {}).items():
                if pr.get("ok"):
                    lines.append(f"✅ <b>{platform.capitalize()}</b>: <a href=\"{pr.get('url','#')}\">Ver post</a>")
                else:
                    err = pr.get("error", "error")
                    if pr.get("action_required") == "M3":
                        lines.append(
                            f"⚠️ <b>{platform.capitalize()}</b>: Token válido pero cuenta no conectada.\n"
                            f"   Acción necesaria: conectar cuenta en Meta Business Manager.\n"
                            f"   El post se guardó como borrador."
                        )
                    else:
                        lines.append(f"❌ <b>{platform.capitalize()}</b>: {err[:80]}")

            if result.get("draft_saved"):
                lines.append(f"\n💾 Borrador guardado: <code>{result['draft_saved']}</code>")

            await progress.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

        except Exception as e:
            logger.error("social_direct_handler_error", error=str(e))
            try:
                await progress.edit_text(f"❌ Error: {e}", parse_mode="HTML")
            except Exception:
                pass

    async def _zt_video(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎬 Video generation — cinematic AI or structured slides.

        Usage:
          /video cinematic <prompt>     — Luma/Kling/Runway cinematic AI video
          /video slides <N> <topic>     — json2video structured slide video
          /video help                   — show options and configured providers
        """
        args = (update.message.text or "").split(maxsplit=2)
        subcommand = args[1].lower() if len(args) > 1 else "help"

        if subcommand == "help" or len(args) < 2:
            luma_ok = "✅" if _os.environ.get("LUMA_API_KEY", "").strip() else "❌"
            kling_ok = "✅" if _os.environ.get("KLING_API_KEY", "").strip() else "❌"
            runway_ok = "✅" if _os.environ.get("RUNWAY_API_KEY", "").strip() else "❌"
            j2v_ok = "✅" if _os.environ.get("JSON2VIDEO_API_KEY", "").strip() else "❌"

            await update.message.reply_text(
                "🎬 <b>/video — Video Generation</b>\n\n"
                "<b>Modos:</b>\n"
                "  <code>/video cinematic &lt;prompt&gt;</code>\n"
                "    Kling/Luma cinematic AI video\n\n"
                "  <code>/video slides &lt;N&gt; &lt;topic&gt;</code>\n"
                "    json2video structured slides (e.g. /video slides 5 claude code)\n\n"
                "  <code>/video help</code> — este mensaje\n\n"
                "<b>Proveedores configurados:</b>\n"
                f"  {luma_ok} LUMA_API_KEY (Dream Machine)\n"
                f"  {kling_ok} KLING_API_KEY (Kling AI)\n"
                f"  {runway_ok} RUNWAY_API_KEY (Runway ML)\n"
                f"  {j2v_ok} JSON2VIDEO_API_KEY (slides)\n\n"
                "💡 También puedes escribir directamente:\n"
                '  <i>"crea un video de 10s de un developer usando AI"</i>\n'
                '  <i>"haz un video de 5 slides sobre automatización"</i>',
                parse_mode="HTML",
            )
            return

        router = context.bot_data.get("brain_router")

        if subcommand == "slides":
            # /video slides <N> <topic>  OR  /video slides <topic>
            rest = args[2] if len(args) > 2 else ""
            if not rest:
                await update.message.reply_text(
                    "Uso: <code>/video slides &lt;N&gt; &lt;topic&gt;</code>\n"
                    "Ejemplo: <code>/video slides 5 claude code</code>",
                    parse_mode="HTML",
                )
                return
            # Inject "slides" keyword so video_compose picks the right route
            synthetic_prompt = f"video de slides {rest}"
            await self._handle_video_gen(update, context, router, synthetic_prompt, update.effective_user.id)

        elif subcommand == "cinematic":
            rest = args[2] if len(args) > 2 else ""
            if not rest:
                await update.message.reply_text(
                    "Uso: <code>/video cinematic &lt;prompt&gt;</code>\n"
                    "Ejemplo: <code>/video cinematic developer coding at night, neon lights</code>",
                    parse_mode="HTML",
                )
                return
            await self._handle_video_gen(update, context, router, rest, update.effective_user.id)

        else:
            # Treat the whole thing as a cinematic prompt
            raw = " ".join(args[1:]).strip()
            await self._handle_video_gen(update, context, router, raw, update.effective_user.id)

    # ── /imagen ────────────────────────────────────────────────────────────────

    async def _zt_imagen(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎨 Generate image directly with FLUX.1-dev — no LLM in pipeline.

        Usage:
          /imagen <prompt en inglés>
          /imagen a dark studio portrait of a woman, dramatic side lighting, film grain
        """
        args_text = (update.message.text or "").split(maxsplit=1)
        if len(args_text) < 2 or not args_text[1].strip():
            await update.message.reply_text(
                "🎨 <b>/imagen — Generar imagen con FLUX.1</b>\n\n"
                "Escribe el prompt directamente en inglés (más preciso):\n"
                "<code>/imagen dark cinematic portrait, film grain, dramatic light</code>\n"
                "<code>/imagen minimalist product photo, white background, shadows</code>\n\n"
                "💡 Para publicar: <code>/imagen &lt;prompt&gt;</code> → usa los botones de la imagen",
                parse_mode="HTML",
            )
            return

        prompt = args_text[1].strip()
        status_msg = await update.message.reply_text(
            f"🎨 <b>FLUX.1-dev generando...</b>\n<code>{prompt[:80]}</code>",
            parse_mode="HTML",
        )

        try:
            from src.workflows.social_publisher import generate_image_bytes

            img_bytes = await generate_image_bytes(prompt)

            if not img_bytes:
                await status_msg.edit_text("❌ No se pudo generar la imagen. Prueba con otro prompt.")
                return

            # Save to drafts
            import time, re as _re, hashlib
            slug = _re.sub(r"[^a-z0-9]+", "_", prompt.lower())[:30].strip("_")
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"instagram_11_imagen_{ts}_{slug}.jpg"
            draft_path = _os.path.expanduser(f"~/.aura/social_drafts/{filename}")
            _os.makedirs(_os.path.dirname(draft_path), exist_ok=True)
            with open(draft_path, "wb") as f:
                f.write(img_bytes)

            draft_url = f"/api/social/drafts/{filename}"

            # Delete status message and send photo
            await status_msg.delete()

            caption_text = f"<b>FLUX.1-dev</b> · {len(img_bytes)//1024}KB\n<code>{prompt[:100]}</code>"
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Publicar Instagram", callback_data=f"img_pub_ig:{filename}"),
                InlineKeyboardButton("🔄 Regenerar", callback_data=f"img_regen:{prompt[:80]}"),
            ]])

            import io
            await update.message.reply_photo(
                photo=io.BytesIO(img_bytes),
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info("imagen_cmd_ok", filename=filename, size_kb=len(img_bytes)//1024)

        except Exception as e:
            logger.error("imagen_cmd_error", error=str(e))
            try:
                await status_msg.edit_text(f"❌ Error: {str(e)[:200]}", parse_mode="HTML")
            except Exception:
                pass

    # ── /galeria ───────────────────────────────────────────────────────────────

    async def _zt_galeria(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🖼 Listar imágenes guardadas en drafts y opción de publicar.

        Usage:
          /galeria               — lista los últimos 10 drafts
          /galeria pub <nombre>  — publica draft directamente
        """
        args = (update.message.text or "").split(maxsplit=2)

        drafts_dir = _os.path.expanduser("~/.aura/social_drafts/")

        if len(args) >= 3 and args[1].lower() in ("pub", "publicar", "post"):
            # /galeria pub <filename>
            filename = args[2].strip()
            await self._galeria_publish(update, context, drafts_dir, filename)
            return

        # List recent drafts
        if not _os.path.isdir(drafts_dir):
            await update.message.reply_text("Sin imágenes aún — genera una con <code>/imagen</code>", parse_mode="HTML")
            return

        images = sorted(
            [f for f in _os.listdir(drafts_dir) if f.lower().endswith((".jpg", ".jpeg", ".png"))],
            key=lambda f: _os.path.getmtime(_os.path.join(drafts_dir, f)),
            reverse=True,
        )[:10]

        if not images:
            await update.message.reply_text(
                "📂 Sin imágenes en la galería.\n"
                "Genera una con <code>/imagen &lt;prompt&gt;</code>",
                parse_mode="HTML",
            )
            return

        lines = [f"🖼 <b>Galería</b> ({len(images)} recientes)\n"]
        for i, fname in enumerate(images, 1):
            size_kb = _os.path.getsize(_os.path.join(drafts_dir, fname)) // 1024
            ts_raw = _os.path.getmtime(_os.path.join(drafts_dir, fname))
            import datetime
            ts_str = datetime.datetime.fromtimestamp(ts_raw).strftime("%d/%m %H:%M")
            lines.append(f"  <code>{i:2d}.</code> {fname[:40]} <i>({size_kb}KB · {ts_str})</i>")

        lines.append(f"\n📤 Para publicar:\n<code>/galeria pub &lt;nombre&gt;</code>")

        # Get dashboard URL
        try:
            import subprocess
            info = subprocess.run(
                ["curl", "-sf", "http://localhost:4030/api/info"],
                capture_output=True, text=True, timeout=2,
            )
            if info.returncode == 0:
                import json as _json
                data = _json.loads(info.stdout)
                url = data.get("authUrl") or data.get("tunnelUrl", "")
                if url:
                    lines.append(f"\n🖥 Dashboard: {url}")
        except Exception:
            pass

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _galeria_publish(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        drafts_dir: str,
        filename: str,
    ) -> None:
        """Publish a draft image directly to Instagram."""
        path = _os.path.join(drafts_dir, filename)
        if not _os.path.isfile(path):
            await update.message.reply_text(f"❌ Archivo no encontrado: <code>{filename}</code>", parse_mode="HTML")
            return

        caption_text = (update.message.text or "").partition("\n")[2].strip() or filename

        status = await update.message.reply_text(
            f"📤 <b>Publicando en Instagram...</b>\n{filename[:60]}", parse_mode="HTML"
        )
        try:
            from src.workflows.social_publisher import post_to_instagram
            with open(path, "rb") as f:
                img_bytes = f.read()
            from src.workflows.social_publisher import upload_image_to_host
            public_url = await upload_image_to_host(img_bytes)
            if not public_url:
                await status.edit_text("❌ No se pudo obtener URL pública para la imagen.")
                return
            result = await post_to_instagram(public_url, caption_text)
            if result.get("ok"):
                await status.edit_text(
                    f"✅ <b>Publicado en Instagram</b>\n"
                    f"🔗 <a href=\"{result.get('url', '#')}\">Ver post</a>",
                    parse_mode="HTML", disable_web_page_preview=True,
                )
            else:
                await status.edit_text(
                    f"⚠️ <b>Resultado:</b>\n{result.get('error', 'Error desconocido')[:200]}",
                    parse_mode="HTML",
                )
        except Exception as e:
            await status.edit_text(f"❌ Error: {str(e)[:200]}", parse_mode="HTML")

    # ── F1: /social status ─────────────────────────────────────────────────────

    async def _zt_social(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """📊 Social publishing status — queue, accounts, last published.

        Usage:
          /social            — full status
          /social status     — same
          /social queue      — pending scheduled posts only
        """
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _Path

        args = (update.message.text or "").split()
        subcommand = args[1].lower() if len(args) > 1 else "status"

        sched_dir = _Path.home() / ".aura" / "social_scheduled"
        drafts_dir = _Path.home() / ".aura" / "social_drafts"

        # — Scheduled queue —
        pending, published, failed = [], [], []
        if sched_dir.exists():
            now = _dt.now(_tz.utc)
            for f in sorted(sched_dir.glob("scheduled_*.json")):
                try:
                    data = _json.loads(f.read_text())
                    status = data.get("status", "pending")
                    if status == "pending":
                        pending.append(data)
                    elif status == "published":
                        published.append(data)
                    elif status == "failed":
                        failed.append(data)
                except Exception:
                    pass

        lines = ["📊 <b>Social Publishing — Estado</b>\n"]

        # Queue section
        if pending:
            lines.append(f"⏳ <b>Cola programada</b> ({len(pending)} posts):")
            now = _dt.now(_tz.utc)
            for p in pending[:5]:
                try:
                    due = _dt.fromisoformat(p["scheduled_for"].replace("Z", "+00:00"))
                    diff = due - now
                    hours = int(diff.total_seconds() // 3600)
                    mins = int((diff.total_seconds() % 3600) // 60)
                    when = f"en {hours}h {mins}m" if hours > 0 else f"en {mins}m"
                    platforms = ", ".join(p.get("platforms", ["instagram"]))
                    caption_preview = p.get("caption", p.get("description", ""))[:50]
                    lines.append(f"  • {when} → {platforms}: <i>{caption_preview}...</i>")
                except Exception:
                    lines.append(f"  • {p.get('caption', '')[:50]}...")
        else:
            lines.append("✅ <b>Cola vacía</b> — no hay posts programados")

        if subcommand == "queue":
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        # Last published
        if published:
            last = published[-1]
            pub_at = last.get("published_at", "?")[:16].replace("T", " ")
            platforms = ", ".join(last.get("platforms", ["instagram"]))
            caption_preview = last.get("caption", "")[:60]
            lines.append(f"\n📅 <b>Último publicado</b> ({pub_at} UTC):")
            lines.append(f"  {platforms}: <i>{caption_preview}...</i>")
        if failed:
            lines.append(f"\n⚠️ {len(failed)} post(s) fallidos en cola")

        # Drafts count
        if drafts_dir.exists():
            n_drafts = len(list(drafts_dir.glob("*.jpg")) + list(drafts_dir.glob("*.png")))
            if n_drafts:
                lines.append(f"\n🖼 <b>Borradores</b>: {n_drafts} imágenes (<code>/galeria</code>)")

        # Account connectivity
        lines.append("\n🔗 <b>Cuentas</b>:")
        try:
            from src.workflows.social_publisher import get_social_status
            acc = await get_social_status()
            ig = acc.get("instagram", {})
            fb = acc.get("facebook", {})
            img = acc.get("image_gen", {})
            lines.append(
                f"  {'✅' if ig.get('ready') else '❌'} Instagram"
                + (f" (@{ig.get('account_info', '')})" if ig.get("ready") else " — no conectada")
            )
            lines.append(
                f"  {'✅' if fb.get('ready') else '⚠️'} Facebook"
                + ("" if fb.get("ready") else " — FACEBOOK_PAGE_ID no configurado")
            )
            lines.append(f"  ✅ Imagen: {img.get('provider', 'FLUX.1')} ({img.get('cost', 'FREE')})")
        except Exception as e:
            lines.append(f"  ⚠️ No se pudo verificar cuentas: {str(e)[:60]}")

        lines.append(
            "\n💡 <code>/post instagram sobre &lt;tema&gt;</code> — publicar ahora\n"
            "<code>/post instagram schedule mañana 18:00 sobre &lt;tema&gt;</code> — programar"
        )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # ── F2: /post <platform> schedule <time> sobre <topic> ────────────────────

    @staticmethod
    async def _zt_design(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Genera un post HTML de diseño editorial @royaluniondesign → PNG → borradores.

        Usage:
          /design post editorial sobre tipografía
          /design carrusel 3 slides sobre identidad de marca
          /design 9:16 quote inspiracional minimalista
        """
        import aiohttp as _aiohttp

        args_text = (update.message.text or "").split(maxsplit=1)
        if len(args_text) < 2:
            await update.message.reply_text(
                "🎨 <b>/design — Diseño Editorial @royaluniondesign</b>\n\n"
                "Genera un post HTML con la marca y lo convierte a PNG.\n\n"
                "<b>Ejemplos:</b>\n"
                "  <code>/design post sobre tipografía moderna</code>\n"
                "  <code>/design carrusel 3 slides sobre identidad de marca</code>\n"
                "  <code>/design 4:5 quote minimalista sobre diseño</code>\n\n"
                "Usa el Dashboard para previsualizar antes de publicar:\n"
                "<i>Panel Social → 🎨 DISEÑO</i>",
                parse_mode="HTML",
            )
            return

        raw = args_text[1].strip()

        # Parse optional format prefix (1:1 | 4:5 | 9:16 | 16:9)
        fmt = "1:1"
        slides = 1
        brief = raw

        parts = raw.split(maxsplit=1)
        if parts[0] in ("1:1", "4:5", "9:16", "16:9"):
            fmt = parts[0]
            brief = parts[1] if len(parts) > 1 else ""

        # Parse slides count from "carrusel N" or "N slides"
        import re as _re
        m = _re.search(r"\b(\d)\s*slides?\b", brief, _re.IGNORECASE)
        if not m:
            m = _re.search(r"carrusel\s+(\d)", brief, _re.IGNORECASE)
        if m:
            slides = max(1, min(5, int(m.group(1))))

        if not brief:
            await update.message.reply_text("Falta el brief del diseño.", parse_mode="HTML")
            return

        api_port = _os.environ.get("API_SERVER_PORT", "8080")
        base_url = f"http://127.0.0.1:{api_port}"

        await update.message.reply_text(
            f"🎨 <b>Generando diseño…</b>\n\n"
            f"📐 Formato: <code>{fmt}</code> · {slides} slide(s)\n"
            f"📝 Brief: <i>{brief[:120]}</i>\n\n"
            "Usando Gemini Flash + DESIGN.md @royaluniondesign…",
            parse_mode="HTML",
        )

        # Step 1: Generate HTML
        try:
            timeout = _aiohttp.ClientTimeout(total=60)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{base_url}/api/social/design/generate",
                    json={"brief": brief, "format": fmt, "slides": slides},
                ) as resp:
                    data = await resp.json()
        except Exception as e:
            await update.message.reply_text(f"❌ Error generando diseño: {e}", parse_mode="HTML")
            return

        if not data.get("ok"):
            await update.message.reply_text(
                f"❌ <b>Error:</b> {data.get('error', 'Sin respuesta')}", parse_mode="HTML"
            )
            return

        task_id = data["task_id"] if "task_id" in data else data.get("taskId", "")
        preview_url = f"{base_url}{data.get('previewUrl', '')}"

        await update.message.reply_text(
            f"✅ <b>Diseño generado</b>\n\n"
            f"🔗 Preview: <code>{preview_url}</code>\n\n"
            "Capturando PNG…",
            parse_mode="HTML",
        )

        # Step 2: Screenshot to PNG
        try:
            timeout = _aiohttp.ClientTimeout(total=45)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{base_url}/api/social/design/screenshot",
                    json={"taskId": task_id, "format": fmt, "slide": 0},
                ) as resp:
                    shot = await resp.json()
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Diseño listo pero screenshot falló: {e}\n\nPreview manual: {preview_url}",
                parse_mode="HTML",
            )
            return

        if not shot.get("ok"):
            await update.message.reply_text(
                f"⚠️ HTML generado pero screenshot falló: {shot.get('error')}\n"
                f"Preview: {preview_url}",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            f"🎨 <b>Diseño listo</b>\n\n"
            f"📁 Borrador: <code>{shot['filename']}</code>\n"
            f"📦 Tamaño: {shot.get('size_kb', '?')}KB\n\n"
            f"Usa <code>/galeria</code> para ver · <code>/galeria pub {shot['filename']}</code> para publicar.",
            parse_mode="HTML",
        )

    def _parse_schedule_time(time_str: str):
        """Parse natural language time string → UTC datetime or None.

        Handles: 'en 2h', 'en 30m', 'en 2 horas', 'a las 18:00',
                 'mañana 18:00', 'mañana a las 18:00', 'hoy 20:00'.
        Returns aware UTC datetime or None if unparseable.
        """
        import re as _re
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        text = time_str.strip().lower()
        now = _dt.now(_tz.utc)

        # en Xh / en X horas
        m = _re.search(r'en\s+(\d+)\s*(?:h|hora[s]?)', text)
        if m:
            return now + _td(hours=int(m.group(1)))

        # en Xm / en X minutos
        m = _re.search(r'en\s+(\d+)\s*(?:m|min(?:uto[s]?)?)', text)
        if m:
            return now + _td(minutes=int(m.group(1)))

        # extract HH:MM if present
        hm = _re.search(r'(\d{1,2}):(\d{2})', text)
        if hm:
            hour, minute = int(hm.group(1)), int(hm.group(2))
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if 'mañana' in text or 'manana' in text:
                base += _td(days=1)
            candidate = base.replace(hour=hour, minute=minute)
            # if "today" time is already past, auto-advance to tomorrow
            if candidate <= now and 'mañana' not in text and 'manana' not in text:
                candidate += _td(days=1)
            return candidate

        # solo "mañana" sin hora → 10:00 next day
        if 'mañana' in text or 'manana' in text:
            return now.replace(hour=10, minute=0, second=0, microsecond=0) + _td(days=1)

        return None

    async def _zt_social_schedule(
        self,
        update: Update,
        context: "ContextTypes.DEFAULT_TYPE",
        platform: str,
        time_str: str,
        topic: str,
    ) -> None:
        """Schedule a social post for later publishing."""
        import json as _json
        import time as _time
        from pathlib import Path as _Path

        due = self._parse_schedule_time(time_str)
        if not due:
            await update.message.reply_text(
                "❌ No entendí el horario. Ejemplos:\n"
                "<code>/post instagram schedule en 2h sobre branding</code>\n"
                "<code>/post instagram schedule mañana 18:00 sobre diseño</code>\n"
                "<code>/post instagram schedule a las 20:00 sobre IA</code>",
                parse_mode="HTML",
            )
            return

        sched_dir = _Path.home() / ".aura" / "social_scheduled"
        sched_dir.mkdir(parents=True, exist_ok=True)

        ts = int(_time.time())
        filename = sched_dir / f"scheduled_{ts}.json"
        data = {
            "caption": topic,
            "description": topic,
            "platforms": [platform],
            "scheduled_for": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "pending",
            "created_at": due.strftime("%Y-%m-%dT%H:%M:%SZ").replace(
                due.strftime("%Y-%m-%dT%H:%M:%SZ"),
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        }
        filename.write_text(_json.dumps(data, ensure_ascii=False, indent=2))

        # Human-readable "when"
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        diff = due - now
        hours = int(diff.total_seconds() // 3600)
        mins = int((diff.total_seconds() % 3600) // 60)
        if hours >= 24:
            when_str = f"mañana a las {due.strftime('%H:%M')} UTC"
        elif hours > 0:
            when_str = f"en {hours}h {mins}m ({due.strftime('%H:%M')} UTC)"
        else:
            when_str = f"en {mins} minutos ({due.strftime('%H:%M')} UTC)"

        await update.message.reply_text(
            f"⏰ <b>Post programado</b>\n\n"
            f"📱 Plataforma: <b>{platform.capitalize()}</b>\n"
            f"🕐 Cuándo: <b>{when_str}</b>\n"
            f"📝 Tema: <i>{topic[:100]}</i>\n\n"
            f"AURA generará la imagen y caption antes de publicar.\n"
            f"<code>/social queue</code> para ver la cola.",
            parse_mode="HTML",
        )

    # ── /content — Content Agent control panel ──────────────────────────────

    async def _zt_content(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🧠 Content Agent — autonomous content creation pipeline.

        Usage:
          /content plan   — run brain, generate today's plans
          /content run    — execute today's plans (generate + post)
          /content status — recent content history
          /content next   — today's scheduled content
          /content feeds  — RSS feed health
        """
        from telegram import Update as _Update
        from src.workflows.content.content_command import handle_content_command

        args_text = (update.message.text or "").partition(" ")[2]  # strip "/content"

        async def _send(msg: str) -> None:
            await update.message.reply_text(msg, parse_mode="Markdown")

        await handle_content_command(args_text, _send)

"""Zero-token social media and video commands — post, video."""

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
            topic = parts[1] if len(parts) > 1 else raw_prompt
            await self._handle_social_direct(update, context, topic, ["facebook"])
        elif platform_hint in ("social", "ambas", "both", "all"):
            topic = parts[1] if len(parts) > 1 else raw_prompt
            await self._handle_social_direct(update, context, topic, ["instagram", "facebook"])
        else:
            # Legacy: instagram/twitter/linkedin via brand image pipeline
            await self._handle_social_post(update, context, raw_prompt)

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

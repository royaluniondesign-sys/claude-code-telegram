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
                "📱 <b>/post — Social Media Pipeline</b>\n\n"
                "Uso:\n"
                "  <code>/post instagram carrusel 5 sobre claude code</code>\n"
                "  <code>/post twitter hilo sobre IA y automatización</code>\n"
                "  <code>/post linkedin post sobre productividad</code>\n\n"
                "Plataformas: instagram · twitter · linkedin\n"
                "Tipos: carrusel/carousel · hilo/thread · post\n\n"
                "💡 También puedes escribir directamente:\n"
                '<i>"publica un carrusel en instagram sobre X, 5 fotos"</i>',
                parse_mode="HTML",
            )
            return

        raw_prompt = args_text[1].strip()
        # Delegate to the orchestrator's social pipeline handler
        await self._handle_social_post(update, context, raw_prompt)

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

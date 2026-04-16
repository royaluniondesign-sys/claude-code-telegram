"""Zero-token voice commands — speak, voz."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenVoiceMixin:
    """Mixin: voice/TTS zero-token commands."""

    async def _zt_speak(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎤 /speak <text> — convierte texto a voz (edge-tts, gratis)."""
        text = (update.message.text or "").replace("/speak", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Uso: /speak <texto>\nEjemplo: /speak Todo listo, jefe."
            )
            return
        try:
            from ..features.voice_tts import generate_voice, send_voice_response
            sent = await send_voice_response(update, context, text)
            if not sent:
                await update.message.reply_text("❌ TTS no disponible — instala edge-tts")
        except Exception as e:
            await update.message.reply_text(f"TTS error: {e}")

    async def _zt_voz(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎙 /voz [on|off] — toggle respuestas de voz automáticas."""
        user_id = update.effective_user.id
        arg = (update.message.text or "").split()[-1].lower()
        voice_users = context.bot_data.setdefault("voice_users", set())

        if arg == "on":
            voice_users.add(user_id)
            await update.message.reply_text(
                "🎙 Voz activada — responderé con audio además de texto.\n"
                "Usa /voz off para desactivar."
            )
        elif arg == "off":
            voice_users.discard(user_id)
            await update.message.reply_text("🔇 Voz desactivada.")
        else:
            estado = "🎙 ON" if user_id in voice_users else "🔇 OFF"
            await update.message.reply_text(
                f"Voz: {estado}\nUsa /voz on o /voz off"
            )

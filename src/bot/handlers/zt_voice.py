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
            from ..features.voice_tts import send_voice_response
            sent = await send_voice_response(update, context, text)
            if not sent:
                await update.message.reply_text(
                    "❌ TTS no disponible — instala edge-tts:\n"
                    "<code>pip install edge-tts</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            await update.message.reply_text(f"TTS error: {e}")

    async def _zt_voz(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎙 /voz [on|off] — toggle respuestas de voz automáticas (persistente)."""
        from ..features.voice_tts import save_voice_prefs

        user_id = update.effective_user.id
        # Parse arg — handle "/voz on", "/voz off", or bare "/voz"
        parts = (update.message.text or "").split()
        arg = parts[-1].lower() if len(parts) > 1 else ""

        voice_users: set[int] = context.bot_data.setdefault("voice_users", set())

        if arg in ("on", "activar", "enable", "1", "si", "sí"):
            voice_users.add(user_id)
            save_voice_prefs(voice_users)
            await update.message.reply_text(
                "🎙 Voz activada — responderé con audio además de texto.\n"
                "Usa /voz off para desactivar."
            )
        elif arg in ("off", "desactivar", "disable", "0", "no"):
            voice_users.discard(user_id)
            save_voice_prefs(voice_users)
            await update.message.reply_text("🔇 Voz desactivada.")
        else:
            estado = "🎙 ON" if user_id in voice_users else "🔇 OFF"
            await update.message.reply_text(
                f"Voz: {estado}\nUsa /voz on o /voz off"
            )

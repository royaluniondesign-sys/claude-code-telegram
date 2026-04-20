"""Text-to-speech output for AURA — edge-tts via tts_engine (free, no API key).

Architecture:
  generate_voice(text)          → OGG OPUS bytes (via src.voice.tts_engine)
  send_voice_response(update, …)→ sends as Telegram voice message

Language detection: auto-selects Spanish or English voice based on text.
Voices used (Microsoft Azure Neural TTS, free via Edge sync):
  Spanish → es-ES-ElviraNeural  (female, warm, Spain)
  English → en-US-AriaNeural   (female, clear, US)
  Sarcastic override → es-ES-AlvaroNeural (male, dry)
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# ── Voice personality map ─────────────────────────────────────────────────────

PERSONALITY_VOICES: dict[str, str] = {
    "sarcastic": "es-ES-AlvaroNeural",
    "neutral":   "es-ES-ElviraNeural",
    "default":   "es-ES-ElviraNeural",
    "en":        "en-US-AriaNeural",
}

# ── Persistence ───────────────────────────────────────────────────────────────

_VOICE_PREFS_FILE = Path.home() / ".aura" / "voice_users.txt"


def load_voice_prefs() -> set[int]:
    """Load persisted voice-on user IDs from disk."""
    try:
        if _VOICE_PREFS_FILE.exists():
            return {
                int(line.strip())
                for line in _VOICE_PREFS_FILE.read_text().splitlines()
                if line.strip().isdigit()
            }
    except Exception as exc:
        logger.warning("voice_prefs_load_error", error=str(exc))
    return set()


def save_voice_prefs(voice_users: set[int]) -> None:
    """Persist voice-on user IDs to disk."""
    try:
        _VOICE_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VOICE_PREFS_FILE.write_text(
            "\n".join(str(uid) for uid in sorted(voice_users)) + "\n"
        )
    except Exception as exc:
        logger.warning("voice_prefs_save_error", error=str(exc))


# ── Language detection ────────────────────────────────────────────────────────

_SPANISH_WORDS = {"el", "la", "los", "las", "que", "de", "en", "por", "con", "para",
                  "es", "un", "una", "no", "se", "su", "al", "del", "lo", "más"}
_ENGLISH_WORDS = {"the", "is", "are", "was", "and", "for", "that", "with", "this",
                  "it", "to", "you", "your", "have", "not", "but", "from", "they"}


def _detect_language(text: str) -> str:
    """Heuristic: count Spanish vs English function words in first 20 tokens."""
    words = set(text.lower().split()[:20])
    es_score = len(words & _SPANISH_WORDS)
    en_score = len(words & _ENGLISH_WORDS)
    return "en" if en_score > es_score else "es"


def _select_voice(text: str, voice_override: Optional[str] = None) -> str:
    """Pick the best voice for the text (or use the explicit override)."""
    if voice_override:
        return voice_override
    lang = _detect_language(text)
    return PERSONALITY_VOICES.get(lang, PERSONALITY_VOICES["default"])


# ── Core TTS ──────────────────────────────────────────────────────────────────

async def generate_voice(
    text: str,
    voice: Optional[str] = None,
) -> bytes:
    """Generate OGG OPUS bytes from text.

    Delegates to src.voice.tts_engine.text_to_ogg which handles:
      - Markdown cleaning
      - edge-tts MP3 generation
      - ffmpeg MP3 → OGG OPUS conversion

    Args:
        text:  Plain or markdown text to speak.
        voice: Edge TTS voice name override. Auto-detected if None.

    Returns:
        OGG OPUS bytes ready to send as Telegram voice message.

    Raises:
        RuntimeError: If TTS or ffmpeg fails.
        ImportError:  If edge-tts is not installed.
    """
    from src.voice.tts_engine import text_to_ogg  # noqa: PLC0415

    selected = _select_voice(text, voice)
    logger.debug("voice_tts_generating", voice=selected, text_len=len(text))
    return await text_to_ogg(text, voice=selected)


async def send_voice_response(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
    text: str,
    voice: Optional[str] = None,
) -> bool:
    """Generate and send a voice message to Telegram.

    Args:
        update:  Telegram update object.
        context: Telegram context (unused but kept for API consistency).
        text:    Response text to speak.
        voice:   Optional voice override.

    Returns:
        True if the voice message was sent successfully, False otherwise.
    """
    message = update.effective_message
    if message is None:
        return False

    try:
        ogg_bytes = await generate_voice(text, voice=voice)
    except ImportError as exc:
        logger.warning("voice_tts_not_installed", error=str(exc))
        return False
    except Exception as exc:
        logger.error("voice_tts_generate_failed", error=str(exc))
        return False

    try:
        await message.reply_voice(voice=ogg_bytes)
        logger.info("voice_tts_sent", bytes=len(ogg_bytes))
        return True
    except Exception as exc:
        logger.error("voice_tts_send_failed", error=str(exc))
        # Last-resort fallback: send as audio document (MP3 instead of OGG is fine here)
        try:
            await message.reply_document(
                document=ogg_bytes,
                filename="aura_voice.ogg",
                caption="🔊 (fallback audio)",
            )
            return True
        except Exception as exc2:
            logger.error("voice_tts_fallback_failed", error=str(exc2))
            return False


# ── /voz command helper ───────────────────────────────────────────────────────

async def handle_voz_command(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """/voz [on|off] — toggle voice responses for this user.

    Preference is persisted to ~/.aura/voice_users.txt so it survives restarts.
    """
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args_text = (message.text or "").replace("/voz", "").strip().lower()
    voice_users: set[int] = context.bot_data.setdefault("voice_users", set())

    if args_text in ("on", "activar", "enable", "1", "si", "sí"):
        voice_users.add(user.id)
        save_voice_prefs(voice_users)
        await message.reply_text(
            "🔊 Voz activada. Recibirás respuestas en texto y audio.\n"
            "Desactívala con /voz off"
        )
    elif args_text in ("off", "desactivar", "disable", "0", "no"):
        voice_users.discard(user.id)
        save_voice_prefs(voice_users)
        await message.reply_text("🔇 Voz desactivada.")
    else:
        is_on = user.id in voice_users
        status = "🔊 activada" if is_on else "🔇 desactivada"
        await message.reply_text(
            f"Voz {status}.\n\n"
            "/voz on — activar respuestas de voz\n"
            "/voz off — desactivar"
        )

"""Text-to-speech using Microsoft Edge TTS (free, no API key).

Best Spanish voices:
- es-ES-AlvaroNeural (male, Spain accent, good for sarcasm)
- es-ES-ElviraNeural (female, Spain accent)
- es-MX-JorgeNeural (male, Mexico)

Usage: generate_voice(text, voice="es-ES-AlvaroNeural") -> bytes (MP3)

Requires: pip install edge-tts
If not installed, functions raise ImportError with instructions.
"""

import io
import re
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# ── Voice personality map ─────────────────────────────────────────────────────

PERSONALITY_VOICES: dict[str, str] = {
    "sarcastic": "es-ES-AlvaroNeural",   # Alvaro sounds drier
    "neutral": "es-ES-ElviraNeural",
    "default": "es-ES-AlvaroNeural",
}

_MAX_CHARS = 500
_MARKDOWN_STRIP_RE = re.compile(
    r"\*\*(.+?)\*\*"            # bold
    r"|\*(.+?)\*"               # italic *
    r"|__(.+?)__"               # underline
    r"|_(.+?)_"                 # italic _
    r"|`{1,3}[^`]*`{1,3}"      # inline code / code blocks
    r"|#{1,6}\s+"               # headings
    r"|>\s+"                    # blockquotes
    r"|\[([^\]]+)\]\([^)]+\)"  # markdown links → keep label
    r"|\n{3,}",                 # excess newlines
    re.DOTALL,
)


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting so TTS reads it naturally."""

    def _replacer(m: re.Match) -> str:
        # Return the first non-None capture group (visible text)
        for g in m.groups():
            if g is not None:
                return g
        return " "

    cleaned = _MARKDOWN_STRIP_RE.sub(_replacer, text)
    # Collapse multiple spaces / newlines
    cleaned = re.sub(r"\n+", ". ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate to max_chars at a sentence boundary when possible."""
    if len(text) <= max_chars:
        return text

    # Try to find sentence boundary before limit
    chunk = text[:max_chars]
    for sep in (". ", "! ", "? ", ".\n", ":\n"):
        idx = chunk.rfind(sep)
        if idx > max_chars // 2:
            return chunk[: idx + 1].rstrip()

    # No good boundary — hard cut at word boundary
    last_space = chunk.rfind(" ")
    if last_space > 0:
        return chunk[:last_space] + "…"
    return chunk + "…"


async def generate_voice(
    text: str,
    voice: str = PERSONALITY_VOICES["default"],
) -> bytes:
    """Generate MP3 bytes from text using edge-tts.

    Args:
        text: Plain or markdown text to speak.
        voice: Edge TTS voice name (e.g. "es-ES-AlvaroNeural").

    Returns:
        Raw MP3 bytes.

    Raises:
        ImportError: If edge-tts is not installed.
        RuntimeError: If TTS generation fails.
    """
    try:
        import edge_tts
    except ImportError as exc:
        raise ImportError(
            "edge-tts is not installed. Run: pip install edge-tts"
        ) from exc

    clean = _strip_markdown(text)
    clean = _truncate_at_sentence(clean, _MAX_CHARS)

    if not clean:
        raise ValueError("Text is empty after cleaning.")

    communicate = edge_tts.Communicate(clean, voice)

    # Collect audio chunks into bytes buffer
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])

    audio = buf.getvalue()
    if not audio:
        raise RuntimeError("edge-tts returned empty audio.")

    logger.info(
        "voice_tts_generated",
        voice=voice,
        text_length=len(clean),
        audio_bytes=len(audio),
    )
    return audio


async def send_voice_response(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
    text: str,
    voice: Optional[str] = None,
) -> bool:
    """Generate and send a voice message to Telegram.

    Telegram prefers OGG Opus for voice messages. edge-tts produces MP3.
    We try to convert with pydub if available; otherwise send as audio file.

    Args:
        update: Telegram update object.
        context: Telegram context (unused but kept for signature consistency).
        text: Response text to speak.
        voice: Optional voice override. Defaults to PERSONALITY_VOICES["default"].

    Returns:
        True if the voice message was sent successfully, False otherwise.
    """
    selected_voice = voice or PERSONALITY_VOICES["default"]

    try:
        mp3_bytes = await generate_voice(text, voice=selected_voice)
    except ImportError as exc:
        logger.warning("voice_tts_not_installed", error=str(exc))
        return False
    except Exception as exc:
        logger.error("voice_tts_generate_failed", error=str(exc))
        return False

    message = update.effective_message
    if message is None:
        return False

    # Attempt OGG Opus conversion via pydub (optional dependency)
    try:
        from pydub import AudioSegment  # type: ignore[import]

        mp3_buf = io.BytesIO(mp3_bytes)
        segment = AudioSegment.from_mp3(mp3_buf)
        ogg_buf = io.BytesIO()
        segment.export(ogg_buf, format="ogg", codec="libopus")
        audio_data = ogg_buf.getvalue()
        send_as_voice = True
        logger.debug("voice_tts_converted_to_ogg")
    except Exception:
        # pydub not available or conversion failed — send MP3 as voice anyway
        # Telegram accepts MP3 as voice in most clients
        audio_data = mp3_bytes
        send_as_voice = True

    try:
        if send_as_voice:
            await message.reply_voice(voice=audio_data)
        else:
            await message.reply_audio(audio=audio_data, title="AURA Voice")
        logger.info("voice_tts_sent", bytes=len(audio_data))
        return True
    except Exception as exc:
        logger.error("voice_tts_send_failed", error=str(exc))
        # Fallback: try sending as audio document
        try:
            await message.reply_audio(audio=mp3_bytes, title="AURA Voice")
            return True
        except Exception as exc2:
            logger.error("voice_tts_audio_fallback_failed", error=str(exc2))
            return False


async def handle_voz_command(
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """/voz [on|off] — toggle voice responses for this user.

    When voice is ON, AURA sends both text and voice for every response.
    """
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args_text = (message.text or "").replace("/voz", "").strip().lower()
    voice_users: set[int] = context.bot_data.setdefault("voice_users", set())

    if args_text in ("on", "activar", "enable", "1", "si", "sí"):
        voice_users.add(user.id)
        await message.reply_text(
            "🔊 Voz activada. Recibirás respuestas en texto y audio.\n"
            "Desactívala con /voz off"
        )
    elif args_text in ("off", "desactivar", "disable", "0", "no"):
        voice_users.discard(user.id)
        await message.reply_text("🔇 Voz desactivada.")
    else:
        # Show current status
        is_on = user.id in voice_users
        status = "🔊 activada" if is_on else "🔇 desactivada"
        await message.reply_text(
            f"Voz {status}.\n\n"
            "/voz on — activar respuestas de voz\n"
            "/voz off — desactivar"
        )

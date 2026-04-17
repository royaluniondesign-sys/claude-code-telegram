"""TTS Engine — edge-tts + ffmpeg → OGG OPUS for Telegram voice messages.

Voice: es-ES-ElviraNeural (Microsoft Azure Neural TTS, free via Edge sync).
Quality: natural, warm, Spanish Spain — locutora de radio.
No API key required. Uses Microsoft's free neural TTS service.

Output: OGG OPUS (Telegram voice message format).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Default voice — Spanish Spain, female, warm and natural
DEFAULT_VOICE = "es-ES-ElviraNeural"

# Available AURA voices (Spanish Spain, female neural)
VOICES = {
    "elvira":  "es-ES-ElviraNeural",    # warm, professional — DEFAULT
    "abril":   "es-ES-AbrilNeural",     # energetic, friendly
    "ximena":  "es-ES-XimenaNeural",    # conversational, clear
    "triana":  "es-ES-TrianaNeural",    # expressive
    "alvaro":  "es-ES-AlvaroNeural",    # male, deep
}


async def text_to_ogg(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = "+5%",
    pitch: str = "+0Hz",
) -> bytes:
    """Convert text to OGG OPUS bytes (Telegram voice format).

    Args:
        text:  Text to speak (max ~4000 chars — split longer texts upstream)
        voice: edge-tts voice name (default: es-ES-ElviraNeural)
        rate:  Speech rate adjustment (+5% = slightly faster, natural)
        pitch: Pitch adjustment (0Hz = natural)

    Returns:
        OGG OPUS bytes ready to send as Telegram voice_note.

    Raises:
        RuntimeError: if edge-tts or ffmpeg fails.
    """
    try:
        import edge_tts
    except ImportError as e:
        raise RuntimeError(
            "edge-tts not installed. Run: pip install edge-tts"
        ) from e

    # Sanitize text: remove markdown symbols that sound bad when spoken
    clean = _clean_for_speech(text)
    if not clean.strip():
        raise ValueError("Empty text after cleaning — nothing to speak.")

    with tempfile.TemporaryDirectory() as tmp:
        mp3_path = os.path.join(tmp, "speech.mp3")
        ogg_path = os.path.join(tmp, "speech.ogg")

        # Step 1: edge-tts → MP3
        communicate = edge_tts.Communicate(clean, voice, rate=rate, pitch=pitch)
        await communicate.save(mp3_path)

        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            raise RuntimeError("edge-tts produced empty audio file.")

        # Step 2: ffmpeg MP3 → OGG OPUS (Telegram voice format)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", mp3_path,
            "-c:a", "libopus",
            "-b:a", "32k",      # 32kbps — good quality, small size
            "-ar", "48000",     # 48kHz (Telegram requirement for voice)
            "-ac", "1",         # mono
            ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed (rc={proc.returncode})")

        ogg_bytes = Path(ogg_path).read_bytes()
        logger.info(
            "tts_ok",
            voice=voice,
            text_len=len(clean),
            ogg_kb=round(len(ogg_bytes) / 1024, 1),
        )
        return ogg_bytes


def _clean_for_speech(text: str) -> str:
    """Remove markdown/code/symbols that sound bad when spoken aloud."""
    import re

    # Remove code blocks entirely (can't speak code naturally)
    text = re.sub(r"```[\s\S]*?```", "[código]", text)
    text = re.sub(r"`[^`]+`", "", text)

    # Remove URLs
    text = re.sub(r"https?://\S+", "[enlace]", text)

    # Remove markdown formatting symbols
    text = re.sub(r"[*_~|>#\[\]()]", "", text)

    # Remove emoji (leave text)
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text, flags=re.UNICODE)

    # Collapse multiple newlines/spaces
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    # Trim to reasonable TTS length (edge-tts handles ~5000 chars max)
    if len(text) > 4000:
        text = text[:3900] + "... [mensaje recortado]"

    return text.strip()


async def list_voices(lang_prefix: str = "es-ES") -> list[dict]:
    """List available voices for a language prefix."""
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        return [v for v in voices if v["ShortName"].startswith(lang_prefix)]
    except Exception as e:
        logger.warning("tts_list_voices_error", error=str(e))
        return []

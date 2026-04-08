"""Text-to-Speech using edge-tts (Microsoft Edge, free, no API key).

Generates natural-sounding speech in 300+ voices.
Default: es-MX-DaliaNeural (Spanish) / en-US-AriaNeural (English)
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# Voice mapping by detected language
_VOICES = {
    "es": "es-MX-DaliaNeural",
    "en": "en-US-AriaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
}

_DEFAULT_VOICE = "es-MX-DaliaNeural"

# Max text length for TTS (avoid huge audio files)
_MAX_TEXT_LENGTH = 2000


def _detect_language(text: str) -> str:
    """Simple heuristic to detect language from text."""
    spanish_markers = {"el", "la", "los", "las", "que", "de", "en", "por", "con", "para"}
    english_markers = {"the", "is", "are", "was", "and", "for", "that", "with", "this"}

    words = set(text.lower().split()[:20])
    es_count = len(words & spanish_markers)
    en_count = len(words & english_markers)

    if es_count > en_count:
        return "es"
    if en_count > es_count:
        return "en"
    return "es"  # Default to Spanish


async def text_to_speech(
    text: str,
    voice: Optional[str] = None,
    language: Optional[str] = None,
) -> bytes:
    """Convert text to speech, return MP3 bytes.

    Args:
        text: Text to speak.
        voice: Specific voice name (e.g., 'es-MX-DaliaNeural').
        language: Language code to select voice automatically.

    Returns:
        MP3 audio bytes.
    """
    import edge_tts

    # Truncate long text
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH] + "..."

    # Select voice
    if not voice:
        lang = language or _detect_language(text)
        voice = _VOICES.get(lang, _DEFAULT_VOICE)

    # Generate to temp file
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(tmp_path)

        audio_bytes = Path(tmp_path).read_bytes()

        logger.info(
            "tts_generated",
            voice=voice,
            text_length=len(text),
            audio_size_kb=round(len(audio_bytes) / 1024, 1),
        )

        return audio_bytes
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def list_voices(language: Optional[str] = None) -> list:
    """List available voices, optionally filtered by language."""
    import edge_tts

    voices = await edge_tts.list_voices()
    if language:
        voices = [v for v in voices if v["Locale"].startswith(language)]
    return voices

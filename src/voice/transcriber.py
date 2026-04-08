"""Local voice transcription using faster-whisper (no API key needed).

Runs Whisper model locally on Mac M4. First run downloads the model (~150MB).
Supports: tiny, base, small, medium, large-v3
Default: 'base' — fast, good accuracy, ~150MB RAM.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# Singleton model instance (loaded once, reused)
_model = None
_model_size = "base"


def _get_model():
    """Load Whisper model (lazy, cached)."""
    global _model
    if _model is not None:
        return _model

    try:
        from faster_whisper import WhisperModel

        logger.info("Loading Whisper model", size=_model_size)
        _model = WhisperModel(
            _model_size,
            device="cpu",
            compute_type="int8",
        )
        logger.info("Whisper model loaded", size=_model_size)
        return _model
    except ImportError:
        logger.error("faster-whisper not installed")
        raise RuntimeError(
            "faster-whisper not installed. Run: uv add faster-whisper"
        )


async def transcribe_audio(
    audio_bytes: bytes,
    language: Optional[str] = None,
) -> str:
    """Transcribe audio bytes to text using local Whisper.

    Args:
        audio_bytes: Raw audio data (OGG, MP3, WAV, etc.)
        language: Optional language hint (e.g., 'es', 'en')

    Returns:
        Transcribed text.
    """
    # Write to temp file (faster-whisper needs a file path)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        # Run transcription in executor (CPU-bound)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None, _transcribe_sync, tmp_path, language
        )
        return text
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _transcribe_sync(
    file_path: str,
    language: Optional[str] = None,
) -> str:
    """Synchronous transcription (runs in thread pool)."""
    model = _get_model()

    kwargs = {}
    if language:
        kwargs["language"] = language

    segments, info = model.transcribe(
        file_path,
        beam_size=5,
        vad_filter=True,
        **kwargs,
    )

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

    result = " ".join(text_parts).strip()

    logger.info(
        "transcription_complete",
        language=info.language,
        confidence=round(info.language_probability, 2),
        length=len(result),
    )

    return result

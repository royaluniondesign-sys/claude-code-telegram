"""TTS Engine — XTTS v2 (Claribel Dervla) → OGG OPUS para Telegram.

Motor principal: Coqui XTTS v2 via subprocess con Python 3.11.
Voz fija: Claribel Dervla — la mejor voz femenina en español del modelo.
Fallback: edge-tts si XTTS no está disponible.

XTTS corre en /tmp/tts_env (Python 3.11) porque el bot usa Python 3.13
y Coqui TTS solo soporta hasta 3.11.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Voz XTTS v2 fija
XTTS_SPEAKER = "Claribel Dervla"
XTTS_LANG    = "es"

# Paths
_WORKER     = Path(__file__).parent / "xtts_worker.py"
_PYTHON311  = Path("/tmp/tts_env/bin/python3.11")
_FFMPEG     = "ffmpeg"

# Límite de texto (~4000 chars — XTTS maneja bien hasta aquí)
_MAX_CHARS  = 4000


# ── Text cleaning ─────────────────────────────────────────────────────────────

def _clean_for_speech(text: str) -> str:
    """Elimina markdown/código/URLs que suenan mal en TTS."""
    text = re.sub(r"```[\s\S]*?```", "código", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"https?://\S+", "enlace", text)
    text = re.sub(r"[*_~|>#\[\]()]", "", text)
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text, flags=re.UNICODE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS - 30] + "... mensaje recortado"
    return text.strip()


# ── XTTS v2 ───────────────────────────────────────────────────────────────────

async def _xtts_to_wav(text: str, wav_path: str) -> bool:
    """Llama al worker XTTS v2 en Python 3.11 via subprocess.

    Returns True si generó correctamente.
    """
    if not _PYTHON311.exists():
        logger.warning("xtts_python311_not_found", path=str(_PYTHON311))
        return False
    if not _WORKER.exists():
        logger.warning("xtts_worker_not_found", path=str(_WORKER))
        return False

    proc = await asyncio.create_subprocess_exec(
        str(_PYTHON311), str(_WORKER), wav_path, "--stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(
            proc.communicate(input=text.encode()),
            timeout=120,  # XTTS tarda ~15-20s en M4
        )
    except asyncio.TimeoutError:
        proc.kill()
        logger.error("xtts_timeout")
        return False

    ok = proc.returncode == 0 and Path(wav_path).exists() and Path(wav_path).stat().st_size > 0
    if not ok:
        logger.error("xtts_failed", returncode=proc.returncode)
    return ok


async def _wav_to_ogg(wav_path: str, ogg_path: str) -> bool:
    """Convierte WAV → OGG OPUS con ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        _FFMPEG, "-y", "-i", wav_path,
        "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "1",
        ogg_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.wait(), timeout=30)
    return proc.returncode == 0


# ── edge-tts fallback ─────────────────────────────────────────────────────────

async def _edgetts_to_ogg(text: str, ogg_path: str) -> bool:
    """Fallback a edge-tts si XTTS no está disponible."""
    try:
        import edge_tts  # noqa: PLC0415
    except ImportError:
        return False

    mp3_path = ogg_path.replace(".ogg", ".mp3")
    comm = edge_tts.Communicate(text, "es-ES-ElviraNeural", rate="+5%")
    await comm.save(mp3_path)
    if not Path(mp3_path).exists():
        return False
    return await _wav_to_ogg(mp3_path, ogg_path)


# ── Public API ────────────────────────────────────────────────────────────────

async def text_to_ogg(
    text: str,
    voice: str = XTTS_SPEAKER,  # ignorado — siempre Claribel hasta nueva orden
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> bytes:
    """Convierte texto a OGG OPUS. Usa XTTS v2, fallback a edge-tts.

    Returns:
        OGG OPUS bytes listos para reply_voice en Telegram.

    Raises:
        RuntimeError: si ambos motores fallan.
    """
    clean = _clean_for_speech(text)
    if not clean:
        raise ValueError("Texto vacío tras limpieza.")

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "speech.wav")
        ogg_path = os.path.join(tmp, "speech.ogg")

        # Intento 1: XTTS v2
        if await _xtts_to_wav(clean, wav_path):
            if await _wav_to_ogg(wav_path, ogg_path):
                data = Path(ogg_path).read_bytes()
                logger.info("tts_xtts_ok", speaker=XTTS_SPEAKER,
                            text_len=len(clean), ogg_kb=round(len(data) / 1024, 1))
                return data

        # Intento 2: edge-tts
        logger.warning("tts_xtts_unavailable_falling_back_to_edgetts")
        if await _edgetts_to_ogg(clean, ogg_path):
            data = Path(ogg_path).read_bytes()
            logger.info("tts_edgetts_fallback_ok", text_len=len(clean))
            return data

        raise RuntimeError("Todos los motores TTS fallaron (XTTS v2 + edge-tts).")

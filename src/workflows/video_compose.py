"""Structured video composition via json2video API.

Converts natural language → JSON2Video schema → rendered MP4.

Flow:
  user: "crea un video de 5 slides sobre claude code"
  → parse_video_request() extracts: topic, slides count, style, voice
  → generate_video_script() uses GeminiBrain to write slide content
  → build_json2video_payload() creates the API JSON
  → submit_and_poll() calls json2video API, polls for completion
  → returns video URL

Free tier: 600 credits (~10 videos × 60 sec)
API key: JSON2VIDEO_API_KEY env var

If no API key is set, returns a mock/preview description instead of failing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import structlog

logger = structlog.get_logger()

# json2video API
_API_BASE = "https://api.json2video.com/v2"
_POLL_INTERVAL = 5      # seconds
_MAX_POLL_TIME = 300    # 5 minutes

# Default dark tech style
_DEFAULT_BG = "#0f172a"
_DEFAULT_TEXT = "#e2e8f0"
_DEFAULT_FONT = "Roboto"
_SPANISH_VOICE = "es-ES-AlvaroNeural"
_ENGLISH_VOICE = "en-US-GuyNeural"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_video_request(prompt: str) -> Dict[str, Any]:
    """Extract topic, slides count, style, language, and voice_enabled from prompt.

    Returns a dict with keys:
      topic: str
      slides: int (3-10, default 5)
      style: str ("tech" | "minimal" | "vibrant")
      language: str ("es" | "en")
      voice_enabled: bool
    """
    text = prompt.strip()

    # Slides count
    slides = 5
    count_match = re.search(
        r"(?i)\b(\d+)\s*(?:slides?|diapositivas?|pantallas?|partes?)\b"
        r"|\b(?:slides?|diapositivas?)\s*[:\-]?\s*(\d+)\b",
        text,
    )
    if count_match:
        raw = count_match.group(1) or count_match.group(2)
        slides = max(2, min(10, int(raw)))

    # Topic — strip meta-words to get the core subject
    topic = re.sub(
        r"(?i)\b(?:crea?|genera?|haz?|make|create|generate|build)\b", "", text
    )
    topic = re.sub(
        r"(?i)\b(?:un|una|el|la|los|las|a|an|the)\b", "", topic
    )
    topic = re.sub(
        r"(?i)\b(?:video|animado?|presentaci[oó]n|explainer|slides?|diapositivas?)\b", "", topic
    )
    topic = re.sub(
        r"(?i)\bde\s+(?:\d+\s+)?(?:slides?|diapositivas?|pantallas?)\b", "", topic
    )
    topic = re.sub(
        r"(?i)\b(?:sobre|acerca\s+de|about|on|of)\b", "", topic
    )
    topic = re.sub(r"\s{2,}", " ", topic).strip(" ,.-")
    if not topic:
        topic = text[:80]

    # Language detection
    spanish_words = re.search(r"(?i)\b(sobre|crea|haz|genera|el|la|un|una|de|para)\b", text)
    language = "es" if spanish_words else "en"

    # Style
    style = "tech"
    if re.search(r"(?i)\b(minimal|clean|simple|elegante)\b", text):
        style = "minimal"
    elif re.search(r"(?i)\b(colorido|vibrant|vivid|colorful|bright)\b", text):
        style = "vibrant"

    # Voice
    voice_enabled = bool(re.search(r"(?i)\b(voz|voice|narr|speak|hab[la])\b", text))

    return {
        "topic": topic,
        "slides": slides,
        "style": style,
        "language": language,
        "voice_enabled": voice_enabled,
    }


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

async def generate_video_script(
    topic: str,
    slides: int,
    style: str,
    language: str = "es",
) -> List[Dict[str, str]]:
    """Generate slide content using GeminiBrain.

    Returns list of dicts: [{title, body, bg_color, text_color}, ...]
    Falls back to template if brain unavailable.
    """
    style_configs: Dict[str, Dict[str, str]] = {
        "tech": {"bg": "#0f172a", "text": "#e2e8f0", "accent": "#38bdf8"},
        "minimal": {"bg": "#ffffff", "text": "#1e293b", "accent": "#6366f1"},
        "vibrant": {"bg": "#1e1b4b", "text": "#f8fafc", "accent": "#f59e0b"},
    }
    colors = style_configs.get(style, style_configs["tech"])

    lang_instruction = (
        "Responde en español." if language == "es"
        else "Respond in English."
    )

    script_prompt = (
        f"Eres un diseñador de presentaciones. {lang_instruction}\n\n"
        f"Crea {slides} slides para un video sobre: \"{topic}\"\n\n"
        f"Responde SOLO con JSON válido, sin markdown:\n"
        f'[{{"title": "Título corto", "body": "Texto de 1-2 líneas conciso"}}]\n\n'
        f"Reglas:\n"
        f"- title: máx 6 palabras\n"
        f"- body: máx 25 palabras, impactante\n"
        f"- El primer slide es el título/introducción\n"
        f"- El último slide es llamada a acción o conclusión\n"
        f"- Devuelve exactamente {slides} objetos en el array"
    )

    raw_slides: List[Dict[str, str]] = []
    try:
        from src.brains.gemini_brain import GeminiBrain
        brain = GeminiBrain(timeout=25)
        response = await brain.execute(prompt=script_prompt)
        if not response.is_error:
            json_match = re.search(r"\[[\s\S]*\]", response.content)
            if json_match:
                parsed = json.loads(json_match.group())
                if isinstance(parsed, list):
                    raw_slides = parsed[:slides]
    except Exception as exc:
        logger.warning("video_script_brain_error", error=str(exc))

    # Fallback: generate simple template slides
    if not raw_slides:
        raw_slides = _template_slides(topic, slides, language)

    # Attach colors to each slide
    result: List[Dict[str, str]] = []
    for i, slide in enumerate(raw_slides):
        result.append({
            "title": str(slide.get("title", f"Slide {i + 1}")),
            "body": str(slide.get("body", topic)),
            "bg_color": colors["bg"],
            "text_color": colors["text"],
            "accent_color": colors["accent"],
        })

    return result


def _template_slides(
    topic: str,
    count: int,
    language: str,
) -> List[Dict[str, str]]:
    """Minimal fallback when brain is unavailable."""
    if language == "es":
        intro = {"title": topic, "body": "Una guía completa"}
        outro = {"title": "Conclusión", "body": "Empieza hoy mismo"}
    else:
        intro = {"title": topic, "body": "A complete guide"}
        outro = {"title": "Conclusion", "body": "Start today"}

    middle_count = max(0, count - 2)
    middles = [
        {"title": f"Punto {i + 1}" if language == "es" else f"Point {i + 1}", "body": topic}
        for i in range(middle_count)
    ]
    slides = [intro] + middles + [outro]
    return slides[:count]


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_json2video_payload(
    slides: List[Dict[str, str]],
    voice_enabled: bool,
    language: str,
) -> Dict[str, Any]:
    """Build valid json2video JSON payload.

    Each slide = one scene with text + optional Azure TTS voice element.
    """
    voice_id = _SPANISH_VOICE if language == "es" else _ENGLISH_VOICE

    scenes: List[Dict[str, Any]] = []
    for i, slide in enumerate(slides):
        elements: List[Dict[str, Any]] = [
            # Background color fill
            {
                "type": "html",
                "html": (
                    f"<div style=\""
                    f"width:1920px;height:1080px;"
                    f"background:{slide['bg_color']};"
                    f"display:flex;flex-direction:column;"
                    f"justify-content:center;align-items:center;"
                    f"padding:80px;box-sizing:border-box;"
                    f"font-family:'{_DEFAULT_FONT}',sans-serif;"
                    f"\">"
                    f"<h1 style=\""
                    f"color:{slide['accent_color']};"
                    f"font-size:72px;font-weight:700;"
                    f"margin:0 0 32px 0;text-align:center;"
                    f"line-height:1.2;"
                    f"\">{slide['title']}</h1>"
                    f"<p style=\""
                    f"color:{slide['text_color']};"
                    f"font-size:40px;font-weight:400;"
                    f"margin:0;text-align:center;"
                    f"line-height:1.5;max-width:1400px;"
                    f"\">{slide['body']}</p>"
                    f"</div>"
                ),
                "width": 1920,
                "height": 1080,
                "x": 0,
                "y": 0,
                "duration": 4,
            },
        ]

        if voice_enabled:
            voice_text = f"{slide['title']}. {slide['body']}"
            elements.append({
                "type": "voice",
                "voice": voice_id,
                "text": voice_text,
                "provider": "microsoft",
                "duration": 4,
            })

        scene: Dict[str, Any] = {
            "comment": f"Slide {i + 1}: {slide['title'][:30]}",
            "elements": elements,
            "duration": 4,
            "transition": {"style": "fade", "duration": 0.5},
        }
        scenes.append(scene)

    return {
        "resolution": "full-hd",
        "quality": 80,
        "scenes": scenes,
    }


# ---------------------------------------------------------------------------
# API submission & polling
# ---------------------------------------------------------------------------

async def submit_and_poll(
    payload: Dict[str, Any],
    api_key: str,
    progress_cb: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """POST to json2video API, poll until complete.

    Returns {url, duration, status}.
    Raises RuntimeError on failure.
    """
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # Submit
        async with session.post(
            f"{_API_BASE}/movies",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        movie_id = data.get("movie")
        if not movie_id:
            raise ValueError(f"json2video: no movie id in response: {data}")

        logger.info("json2video_submitted", movie_id=movie_id)

        if progress_cb:
            try:
                await progress_cb(f"🎬 Renderizando... (ID: {movie_id[:8]})")
            except Exception:
                pass

        # Poll
        poll_url = f"{_API_BASE}/movies/{movie_id}"
        deadline = time.time() + _MAX_POLL_TIME
        elapsed_polls = 0

        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            elapsed_polls += 1

            async with session.get(
                poll_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                status_data = await resp.json()

            status = status_data.get("status", "")

            if progress_cb and elapsed_polls % 3 == 0:
                mins = (elapsed_polls * _POLL_INTERVAL) // 60
                secs = (elapsed_polls * _POLL_INTERVAL) % 60
                try:
                    await progress_cb(
                        f"🎬 Procesando video... ({mins}:{secs:02d} / {status})"
                    )
                except Exception:
                    pass

            if status == "done":
                video_url = status_data.get("url", "")
                duration = status_data.get("duration", 0)
                if not video_url:
                    raise ValueError(f"json2video: done but no URL: {status_data}")
                logger.info("json2video_done", movie_id=movie_id, url=video_url[:80])
                return {"url": video_url, "duration": duration, "status": "done"}

            if status in ("error", "failed"):
                error_msg = status_data.get("error", "unknown error")
                raise RuntimeError(f"json2video render failed: {error_msg}")

        raise asyncio.TimeoutError(
            f"json2video: render did not complete in {_MAX_POLL_TIME}s"
        )


# ---------------------------------------------------------------------------
# Mock preview (no API key)
# ---------------------------------------------------------------------------

def _generate_mock_preview(
    request: Dict[str, Any],
    slides: List[Dict[str, str]],
) -> str:
    """Return a text preview when no json2video key is available."""
    topic = request["topic"]
    slide_count = request["slides"]
    style = request["style"]
    lang = request["language"]

    header = (
        f"📋 <b>Video Preview</b> — {slide_count} slides sobre \"{topic}\"\n"
        f"Estilo: {style} | Resolución: 1920×1080 | Voz: {'Sí' if request['voice_enabled'] else 'No'}\n\n"
    ) if lang == "es" else (
        f"📋 <b>Video Preview</b> — {slide_count} slides about \"{topic}\"\n"
        f"Style: {style} | Resolution: 1920×1080 | Voice: {'Yes' if request['voice_enabled'] else 'No'}\n\n"
    )

    slide_lines = []
    for i, slide in enumerate(slides, 1):
        slide_lines.append(f"<b>Slide {i}:</b> {slide['title']}\n  <i>{slide['body']}</i>")

    footer = (
        "\n\n💡 Para renderizar el video real, configura:\n"
        "<code>JSON2VIDEO_API_KEY=tu_key</code>\n"
        "Free tier: json2video.com (600 créditos gratis)"
    ) if lang == "es" else (
        "\n\n💡 To render the actual video, set:\n"
        "<code>JSON2VIDEO_API_KEY=your_key</code>\n"
        "Free tier: json2video.com (600 free credits)"
    )

    return header + "\n".join(slide_lines) + footer


# ---------------------------------------------------------------------------
# Main pipeline entry point
# ---------------------------------------------------------------------------

async def run_video_pipeline(
    prompt: str,
    notify_fn: Optional[Callable[[str], Any]] = None,
) -> str:
    """Full structured video pipeline.

    Returns a video URL string on success, or a text preview/error.
    """
    api_key = os.environ.get("JSON2VIDEO_API_KEY", "").strip()

    # Parse request
    request = parse_video_request(prompt)
    topic = request["topic"]
    slides_count = request["slides"]

    if notify_fn:
        try:
            await notify_fn(f"📝 Generando script: {slides_count} slides sobre \"{topic}\"...")
        except Exception:
            pass

    # Generate script
    slides = await generate_video_script(
        topic=topic,
        slides=slides_count,
        style=request["style"],
        language=request["language"],
    )

    # No API key → return mock preview
    if not api_key:
        logger.info("json2video_no_key_mock", topic=topic)
        return _generate_mock_preview(request, slides)

    if notify_fn:
        try:
            await notify_fn("🎨 Script listo. Enviando a json2video...")
        except Exception:
            pass

    # Build payload
    payload = build_json2video_payload(
        slides=slides,
        voice_enabled=request["voice_enabled"],
        language=request["language"],
    )

    # Submit and poll
    result = await submit_and_poll(
        payload=payload,
        api_key=api_key,
        progress_cb=notify_fn,
    )

    return result["url"]

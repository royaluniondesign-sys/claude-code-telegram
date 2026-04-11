"""Social media content pipeline.

Flow:
  parse_request() → generate_images() → generate_captions() → post_via_n8n()

Supports: instagram carousel, twitter/X thread, linkedin post
N8N handles the actual API calls (Instagram Graph API, etc.)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp
import structlog

logger = structlog.get_logger()

# Pollinations.ai FLUX.1 endpoint — free, no auth, 1080x1080 for social
_POLLINATIONS_URL = (
    "https://image.pollinations.ai/prompt/{prompt}"
    "?width=1080&height=1080&nologo=true&enhance=true&model=flux"
)
_IMAGE_TIMEOUT = 90  # image gen can be slow

# Draft save path for offline fallback
_DRAFTS_DIR = Path.home() / ".aura" / "social_drafts"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_social_request(prompt: str) -> dict[str, Any]:
    """Extract platform, post_type, topic, count, and style from a prompt.

    Handles Spanish and English phrases.
    Returns a dict with keys:
      platform: "instagram" | "twitter" | "linkedin"
      post_type: "carousel" | "post" | "thread"
      topic: str
      count: int (1–10)
      style: str
    """
    text = prompt.strip()
    lower = text.lower()

    # --- Platform detection ---
    if re.search(r"\binstagram\b|\bIG\b|\bInsta\b", text, re.IGNORECASE):
        platform = "instagram"
    elif re.search(r"\btwitter\b|\bX\b|\btweet\b|\bhilo\b|\bthread\b", text, re.IGNORECASE):
        platform = "twitter"
    elif re.search(r"\blinkedin\b", text, re.IGNORECASE):
        platform = "linkedin"
    else:
        platform = "instagram"  # default

    # --- Post type detection ---
    if re.search(r"\bcarrus?el\b|\bcarousel\b|\bslides?\b|\balbum\b", lower):
        post_type = "carousel"
    elif re.search(r"\bhilo\b|\bthread\b|\bhilos\b", lower):
        post_type = "thread"
    else:
        post_type = "carousel" if platform == "instagram" else "post"

    # --- Count extraction: "5 fotos", "3 slides", "10 imágenes", etc. ---
    count = 1
    count_match = re.search(
        r"(\d+)\s*(?:foto|image|imagen|slide|photo|pic|page|página|post|tweet)s?",
        lower,
    )
    if count_match:
        count = max(1, min(10, int(count_match.group(1))))
    elif post_type == "carousel" and count == 1:
        count = 5  # sensible default for carousels

    # --- Style hints ---
    style_matches = re.findall(
        r"\b(?:minimalista|minimal|colorful|colorido|dark|oscuro|light|claro|profesional"
        r"|professional|moderno|modern|elegante|elegant|neon|vintage|futurista|futuristic)\b",
        lower,
    )
    style = ", ".join(style_matches) if style_matches else ""

    # --- Topic extraction ---
    # Remove platform/type/count noise to isolate the core topic
    topic = text
    noise_patterns = [
        r"(?i)\bpublica?\b", r"(?i)\bpublic[ao]\b", r"(?i)\bsube?\b",
        r"(?i)\bpost(?:ea)?\b", r"(?i)\bcomparte?\b",
        r"(?i)\ben\s+instagram\b", r"(?i)\ben\s+twitter\b", r"(?i)\ben\s+linkedin\b",
        r"(?i)\binstagram\b", r"(?i)\btwitter\b", r"(?i)\blinkedin\b",
        r"(?i)\bcarrus?el\b", r"(?i)\bcarousel\b",
        r"(?i)\bun\s+hilo\b", r"(?i)\bhilo\b", r"(?i)\bthread\b",
        r"(?i)\bun\s+post\b", r"(?i)\bun\s+tweet\b",
        r"\b\d+\s*(?:foto|image|imagen|slide|photo|pic|page|página|post|tweet)s?\b",
        r"(?i)\bsobre\b", r"(?i)\bacerca\s+de\b", r"(?i)\babout\b",
        r"(?i)\bde\s+tema\b",
    ]
    for pat in noise_patterns:
        topic = re.sub(pat, " ", topic)
    topic = re.sub(r"\s{2,}", " ", topic).strip(" ,.;:")
    if not topic:
        topic = prompt

    return {
        "platform": platform,
        "post_type": post_type,
        "topic": topic,
        "count": count,
        "style": style,
    }


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

async def _fetch_single_image(session: aiohttp.ClientSession, prompt: str, idx: int) -> dict[str, Any]:
    """Fetch one image from pollinations.ai. Returns image dict or error dict."""
    encoded = urllib.parse.quote(prompt, safe="")
    url = _POLLINATIONS_URL.format(prompt=encoded)

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT)) as resp:
            if resp.status != 200:
                logger.warning("pollinations_error", idx=idx, status=resp.status)
                return {"error": f"HTTP {resp.status}", "prompt": prompt, "index": idx}
            image_bytes = await resp.read()
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            logger.info("pollinations_ok", idx=idx, size_kb=len(image_bytes) // 1024)
            return {"url": url, "b64": b64, "prompt": prompt, "index": idx}
    except asyncio.TimeoutError:
        logger.warning("pollinations_timeout", idx=idx, timeout=_IMAGE_TIMEOUT)
        return {"error": f"timeout after {_IMAGE_TIMEOUT}s", "prompt": prompt, "index": idx}
    except Exception as e:
        logger.error("pollinations_exception", idx=idx, error=str(e))
        return {"error": str(e), "prompt": prompt, "index": idx}


def _build_carousel_prompts(topic: str, count: int, style: str) -> list[str]:
    """Build visually consistent prompts for a carousel series.

    Each prompt shares the same style seed so images feel like a series.
    """
    style_suffix = f", {style}" if style else ""
    base_style = (
        f"professional social media graphic{style_suffix}, "
        "consistent color palette, clean typography, high quality, "
        "Instagram carousel style"
    )

    prompts = []
    for i in range(count):
        slide_num = i + 1
        prompt = (
            f"Slide {slide_num} of {count}: {topic}. "
            f"{base_style}, slide {slide_num}/{count}"
        )
        prompts.append(prompt)

    return prompts


async def generate_images_for_post(
    topic: str,
    count: int,
    style: str = "",
) -> list[dict[str, Any]]:
    """Generate images concurrently via pollinations.ai FLUX.1.

    Returns list of dicts: {"url": str, "b64": str, "prompt": str, "index": int}.
    Failed images include {"error": str} instead of b64/url.
    """
    prompts = _build_carousel_prompts(topic, count, style)

    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_single_image(session, prompt, idx)
            for idx, prompt in enumerate(prompts)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    return list(results)


# ---------------------------------------------------------------------------
# Caption generation
# ---------------------------------------------------------------------------

async def generate_captions(
    topic: str,
    images: list[dict[str, Any]],
    platform: str,
    style: str = "",
) -> list[str]:
    """Generate captions using Gemini (free CLI, no API key).

    Single prompt → JSON array of N captions.
    Each caption is platform-appropriate: with hashtags for Instagram,
    shorter for Twitter, professional for LinkedIn.
    """
    count = len(images)

    platform_instructions: dict[str, str] = {
        "instagram": (
            f"Write {count} Instagram captions. Each: 2-3 sentences, engaging, "
            "end with 5-8 relevant hashtags. In the same language as the topic."
        ),
        "twitter": (
            f"Write {count} tweets. Each: max 280 chars, punchy, include 1-2 hashtags. "
            "Same language as the topic."
        ),
        "linkedin": (
            f"Write {count} LinkedIn post captions. Each: professional, 2-4 sentences, "
            "insight-driven, 2-3 relevant hashtags. Same language as the topic."
        ),
    }

    instructions = platform_instructions.get(
        platform, platform_instructions["instagram"]
    )

    style_note = f" Style: {style}." if style else ""
    prompt = (
        f"Topic: {topic}.{style_note}\n\n"
        f"{instructions}\n\n"
        f"Respond ONLY with a valid JSON array of {count} strings. "
        f"No markdown, no explanation. Example:\n"
        f'["caption 1", "caption 2", ...]'
    )

    raw = await _call_gemini_for_captions(prompt)

    # Parse JSON array from response
    captions = _parse_captions_json(raw, count, topic, platform)
    return captions


async def _call_gemini_for_captions(prompt: str) -> str:
    """Call Gemini CLI for caption generation. Falls back to simple captions."""
    import shutil
    from pathlib import Path as _Path

    gemini_path = shutil.which("gemini")
    if not gemini_path:
        for candidate in ["/opt/homebrew/bin/gemini", "/usr/local/bin/gemini"]:
            if _Path(candidate).exists():
                gemini_path = candidate
                break

    if not gemini_path:
        logger.warning("gemini_not_found_for_captions")
        return ""

    try:
        proc = await asyncio.create_subprocess_exec(
            gemini_path, "-p", prompt, "--approval-mode", "yolo", "-o", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        raw = stdout.decode("utf-8", errors="replace").strip()
        # Strip ANSI escape codes
        raw = re.sub(r"\x1b\[[0-9;]*[mGKHFJA-Za-z]|\x1b[()][AB012]", "", raw)
        return raw
    except Exception as e:
        logger.warning("gemini_caption_error", error=str(e))
        return ""


def _parse_captions_json(raw: str, count: int, topic: str, platform: str) -> list[str]:
    """Parse JSON array from Gemini response with fallback."""
    # Try to find JSON array in response
    array_match = re.search(r"\[.*\]", raw, re.DOTALL)
    if array_match:
        try:
            parsed = json.loads(array_match.group())
            if isinstance(parsed, list) and len(parsed) >= count:
                return [str(c) for c in parsed[:count]]
            if isinstance(parsed, list) and parsed:
                # Pad if fewer than expected
                while len(parsed) < count:
                    parsed.append(parsed[-1])
                return [str(c) for c in parsed[:count]]
        except json.JSONDecodeError:
            pass

    # Fallback: generate simple captions
    logger.warning("caption_json_parse_failed", raw_length=len(raw))
    hashtag_map = {
        "instagram": "#contenido #digitalmarketing #socialmedia",
        "twitter": "#tech",
        "linkedin": "#professional #growth",
    }
    hashtags = hashtag_map.get(platform, "")
    return [
        f"✨ {topic} — parte {i + 1} {hashtags}".strip()
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# N8N payload and posting
# ---------------------------------------------------------------------------

def build_n8n_payload(
    platform: str,
    post_type: str,
    topic: str,
    images: list[dict[str, Any]],
    captions: list[str],
) -> dict[str, Any]:
    """Build the webhook payload N8N expects.

    Structure:
      platform: str
      type: str ("carousel" | "post" | "thread")
      images: list of {"b64": str, "prompt": str} (base64 encoded)
      captions: list of str
      metadata: {topic, count, timestamp}
    """
    images_payload = [
        {
            "b64": img.get("b64", ""),
            "prompt": img.get("prompt", ""),
            "index": img.get("index", i),
        }
        for i, img in enumerate(images)
        if not img.get("error")
    ]

    return {
        "platform": platform,
        "type": post_type,
        "images": images_payload,
        "captions": captions,
        "metadata": {
            "topic": topic,
            "count": len(images_payload),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "aura-bot",
        },
    }


async def post_to_social(
    platform: str,
    post_type: str,
    topic: str,
    images: list[dict[str, Any]],
    captions: list[str],
    n8n_url: str,
) -> dict[str, Any]:
    """POST to n8n_url + /webhook/aura-social.

    Returns {"success": bool, "post_url": str, "error": str}.
    Falls back to saving draft locally if N8N is unreachable.
    """
    from .n8n_client import call_webhook

    payload = build_n8n_payload(platform, post_type, topic, images, captions)

    # Override N8N URL if provided (else n8n_client uses RUD_N8N_URL env)
    original_env = os.environ.get("RUD_N8N_URL")
    if n8n_url:
        os.environ["RUD_N8N_URL"] = n8n_url

    try:
        result = await call_webhook("aura-social", payload)
    finally:
        if original_env is not None:
            os.environ["RUD_N8N_URL"] = original_env
        elif n8n_url:
            os.environ.pop("RUD_N8N_URL", None)

    if "error" in result:
        # Save draft locally as fallback
        draft_path = _save_draft(payload)
        return {
            "success": False,
            "post_url": "",
            "error": result["error"],
            "draft_saved": str(draft_path) if draft_path else "",
        }

    return {
        "success": True,
        "post_url": result.get("post_url", result.get("url", "")),
        "error": "",
    }


def _save_draft(payload: dict[str, Any]) -> Optional[Path]:
    """Save social post draft to ~/.aura/social_drafts/ as JSON fallback."""
    try:
        _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        platform = payload.get("platform", "unknown")
        draft_file = _DRAFTS_DIR / f"{platform}_{ts}.json"

        # Store payload without heavy b64 image data (just prompts for reference)
        draft = {
            "platform": payload.get("platform"),
            "type": payload.get("type"),
            "captions": payload.get("captions", []),
            "metadata": payload.get("metadata", {}),
            "image_prompts": [
                img.get("prompt", "") for img in payload.get("images", [])
            ],
        }
        draft_file.write_text(json.dumps(draft, indent=2, ensure_ascii=False))
        logger.info("social_draft_saved", path=str(draft_file))
        return draft_file
    except Exception as e:
        logger.error("social_draft_save_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

async def run_social_pipeline(
    prompt: str,
    notify_fn: Optional[Callable[[str], Any]] = None,
) -> str:
    """Execute the full social media content pipeline.

    Steps: parse → generate images → generate captions → post via N8N

    Args:
        prompt: Raw user message (e.g. "publica un carrusel en instagram sobre claude code, 5 fotos")
        notify_fn: Optional async callable for progress updates sent to Telegram.

    Returns:
        Human-readable result string for Telegram.
    """

    async def _notify(msg: str) -> None:
        if notify_fn is not None:
            try:
                await notify_fn(msg)
            except Exception:
                pass

    # Step 1: Parse request
    parsed = parse_social_request(prompt)
    platform = parsed["platform"]
    post_type = parsed["post_type"]
    topic = parsed["topic"]
    count = parsed["count"]
    style = parsed["style"]

    logger.info(
        "social_pipeline_start",
        platform=platform,
        post_type=post_type,
        topic=topic[:60],
        count=count,
    )

    await _notify(f"🎨 Generando {count} imagen{'es' if count > 1 else ''}...")

    # Step 2: Generate images concurrently
    images = await generate_images_for_post(topic, count, style)

    # Count successes
    ok_images = [img for img in images if not img.get("error")]
    failed = count - len(ok_images)

    if not ok_images:
        return (
            f"❌ No se pudieron generar imágenes para el {post_type} de {platform}. "
            "Verifica tu conexión."
        )

    await _notify(f"✍️ Escribiendo captions para {platform}...")

    # Step 3: Generate captions
    captions = await generate_captions(topic, ok_images, platform, style)

    await _notify(f"📤 Publicando en {platform}...")

    # Step 4: Post via N8N
    n8n_url = os.environ.get("RUD_N8N_URL", "")
    result = await post_to_social(platform, post_type, topic, ok_images, captions, n8n_url)

    # Build response message
    if result["success"]:
        post_url = result.get("post_url", "")
        url_line = f"\n🔗 {post_url}" if post_url else ""
        warn = f"\n⚠️ {failed} imagen(es) fallaron" if failed > 0 else ""
        return (
            f"✅ {post_type.capitalize()} publicado en {platform.capitalize()}!"
            f"{url_line}{warn}\n"
            f"📸 {len(ok_images)} imagen(es) · tema: {topic[:60]}"
        )
    else:
        draft_line = ""
        if result.get("draft_saved"):
            draft_line = f"\n💾 Borrador guardado: {result['draft_saved']}"
        return (
            f"❌ Error publicando en {platform}: {result['error']}"
            f"{draft_line}\n\n"
            f"💡 Asegúrate de que N8N esté corriendo en {n8n_url or 'RUD_N8N_URL'}."
        )

"""ImageBrain — AI image generation cascade.

Priority (best quality → fully free fallback):
  1. NVIDIA FLUX.1-dev  — free with nvapi key, best quality (~10s, 768-1024px)
  2. NVIDIA FLUX.1-schnell — same key, faster (4s)
  3. HuggingFace FLUX.1-schnell — needs HF_TOKEN, free tier
  4. Pollinations.ai FLUX.1 — zero-auth, zero-key, always available

Returns __IMAGE_B64__:<base64> so orchestrator can send as Telegram photo.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# ── NVIDIA Build ──────────────────────────────────────────────────────────────
_NV_API_KEY = os.environ.get(
    "NVIDIA_API_KEY",
    "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",
)
_NV_DEV_URL    = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
_NV_SCHNELL_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-schnell"

# ── HuggingFace ───────────────────────────────────────────────────────────────
_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_HF_URL   = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"

# ── Pollinations (fallback, no key) ───────────────────────────────────────────
_POLL_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1024&height=1024&nologo=true&model=flux"

_DEFAULT_TIMEOUT = 60


def _sanitize(prompt: str, max_len: int = 450) -> str:
    """Strip meta-instructions and cap length (NVIDIA black-image prevention)."""
    import re
    clean = re.sub(r"(?i)(style:|mood:|format:|subject:|use|generate|create|make)\s*", "", prompt)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_len]


async def _try_nvidia(prompt: str, timeout: int, dev: bool = True) -> Optional[bytes]:
    """Attempt NVIDIA FLUX generation. Returns bytes or None on failure."""
    import aiohttp
    url = _NV_DEV_URL if dev else _NV_SCHNELL_URL
    payload = {
        "prompt": _sanitize(prompt),
        "width": 1024, "height": 1024,
        "cfg_scale": 5, "seed": 0,
    }
    headers = {
        "Authorization": f"Bearer {_NV_API_KEY}",
        "Accept": "image/jpeg",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning("nvidia_flux_failed", status=resp.status, dev=dev)
                    return None
                data = await resp.read()
                # Black image guard: NVIDIA returns tiny <5KB JPEG on silent errors
                if len(data) < 5000:
                    logger.warning("nvidia_flux_black_image", bytes=len(data))
                    return None
                return data
    except Exception as e:
        logger.warning("nvidia_flux_exception", error=str(e)[:80], dev=dev)
        return None


async def _try_huggingface(prompt: str, timeout: int) -> Optional[bytes]:
    """Attempt HuggingFace FLUX.1-schnell. Requires HF_TOKEN."""
    if not _HF_TOKEN:
        return None
    import aiohttp
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                _HF_URL,
                json={"inputs": _sanitize(prompt)},
                headers={"Authorization": f"Bearer {_HF_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception as e:
        logger.warning("hf_flux_exception", error=str(e)[:80])
        return None


async def _try_pollinations(prompt: str, timeout: int) -> Optional[bytes]:
    """Fallback: Pollinations.ai — always free, no key needed."""
    import aiohttp
    encoded = urllib.parse.quote(prompt[:300], safe="")
    url = _POLL_URL.format(prompt=encoded)
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception as e:
        logger.warning("pollinations_exception", error=str(e)[:80])
        return None


class ImageBrain(Brain):
    """AI image generation — NVIDIA FLUX.1-dev primary, free cascade fallback."""

    name = "image"
    display_name = "Image Gen (FLUX.1-dev)"
    emoji = "🎨"

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_: Any,
    ) -> BrainResponse:
        timeout = timeout_seconds or self._timeout
        start = time.time()
        provider = "?"
        image_bytes: Optional[bytes] = None

        # 1. NVIDIA FLUX.1-dev (best quality)
        if _NV_API_KEY:
            image_bytes = await _try_nvidia(prompt, timeout, dev=True)
            if image_bytes:
                provider = "NVIDIA FLUX.1-dev"
            else:
                # 2. NVIDIA FLUX.1-schnell (faster fallback on same key)
                image_bytes = await _try_nvidia(prompt, min(timeout, 30), dev=False)
                if image_bytes:
                    provider = "NVIDIA FLUX.1-schnell"

        # 3. HuggingFace
        if not image_bytes and _HF_TOKEN:
            image_bytes = await _try_huggingface(prompt, timeout)
            if image_bytes:
                provider = "HuggingFace FLUX.1-schnell"

        # 4. Pollinations (always-free, zero-auth)
        if not image_bytes:
            image_bytes = await _try_pollinations(prompt, timeout)
            if image_bytes:
                provider = "Pollinations FLUX.1"

        elapsed = int((time.time() - start) * 1000)

        if not image_bytes:
            return BrainResponse(
                content="❌ Todos los proveedores de imagen fallaron",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="all_failed",
            )

        logger.info(
            "image_brain_ok",
            provider=provider, elapsed_ms=elapsed, size_kb=len(image_bytes) // 1024,
        )

        b64 = base64.b64encode(image_bytes).decode()
        return BrainResponse(
            content=f"__IMAGE_B64__:{b64}",
            brain_name=self.name,
            duration_ms=elapsed,
            metadata={"provider": provider},
        )

    async def health_check(self) -> BrainStatus:
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": "FLUX.1-dev (NVIDIA) → schnell → HuggingFace → Pollinations",
            "auth": "NVIDIA API key (free tier) + Pollinations fallback (no key)",
            "cost": "Free",
        }

"""ImageBrain — free AI image generation via pollinations.ai (FLUX.1, no API key).

Endpoint: https://image.pollinations.ai/prompt/{encoded_prompt}
Returns JPEG bytes directly — no auth, no rate limit advertised.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_BASE_URL = "https://image.pollinations.ai/prompt/{prompt}"
_PARAMS = "width=1024&height=1024&nologo=true&enhance=true&model=flux"
_DEFAULT_TIMEOUT = 60  # image gen can take ~10-30s


class ImageBrain(Brain):
    """Image generation via pollinations.ai — FLUX.1, free, no key."""

    name = "image"
    display_name = "Image Gen (Pollinations)"
    emoji = "🎨"

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def execute(
        self,
        prompt: str,
        timeout_seconds: int = 0,
        **_: Any,
    ) -> BrainResponse:
        import asyncio
        import aiohttp

        timeout = timeout_seconds or self._timeout
        start = time.time()

        # Build URL
        encoded = urllib.parse.quote(prompt, safe="")
        url = f"{_BASE_URL.format(prompt=encoded)}?{_PARAMS}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        elapsed = int((time.time() - start) * 1000)
                        return BrainResponse(
                            content=f"Image gen error: HTTP {resp.status}",
                            brain_name=self.name,
                            duration_ms=elapsed,
                            is_error=True,
                            error_type="http_error",
                        )

                    image_bytes = await resp.read()
                    elapsed = int((time.time() - start) * 1000)
                    content_type = resp.headers.get("content-type", "image/jpeg")

                    logger.info(
                        "image_gen_ok",
                        elapsed_ms=elapsed,
                        size_kb=len(image_bytes) // 1024,
                        content_type=content_type,
                    )

                    # Return bytes as base64 in content + a special marker
                    # Orchestrator checks for this marker to send as photo
                    import base64
                    b64 = base64.b64encode(image_bytes).decode()
                    return BrainResponse(
                        content=f"__IMAGE_B64__:{b64}",
                        brain_name=self.name,
                        duration_ms=elapsed,
                    )

        except asyncio.TimeoutError:
            elapsed = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"⏱ Image gen timeout ({timeout}s)",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("image_brain_error", error=str(e))
            return BrainResponse(
                content=f"Image error: {e}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": "FLUX.1 via pollinations.ai",
            "auth": "none (free public API)",
            "cost": "Free",
        }

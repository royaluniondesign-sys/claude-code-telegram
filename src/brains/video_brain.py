"""Video generation brain — multi-provider cascade.

Priority order (free → paid):
1. Luma AI (Dream Machine) — generous free tier, cinematic quality
2. Kling AI — 66 free credits/day, best motion quality
3. Runway ML — limited free, best for text-to-video
4. Pollinations video — placeholder for if they add video support

For structured/slide videos (NOT cinematic):
→ Use json2video API instead (see src/workflows/video_compose.py)

Auth: API keys via env vars (all optional — graceful fallback)
  KLING_API_KEY    — Kling AI
  LUMA_API_KEY     — Luma Dream Machine
  RUNWAY_API_KEY   — Runway ML
  JSON2VIDEO_API_KEY — json2video.com
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# Polling configuration
_POLL_INTERVAL = 5      # seconds between status checks
_MAX_POLL_TIME = 180    # 3-minute max wait per provider


class VideoBrain(Brain):
    """Video generation via multi-provider cascade (Luma → Kling → Runway)."""

    name = "video"
    display_name = "Video Gen"
    emoji = "🎬"

    def __init__(self, timeout: int = _MAX_POLL_TIME) -> None:
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

        luma_key = os.environ.get("LUMA_API_KEY", "").strip()
        kling_key = os.environ.get("KLING_API_KEY", "").strip()
        runway_key = os.environ.get("RUNWAY_API_KEY", "").strip()

        providers = []
        if luma_key:
            providers.append(("luma", self._luma, luma_key))
        if kling_key:
            providers.append(("kling", self._kling, kling_key))
        if runway_key:
            providers.append(("runway", self._runway, runway_key))

        if not providers:
            elapsed = int((time.time() - start) * 1000)
            missing = "LUMA_API_KEY, KLING_API_KEY, or RUNWAY_API_KEY"
            return BrainResponse(
                content=(
                    "❌ No video API key found.\n\n"
                    f"Set one of: <code>{missing}</code>\n\n"
                    "Recommended free tier: Luma Dream Machine (LUMA_API_KEY)\n"
                    "Or json2video (JSON2VIDEO_API_KEY) for slide videos."
                ),
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="no_api_key",
            )

        last_error = ""
        for provider_name, provider_fn, api_key in providers:
            try:
                logger.info("video_brain_try", provider=provider_name)
                video_url = await asyncio.wait_for(
                    provider_fn(prompt, api_key),
                    timeout=timeout,
                )
                elapsed = int((time.time() - start) * 1000)
                logger.info(
                    "video_brain_ok",
                    provider=provider_name,
                    elapsed_ms=elapsed,
                    url=video_url[:80] if video_url else "",
                )
                return BrainResponse(
                    content=f"__VIDEO_URL__:{video_url}",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    metadata={"provider": provider_name},
                )
            except asyncio.TimeoutError:
                last_error = f"{provider_name}: timeout ({timeout}s)"
                logger.warning("video_brain_timeout", provider=provider_name)
            except Exception as exc:
                last_error = f"{provider_name}: {exc}"
                logger.warning("video_brain_provider_error", provider=provider_name, error=str(exc))

        elapsed = int((time.time() - start) * 1000)
        return BrainResponse(
            content=f"❌ All video providers failed. Last error: {last_error}",
            brain_name=self.name,
            duration_ms=elapsed,
            is_error=True,
            error_type="all_providers_failed",
        )

    # ------------------------------------------------------------------
    # Provider implementations
    # ------------------------------------------------------------------

    async def _luma(self, prompt: str, api_key: str) -> str:
        """Luma Dream Machine — POST generation, poll for completion."""
        import aiohttp

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "loop": False,
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            # Submit generation
            async with session.post(
                "https://api.lumalabs.ai/dream-machine/v1/generations",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            gen_id = data.get("id")
            if not gen_id:
                raise ValueError(f"Luma: no generation id in response: {data}")

            # Poll for completion
            poll_url = f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}"
            deadline = time.time() + _MAX_POLL_TIME

            while time.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                async with session.get(
                    poll_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    status_data = await resp.json()

                state = status_data.get("state", "")
                if state == "completed":
                    assets = status_data.get("assets", {})
                    video_url = assets.get("video", "")
                    if not video_url:
                        raise ValueError(f"Luma: completed but no video URL: {status_data}")
                    return video_url
                elif state in ("failed", "error"):
                    failure = status_data.get("failure_reason", "unknown")
                    raise RuntimeError(f"Luma generation failed: {failure}")
                # states: pending, dreaming → keep polling

            raise asyncio.TimeoutError(f"Luma: generation did not complete in {_MAX_POLL_TIME}s")

    async def _kling(self, prompt: str, api_key: str) -> str:
        """Kling AI — text-to-video, poll for completion."""
        import aiohttp

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model_name": "kling-v1",
            "prompt": prompt,
            "negative_prompt": "blurry, low quality, artifacts",
            "cfg_scale": 0.5,
            "mode": "std",
            "duration": "5",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                "https://api.klingai.com/v1/videos/text2video",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            task_id = data.get("data", {}).get("task_id") or data.get("task_id")
            if not task_id:
                raise ValueError(f"Kling: no task_id in response: {data}")

            # Poll
            poll_url = f"https://api.klingai.com/v1/videos/text2video/{task_id}"
            deadline = time.time() + _MAX_POLL_TIME

            while time.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                async with session.get(
                    poll_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    status_data = await resp.json()

                task_status = (
                    status_data.get("data", {}).get("task_status")
                    or status_data.get("task_status", "")
                )
                if task_status == "succeed":
                    videos = (
                        status_data.get("data", {}).get("task_result", {}).get("videos", [])
                    )
                    if videos:
                        return videos[0].get("url", "")
                    raise ValueError(f"Kling: succeed but no video URL: {status_data}")
                elif task_status in ("failed", "error"):
                    raise RuntimeError(f"Kling task failed: {status_data}")

            raise asyncio.TimeoutError(f"Kling: task did not complete in {_MAX_POLL_TIME}s")

    async def _runway(self, prompt: str, api_key: str) -> str:
        """Runway ML — text-to-video generation."""
        import aiohttp

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
        }
        payload: Dict[str, Any] = {
            "promptText": prompt,
            "model": "gen3a_turbo",
            "duration": 5,
            "ratio": "1280:768",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                "https://api.runwayml.com/v1/image_to_video",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            task_id = data.get("id")
            if not task_id:
                raise ValueError(f"Runway: no task id in response: {data}")

            # Poll
            poll_url = f"https://api.runwayml.com/v1/tasks/{task_id}"
            deadline = time.time() + _MAX_POLL_TIME

            while time.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                async with session.get(
                    poll_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    status_data = await resp.json()

                status = status_data.get("status", "")
                if status == "SUCCEEDED":
                    output = status_data.get("output", [])
                    if output:
                        return output[0]
                    raise ValueError(f"Runway: SUCCEEDED but no output URL: {status_data}")
                elif status in ("FAILED", "CANCELLED"):
                    failure = status_data.get("failure", "unknown")
                    raise RuntimeError(f"Runway task {status}: {failure}")

            raise asyncio.TimeoutError(f"Runway: task did not complete in {_MAX_POLL_TIME}s")

    async def health_check(self) -> BrainStatus:
        has_key = any(
            os.environ.get(k, "").strip()
            for k in ("LUMA_API_KEY", "KLING_API_KEY", "RUNWAY_API_KEY")
        )
        return BrainStatus.READY if has_key else BrainStatus.NOT_AUTHENTICATED

    async def get_info(self) -> Dict[str, Any]:
        luma = bool(os.environ.get("LUMA_API_KEY", "").strip())
        kling = bool(os.environ.get("KLING_API_KEY", "").strip())
        runway = bool(os.environ.get("RUNWAY_API_KEY", "").strip())
        return {
            "name": self.name,
            "display_name": self.display_name,
            "providers": {
                "luma": "configured" if luma else "missing LUMA_API_KEY",
                "kling": "configured" if kling else "missing KLING_API_KEY",
                "runway": "configured" if runway else "missing RUNWAY_API_KEY",
            },
            "cascade_order": ["luma", "kling", "runway"],
        }

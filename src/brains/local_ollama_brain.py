"""LocalOllamaBrain — direct HTTP to local Ollama (no subprocess, no macOS permission issues).

Uses http://localhost:11434/api/generate — zero cost, fully local, fast.
Default model: qwen2.5:7b (code-focused).
Falls back to granite3.3:8b if qwen unavailable.

Role in conductor:
  Layer 1 (analysis/diagnosis) — read context, identify root cause
  Layer 2 (synthesis/codegen) — generate implementation, output complete file content

Does NOT write files — that's Layer 3's job (haiku via claude CLI with tool access).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_MODEL = "qwen2.5:7b"
_FALLBACK_MODEL = "granite3.3:8b"
_DEFAULT_TIMEOUT = 120


class LocalOllamaBrain(Brain):
    """Direct HTTP brain to local Ollama. No subprocess, no macOS TCC prompts."""

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        timeout: int = _DEFAULT_TIMEOUT,
        base_url: str = _OLLAMA_URL,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "local-ollama"

    @property
    def display_name(self) -> str:
        return f"Ollama ({self._model})"

    @property
    def emoji(self) -> str:
        return "🦙"

    async def execute(
        self,
        prompt: str,
        timeout_seconds: int = 0,
        **_kwargs: Any,
    ) -> BrainResponse:
        timeout = timeout_seconds or self._timeout
        start = time.time()

        try:
            import aiohttp
        except ImportError:
            return BrainResponse(
                content="aiohttp not installed",
                brain_name=self.name,
                is_error=True,
                error_type="import_error",
            )

        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": 2048,
                "temperature": 0.3,
                "top_p": 0.9,
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._base_url}/api/generate",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    elapsed = int((time.time() - start) * 1000)
                    if resp.status != 200:
                        body = await resp.text()
                        return BrainResponse(
                            content=f"Ollama HTTP {resp.status}: {body[:200]}",
                            brain_name=self.name,
                            duration_ms=elapsed,
                            is_error=True,
                            error_type="http_error",
                        )
                    data = await resp.json()
                    output = data.get("response", "").strip()
                    if not output:
                        return BrainResponse(
                            content="(empty response)",
                            brain_name=self.name,
                            duration_ms=elapsed,
                            is_error=True,
                            error_type="empty_output",
                        )
                    logger.info(
                        "local_ollama_ok",
                        model=self._model,
                        duration_ms=elapsed,
                        output_len=len(output),
                    )
                    return BrainResponse(
                        content=output,
                        brain_name=self.name,
                        duration_ms=elapsed,
                    )

        except asyncio.TimeoutError:
            elapsed = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"⏱️ Ollama timeout ({timeout}s) — model may be loading",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("local_ollama_error", error=str(e))
            return BrainResponse(
                content=f"❌ Ollama: {e}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m["name"] for m in data.get("models", [])]
                        if any(self._model.split(":")[0] in m for m in models):
                            return BrainStatus.READY
                        return BrainStatus.NOT_INSTALLED
                    return BrainStatus.ERROR
        except Exception:
            return BrainStatus.ERROR

    async def get_info(self) -> Dict[str, Any]:
        models = []
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model,
            "base_url": self._base_url,
            "available_models": models,
            "cost": "$0 — local Ollama",
            "timeout_s": self._timeout,
        }

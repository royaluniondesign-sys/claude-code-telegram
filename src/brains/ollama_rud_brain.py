"""Ollama RUD Brain — connects to Ollama running on the remote RUD server.

URL is configurable via RUD_OLLAMA_URL env var (default: http://192.168.1.219:11434).
Falls back gracefully when the server is unreachable.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_DEFAULT_URL = "http://192.168.1.219:11434"
_DEFAULT_TIMEOUT = 120

# Model preference order: first match wins
_PREFERRED_MODELS = ["llama3", "mistral", "codellama", "phi3"]

_SYSTEM_PROMPT = """\
You are AURA, Ricardo's personal AI assistant. Warm, direct, natural — like a smart friend.

Rules:
- Respond in the same language Ricardo writes (Spanish or English).
- Be concise — this is Telegram.
- NEVER say what model you are. You are AURA.
- NEVER fabricate data or file contents you haven't verified.
- No preamble, no filler, just answer.
"""


def _get_base_url() -> str:
    return os.environ.get("RUD_OLLAMA_URL", _DEFAULT_URL).rstrip("/")


def _pick_best_model(available_models: List[str]) -> Optional[str]:
    """Return the first preferred model found in the available list."""
    for preferred in _PREFERRED_MODELS:
        for model in available_models:
            if model.startswith(preferred):
                return model
    return available_models[0] if available_models else None


class OllamaRudBrain(Brain):
    """Ollama brain connecting to the remote RUD server via HTTP."""

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._model: Optional[str] = None

    @property
    def name(self) -> str:
        return "ollama-rud"

    @property
    def display_name(self) -> str:
        model_label = self._model or "auto"
        return f"Ollama RUD ({model_label})"

    @property
    def emoji(self) -> str:
        return "\U0001f5a5\ufe0f"  # 🖥️

    def _base_url(self) -> str:
        return _get_base_url()

    async def _list_models(self) -> List[str]:
        """Return available model names from the RUD Ollama server."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self._base_url()}/api/tags")
                if r.status_code == 200:
                    data = r.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception as exc:
            logger.debug("ollama_rud_list_models_error", error=str(exc))
        return []

    async def _ensure_model(self) -> Optional[str]:
        """Auto-detect and cache the best available model."""
        if self._model:
            return self._model
        models = await self._list_models()
        best = _pick_best_model(models)
        if best:
            self._model = best
            logger.info("ollama_rud_model_selected", model=best)
        return self._model

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        history: Optional[list] = None,
        **_: Any,
    ) -> BrainResponse:
        """Execute a prompt via the RUD Ollama server."""
        import httpx

        start = time.time()
        effective_timeout = timeout_seconds or self._timeout

        model = await self._ensure_model()
        if not model:
            elapsed_ms = int((time.time() - start) * 1000)
            return BrainResponse(
                content="RUD server offline or no models available. Run rud_server_setup.sh on the server.",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="unreachable",
            )

        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                r = await client.post(
                    f"{self._base_url()}/api/chat",
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()

            elapsed_ms = int((time.time() - start) * 1000)
            msg = data.get("message", {})
            content = msg.get("content", "").strip()

            if not content:
                return BrainResponse(
                    content="(no output from RUD Ollama)",
                    brain_name=self.name,
                    duration_ms=elapsed_ms,
                    is_error=True,
                    error_type="empty_response",
                )

            return BrainResponse(
                content=content,
                brain_name=self.name,
                duration_ms=elapsed_ms,
            )

        except httpx.ConnectError:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning("ollama_rud_unreachable", url=self._base_url())
            return BrainResponse(
                content=f"RUD server unreachable ({self._base_url()}). Is it online?",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="unreachable",
            )
        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning("ollama_rud_timeout", timeout=effective_timeout)
            return BrainResponse(
                content=f"RUD Ollama timed out after {effective_timeout}s",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="timeout",
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ollama_rud_error", error=str(exc))
            return BrainResponse(
                content=f"RUD Ollama error: {exc}",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type=type(exc).__name__,
            )

    async def health_check(self) -> BrainStatus:
        """Check if the RUD Ollama server is reachable."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base_url()}/api/tags")
                if r.status_code == 200:
                    return BrainStatus.READY
                return BrainStatus.ERROR
        except Exception as exc:
            logger.debug("ollama_rud_health_error", error=str(exc))
            return BrainStatus.NOT_INSTALLED

    async def get_info(self) -> Dict[str, Any]:
        """Return info about the RUD Ollama brain."""
        models = await self._list_models()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model or "(auto-detect on first use)",
            "available_models": models,
            "auth": "None (LAN access)",
            "cost": "FREE — remote server",
            "server_url": self._base_url(),
        }

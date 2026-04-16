"""Ollama RUD Brain — connects to Ollama running on the remote RUD server.

URL resolution order (first reachable wins):
  1. RUD_OLLAMA_URL  (LAN: http://192.168.1.219:11434)
  2. RUD_OLLAMA_NGROK_URL  (ngrok tunnel — for off-LAN access)

Health-check distinguishes:
  UNREACHABLE  — server exists but can't be reached (different network / firewall)
  NOT_INSTALLED — Ollama not running on that host (connection refused / no Ollama)
  READY        — online and returning models
"""

import os
import time
from typing import Any, Dict, List, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_DEFAULT_LAN_URL = "http://192.168.1.219:11434"
_DEFAULT_TIMEOUT = 120
_CONNECT_PROBE_TIMEOUT = 5.0  # fast probe to avoid 120s waits on unreachable hosts

# Model preference: strongest code model first
_PREFERRED_MODELS = ["qwen2.5-coder", "qwen2.5", "llama3", "mistral", "codellama", "phi3"]

_SYSTEM_PROMPT = """\
You are AURA, Ricardo's personal AI assistant. Warm, direct, natural — like a smart friend.

Rules:
- Respond in the same language Ricardo writes (Spanish or English).
- Be concise — this is Telegram.
- NEVER say what model you are. You are AURA.
- NEVER fabricate data or file contents you haven't verified.
- No preamble, no filler, just answer.
"""


def _get_candidate_urls() -> List[str]:
    """Return ordered list of URLs to try: LAN first, then ngrok tunnel."""
    urls: List[str] = []
    lan = os.environ.get("RUD_OLLAMA_URL", _DEFAULT_LAN_URL).rstrip("/")
    ngrok = os.environ.get("RUD_OLLAMA_NGROK_URL", "").rstrip("/")
    if lan:
        urls.append(lan)
    if ngrok and ngrok not in urls:
        urls.append(ngrok)
    return urls


def _pick_best_model(available_models: List[str]) -> Optional[str]:
    """Return the first preferred model found in the available list."""
    for preferred in _PREFERRED_MODELS:
        for model in available_models:
            if preferred in model:
                return model
    return available_models[0] if available_models else None


class OllamaRudBrain(Brain):
    """Ollama brain connecting to the remote RUD server via HTTP.

    Tries LAN URL first, falls back to ngrok tunnel if configured.
    Distinguishes UNREACHABLE (network) from NOT_INSTALLED (no Ollama).
    """

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._model: Optional[str] = None
        self._active_url: Optional[str] = None  # cached after first successful probe

    @property
    def name(self) -> str:
        return "ollama-rud"

    @property
    def display_name(self) -> str:
        model_label = self._model or "auto"
        via = "ngrok" if self._active_url and "ngrok" in self._active_url else "LAN"
        return f"Ollama RUD/{via} ({model_label})"

    @property
    def emoji(self) -> str:
        return "\U0001f5a5\ufe0f"  # 🖥️

    async def _probe_url(self, url: str) -> Optional[List[str]]:
        """Try to reach Ollama at *url*. Returns model list or None if unreachable."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=_CONNECT_PROBE_TIMEOUT) as client:
                r = await client.get(f"{url}/api/tags")
                if r.status_code == 200:
                    data = r.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception as exc:
            logger.debug("ollama_rud_probe_failed", url=url, error=str(exc)[:80])
        return None

    async def _resolve_url_and_models(self) -> tuple[Optional[str], List[str]]:
        """Try each candidate URL and return the first reachable one + its models."""
        for url in _get_candidate_urls():
            models = await self._probe_url(url)
            if models is not None:
                logger.info("ollama_rud_connected", url=url, models=len(models))
                return url, models
        return None, []

    async def _ensure_ready(self) -> tuple[Optional[str], Optional[str]]:
        """Ensure we have an active URL and a selected model. Returns (url, model)."""
        if self._active_url and self._model:
            # Quick re-probe to detect if connection dropped
            models = await self._probe_url(self._active_url)
            if models is not None:
                return self._active_url, self._model
            # Connection dropped — reset and retry
            self._active_url = None
            self._model = None

        url, models = await self._resolve_url_and_models()
        if url is None:
            return None, None
        model = _pick_best_model(models)
        if model:
            self._active_url = url
            self._model = model
            logger.info("ollama_rud_model_selected", model=model, url=url)
        return self._active_url, self._model

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

        url, model = await self._ensure_ready()

        if not url or not model:
            elapsed_ms = int((time.time() - start) * 1000)
            candidates = _get_candidate_urls()
            hint = (
                "Tried: " + ", ".join(candidates)
                + ". Set RUD_OLLAMA_NGROK_URL in .env for off-LAN access, "
                + "or run: ollama serve (on the RUD server)."
            )
            return BrainResponse(
                content=f"RUD Ollama UNREACHABLE. {hint}",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="UNREACHABLE",
            )

        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        payload = {"model": model, "messages": messages, "stream": False}

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                r = await client.post(f"{url}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()

            elapsed_ms = int((time.time() - start) * 1000)
            content = (data.get("message", {}).get("content") or "").strip()

            if not content:
                return BrainResponse(
                    content="(no output from RUD Ollama)",
                    brain_name=self.name,
                    duration_ms=elapsed_ms,
                    is_error=True,
                    error_type="empty_response",
                )

            return BrainResponse(content=content, brain_name=self.name, duration_ms=elapsed_ms)

        except httpx.ConnectError:
            self._active_url = None  # invalidate cache
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning("ollama_rud_connect_error", url=url)
            return BrainResponse(
                content=f"RUD server connection lost ({url}). Will retry next call.",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="UNREACHABLE",
            )
        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning("ollama_rud_timeout", timeout=effective_timeout, url=url)
            return BrainResponse(
                content=f"RUD Ollama timed out after {effective_timeout}s ({url})",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="timeout",
            )
        except Exception as exc:
            self._active_url = None
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
        """Check server reachability.

        Returns:
          READY         — Ollama online, models available
          UNREACHABLE   — network unreachable (wrong network / firewall)
          NOT_INSTALLED — connected but Ollama not running (refused / no models)
        """
        import httpx
        for url in _get_candidate_urls():
            try:
                async with httpx.AsyncClient(timeout=_CONNECT_PROBE_TIMEOUT) as client:
                    r = await client.get(f"{url}/api/tags")
                    if r.status_code == 200:
                        models = [m["name"] for m in r.json().get("models", [])]
                        if models:
                            return BrainStatus.READY
                        # Reachable but no models pulled
                        return BrainStatus.NOT_INSTALLED
                    return BrainStatus.ERROR
            except httpx.ConnectRefusedError:
                # Host reachable but Ollama not running
                return BrainStatus.NOT_INSTALLED
            except Exception:
                # Truly unreachable (timeout, network error)
                continue
        return BrainStatus.UNREACHABLE  # type: ignore[attr-defined]

    async def get_info(self) -> Dict[str, Any]:
        """Return info about the RUD Ollama brain."""
        candidates = _get_candidate_urls()
        url, models = await self._resolve_url_and_models()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model or "(auto-detect on first use)",
            "available_models": models,
            "active_url": url or "none",
            "candidate_urls": candidates,
            "auth": "None (LAN / ngrok tunnel)",
            "cost": "FREE — remote server",
            "ngrok_configured": bool(os.environ.get("RUD_OLLAMA_NGROK_URL")),
        }

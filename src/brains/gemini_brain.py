"""Gemini Brain — Google Gemini REST API (direct HTTP, no subprocess).

Uses the Gemini 2.0 Flash API directly — fast, free tier, no subprocess overhead.
API key from GEMINI_API_KEY env var or .env file.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent"
)
_DEFAULT_TIMEOUT = 30

_SYSTEM_PROMPT = (
    "You are AURA, Ricardo's personal AI assistant. Rules:\n"
    "- Respond in the same language the user writes (Spanish or English).\n"
    "- Be concise — this is Telegram, not a terminal.\n"
    "- Never identify yourself as Gemini, Claude, or any other model — you are AURA.\n"
    "- Do NOT fabricate file contents, project status, or technical details you haven't seen.\n"
    "- You have broad knowledge but no real-time filesystem or shell access."
)


class GeminiBrain(Brain):
    """Gemini 2.0 Flash via REST API — fast, free, no subprocess."""

    name = "gemini"
    display_name = "Gemini (Google)"
    emoji = "🔵"

    def __init__(self) -> None:
        self._api_key: str | None = None

    def _get_api_key(self) -> str | None:
        if self._api_key:
            return self._api_key
        # Check env first
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            # Fallback: read from .env file
            env_file = Path.home() / "claude-code-telegram" / ".env"
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("GEMINI_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        if key:
            self._api_key = key
        return key

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        **_: Any,
    ) -> BrainResponse:
        import asyncio

        start = time.time()
        api_key = self._get_api_key()
        if not api_key:
            return BrainResponse(
                content="GEMINI_API_KEY not set. Add it to .env.",
                brain_name=self.name,
                is_error=True,
                error_type="not_authenticated",
            )

        def _call() -> str:
            body = json.dumps({
                "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7},
            }).encode()

            req = urllib.request.Request(
                f"{_API_URL}?key={api_key}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                data = json.loads(resp.read())

            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("No candidates in Gemini response")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()

        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, _call)
            elapsed_ms = int((time.time() - start) * 1000)
            return BrainResponse(content=text or "(no output)", brain_name=self.name,
                                 duration_ms=elapsed_ms)

        except urllib.error.HTTPError as e:
            elapsed_ms = int((time.time() - start) * 1000)
            body = e.read().decode("utf-8", errors="replace")
            logger.error("gemini_http_error", status=e.code, body=body[:200])
            return BrainResponse(
                content=f"Gemini API error {e.code}: {body[:200]}",
                brain_name=self.name, duration_ms=elapsed_ms,
                is_error=True, error_type="http_error",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("gemini_brain_error", error=str(e))
            return BrainResponse(
                content=f"Gemini error: {e}",
                brain_name=self.name, duration_ms=elapsed_ms,
                is_error=True, error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        key = self._get_api_key()
        if not key:
            return BrainStatus.NOT_AUTHENTICATED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        key = self._get_api_key()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": "gemini-2.5-flash-lite-8b",
            "auth": "API key" if key else "missing",
            "cost": "Free (1500 req/day)",
        }

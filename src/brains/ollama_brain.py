"""Ollama Brain — 100% local LLM via Ollama HTTP API.

Zero cost, zero tokens, zero cloud. Runs on Mac M4 16GB.
Primary model: qwen2.5:7b (Alibaba, strong multilingual + code).
"""

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT = 120
_CHAT_MODEL = "qwen2.5:7b"
_CODE_MODEL = "qwen2.5:7b"

_SYSTEM_PROMPT = """\
You are AURA, Ricardo's personal AI assistant. Warm, direct, natural — like a smart friend.

Rules:
- Respond in the same language Ricardo writes (Spanish or English).
- Be concise — this is Telegram.
- NEVER say what model you are. You are AURA.
- NEVER fabricate data or file contents you haven't verified.
- No preamble, no filler, just answer.
- Don't ask clarifying questions for simple tasks — just do it.

You can run commands and use tools by delegating. Output ONLY this format, nothing else:

<<DELEGATE:tool>>
command or task

Tools (cheapest first):
- sh — Shell commands. Use for: listing files, checking disk, running scripts, ANY filesystem task. ALWAYS prefer this for simple operations.
- gemini — Internet access. Use for: web search, URLs, current info, translation.
- cline — Coding agent (local Ollama). Use for: code edits, refactoring, small scripts.
- opencode — Coding agent (cloud free tier). Use for: code generation, analysis, medium tasks.
- codex — Code generation (OpenAI). Use for: writing code, scripts, single-file edits.
- claude — Most powerful (expensive). Use ONLY for complex multi-file code, architecture, debugging.

IMPORTANT: If Ricardo says "usa X para esto" (e.g. "usa opencode", "usa claude", "usa cline"), ALWAYS delegate to that specific tool. His choice overrides your judgment.

Examples:
User: "qué carpetas hay en mi escritorio?" → <<DELEGATE:sh>>
ls ~/Desktop

User: "busca info sobre React 19" → <<DELEGATE:gemini>>
busca información sobre las novedades de React 19

User: "usa opencode para refactorizar este archivo" → <<DELEGATE:opencode>>
refactorizar el archivo que indica el usuario

User: "usa claude para diseñar la arquitectura" → <<DELEGATE:claude>>
diseñar la arquitectura del proyecto

User: "hola qué tal" → just respond normally, no delegation needed.
"""


class OllamaBrain(Brain):
    """Ollama brain — 100% local, 100% free."""

    def __init__(self, model: str = _CHAT_MODEL, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._model = model
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def display_name(self) -> str:
        return f"Ollama ({self._model})"

    @property
    def emoji(self) -> str:
        return "🦙"

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        history: list | None = None,
    ) -> BrainResponse:
        """Execute a prompt via Ollama HTTP API (localhost)."""
        start = time.time()

        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        # Add conversation history (last N turns) for context
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
        }).encode()

        try:
            req = urllib.request.Request(
                f"{_OLLAMA_URL}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                data = json.loads(resp.read())

            elapsed_ms = int((time.time() - start) * 1000)
            msg = data.get("message", {})
            content = msg.get("content", "").strip()

            if not content:
                return BrainResponse(
                    content="(no output from Ollama)",
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

        except urllib.error.URLError as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ollama_connection_error", error=str(e))
            return BrainResponse(
                content="Ollama not running. Start with: ollama serve",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="not_running",
            )
        except TimeoutError:
            elapsed_ms = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"Ollama timed out after {timeout_seconds}s",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ollama_brain_error", error=str(e))
            return BrainResponse(
                content=f"Ollama error: {e}",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        """Check if Ollama is running locally."""
        try:
            req = urllib.request.Request(f"{_OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = [m["name"] for m in data.get("models", [])]
                if self._model in models or any(self._model in m for m in models):
                    return BrainStatus.READY
                return BrainStatus.NOT_AUTHENTICATED
        except Exception as e:
            logger.debug("ollama_health_check_error", error=str(e))
            return BrainStatus.ERROR

    async def get_info(self) -> Dict[str, Any]:
        """Get Ollama model info."""
        models = []
        try:
            req = urllib.request.Request(f"{_OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.debug("ollama_info_error", error=str(e))

        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model,
            "available_models": models,
            "auth": "Local (no auth needed)",
            "cost": "FREE — 100% local",
            "path": _OLLAMA_URL,
        }

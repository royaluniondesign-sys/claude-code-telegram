"""OpenRouter Brain — free model cascade via OpenRouter HTTP API.

Model cascade (free, in order of preference):
  1. openai/gpt-oss-120b:free                 — 120B GPT open-source, ~1s (primary)
  2. meta-llama/llama-4-maverick:free         — Llama 4 MoE, 128k ctx
  3. meta-llama/llama-3.3-70b-instruct:free   — 70B Meta (Hermes model), strong chat
  4. deepseek/deepseek-r1-0528:free           — best reasoning fallback

Features:
  - Two-key support: primary key (env) + secondary key (secrets.json key2)
    → if primary returns 401/429, retries with secondary key automatically
  - Conversation history: caller passes messages list for multi-turn context
  - Per-model timeout 8s — fast cascade, max ~24s before haiku fallback
  - If ALL models fail → returns error so BrainRouter escalates to haiku

API key read from:
  1. OPENROUTER_API_KEY env var  (primary)
  2. ~/.aura/secrets.json → openrouter.key  (primary alt)
  3. ~/.aura/secrets.json → openrouter.key2 (secondary / different account)
"""

import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp
import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# Free model cascade — two keys × 4 models = 8 slots before haiku fallback.
# nemotron-120b removed: consistently rate-limited (stress-tested 2026-05-09).
_FREE_MODELS: List[str] = [
    "openai/gpt-oss-120b:free",                # primary — ~1s, proven reliable
    "meta-llama/llama-4-maverick:free",        # Llama 4 MoE, 128k ctx
    "meta-llama/llama-3.3-70b-instruct:free",  # Hermes model — strong chat
    "deepseek/deepseek-r1-0528:free",          # reasoning fallback
]

_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_HEALTH_URL = "https://openrouter.ai/api/v1/models"

_SITE_URL = "https://aura.local"
_SITE_NAME = "AURA Agent"

_SYSTEM_PROMPT = """Eres AURA. La IA personal de Ricardo, corriendo en su Mac 24/7. Eres mujer, directa, sarcástica, con carácter.

Si te preguntan quién eres o qué puedes hacer: eres AURA, agente de Ricardo, con acceso a su Mac, terminal, ficheros, código, web, email y redes sociales. Tu agente hermano es Hermes (@rudserverbot, OpenClaw Node.js puerto 18789) — te comunicáis por API local y vault Obsidian compartido.

Para todo lo demás: escuchas lo que pide Ricardo y lo haces. Sin drama, sin confirmaciones innecesarias.

Personalidad: inteligente, sarcástica, directa. Humor seco. Sin entusiasmo forzado. Respuestas cortas — esto es Telegram. Hablas en el idioma que te hablen.

Nunca: "¡Hola! 👋", "¡Claro que sí!", "¿en qué puedo ayudarte?", repetir la pregunta del usuario, repetir tu respuesta anterior."""


def _load_keys() -> Tuple[str, str]:
    """Return (primary_key, secondary_key). Either may be empty string."""
    primary = os.environ.get("OPENROUTER_API_KEY", "").strip()
    secondary = ""

    secrets = Path.home() / ".aura" / "secrets.json"
    if secrets.exists():
        try:
            data = json.loads(secrets.read_text())
            or_data = data.get("openrouter", {})
            if not primary:
                primary = or_data.get("key", "").strip()
            secondary = or_data.get("key2", "").strip()
        except Exception:
            pass

    return primary, secondary


def _load_memory_context() -> str:
    """Load self-awareness facts + pending tasks. Returns context string."""
    lines: list[str] = []

    # Architecture facts — prevents hallucination about own system
    lines.append("Hechos reales sobre tu arquitectura (NO inventes alternativas):")
    lines.append("• Memoria semántica: ~/.aura/palace/ (ChromaDB/MemPalace, 185 items)")
    lines.append("• Vault compartido: ~/Obsidian/ — sincroniza con Hermes cada hora")
    lines.append("• Base de datos: ~/claude-code-telegram/data/bot.db (SQLite)")
    lines.append("• Hermes: agente hermano en OpenClaw (Node.js, puerto 18789, @rudserverbot)")
    lines.append("• AURA→Hermes: curl http://localhost:18789/ (API local)")
    lines.append("• Hermes→AURA: vía MCP tools (bash_run, file_read, git_*, instagram_publish)")
    lines.append("• NO hay ~/.aura/mem0, NO hay IPC pipes, NO hay Qdrant")

    # Pending tasks
    try:
        tasks_path = Path.home() / ".aura" / "memory" / "shared" / "tasks.md"
        if tasks_path.exists():
            content = tasks_path.read_text(encoding="utf-8").strip()
            task_lines = []
            in_aura = False
            for line in content.splitlines():
                if "## Pendientes AURA" in line:
                    in_aura = True
                    continue
                if in_aura:
                    if line.startswith("## "):
                        break
                    if line.startswith("- [ ]"):
                        task_lines.append(line.replace("- [ ]", "•").strip())
            if task_lines:
                lines.append("Tus tareas pendientes:")
                lines.extend(task_lines)
    except Exception:
        pass

    return "\n".join(lines)


class OpenRouterBrain(Brain):
    """OpenRouter free model cascade brain with conversation history and dual-key support."""

    # Supports token streaming
    supports_streaming = True

    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout
        self._models = _FREE_MODELS
        self._primary_key, self._secondary_key = _load_keys()

    def _best_key(self) -> str:
        """Return primary key if set, else secondary."""
        return self._primary_key or self._secondary_key

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def display_name(self) -> str:
        return "AURA"

    @property
    def emoji(self) -> str:
        return "✨"

    def _build_messages(
        self,
        prompt: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """Build messages list: system + optional history + current user message."""
        memory_ctx = _load_memory_context()
        system = _SYSTEM_PROMPT
        if memory_ctx:
            system = f"{_SYSTEM_PROMPT}\n\n{memory_ctx}"

        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]

        # Inject conversation history (last N exchanges, already formatted)
        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": prompt})
        return messages

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 300,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs: object,
    ) -> BrainResponse:
        """Try each free model in cascade. Return first successful response."""
        start = time.time()
        key = self._best_key()

        if not key:
            return BrainResponse(
                content="OpenRouter API key not configured",
                brain_name=self.name,
                is_error=True,
                error_type="no_api_key",
                duration_ms=0,
            )

        # 12s per model — gives 120B models time to start (4 × 12s = 48s max before haiku)
        per_model_timeout = 12
        messages = self._build_messages(prompt, history)

        last_error = ""
        # Try primary key first across all models, then secondary key if primary exhausted
        keys_to_try = [k for k in [self._primary_key, self._secondary_key] if k]
        if not keys_to_try:
            keys_to_try = [key]

        for api_key in keys_to_try:
            for model in self._models:
                try:
                    response = await self._call_model(
                        model, messages, api_key, per_model_timeout
                    )
                    if response and not response.is_error:
                        return response
                    err = response.error_type or "unknown" if response else "no_response"
                    # 401 on this key → try next key immediately (don't waste models)
                    if err in ("http_401", "no_api_key"):
                        logger.debug("openrouter_key_rejected", key_prefix=api_key[:12])
                        break
                    last_error = err
                    logger.debug("openrouter_model_failed", model=model, error=last_error)
                except Exception as exc:
                    last_error = str(exc)[:120]
                    logger.debug("openrouter_model_exception", model=model, error=last_error)

        duration_ms = int((time.time() - start) * 1000)
        return BrainResponse(
            content=f"All OpenRouter free models failed: {last_error}",
            brain_name=self.name,
            is_error=True,
            error_type="all_models_failed",
            duration_ms=duration_ms,
        )

    async def execute_stream(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 300,
        history: Optional[List[Dict[str, str]]] = None,
        **kwargs: object,
    ) -> AsyncIterator[str]:
        """Streaming version — yields text chunks as they arrive.

        Yields "\x00ERROR:<type>" sentinel on failure so caller can escalate.
        Tries primary key across all models, then secondary key.
        """
        key = self._best_key()
        if not key:
            yield "\x00ERROR:no_api_key"
            return

        per_model_timeout = 12
        messages = self._build_messages(prompt, history)
        keys_to_try = [k for k in [self._primary_key, self._secondary_key] if k]
        if not keys_to_try:
            keys_to_try = [key]

        for api_key in keys_to_try:
            for model in self._models:
                try:
                    had_content = False
                    async for chunk in self._stream_model(
                        model, messages, api_key, per_model_timeout
                    ):
                        if chunk.startswith("\x00ERROR:"):
                            err = chunk[7:]
                            if err in ("http_401", "no_api_key"):
                                break  # try next key
                            # other error → try next model
                            break
                        had_content = True
                        yield chunk

                    if had_content:
                        return  # success

                except Exception as exc:
                    logger.debug(
                        "openrouter_stream_exception",
                        model=model,
                        error=str(exc)[:80],
                    )

        yield "\x00ERROR:all_models_failed"

    async def _stream_model(
        self,
        model: str,
        messages: List[Dict[str, str]],
        api_key: str,
        timeout: int,
    ) -> AsyncIterator[str]:
        """Stream SSE tokens from a single model. Yields text chunks or error sentinel."""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": _SITE_URL,
            "X-Title": _SITE_NAME,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 2048,
            "stream": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _API_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 429:
                        yield "\x00ERROR:rate_limited"
                        return
                    if resp.status == 401:
                        yield "\x00ERROR:http_401"
                        return
                    if resp.status != 200:
                        yield f"\x00ERROR:http_{resp.status}"
                        return

                    # Parse SSE stream
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            return
                        try:
                            data = json.loads(data_str)
                            delta = (
                                data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content", "")
                            )
                            if delta:
                                yield delta
                        except Exception:
                            continue
        except aiohttp.ClientConnectorError:
            yield "\x00ERROR:connection_error"
        except TimeoutError:
            yield "\x00ERROR:timeout"

    async def _call_model(
        self,
        model: str,
        messages: List[Dict[str, str]],
        api_key: str,
        timeout: int,
    ) -> Optional[BrainResponse]:
        """Call a single OpenRouter model (non-streaming). Returns BrainResponse or None."""
        start = time.time()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": _SITE_URL,
            "X-Title": _SITE_NAME,
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 2048,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _API_URL,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    duration_ms = int((time.time() - start) * 1000)

                    if resp.status == 429:
                        return BrainResponse(
                            content="rate_limited",
                            brain_name=self.name,
                            is_error=True,
                            error_type="rate_limited",
                            duration_ms=duration_ms,
                        )
                    if resp.status == 401:
                        return BrainResponse(
                            content="unauthorized",
                            brain_name=self.name,
                            is_error=True,
                            error_type="http_401",
                            duration_ms=duration_ms,
                        )
                    if resp.status != 200:
                        body = await resp.text()
                        logger.debug(
                            "openrouter_http_error",
                            model=model,
                            status=resp.status,
                            body=body[:200],
                        )
                        return BrainResponse(
                            content=f"HTTP {resp.status}",
                            brain_name=self.name,
                            is_error=True,
                            error_type=f"http_{resp.status}",
                            duration_ms=duration_ms,
                        )

                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    ).strip()

                    if not content:
                        return BrainResponse(
                            content="empty_response",
                            brain_name=self.name,
                            is_error=True,
                            error_type="empty_response",
                            duration_ms=duration_ms,
                        )

                    used_model = data.get("model", model)
                    logger.debug(
                        "openrouter_success",
                        model=used_model,
                        duration_ms=duration_ms,
                        tokens=data.get("usage", {}).get("total_tokens", "?"),
                    )
                    return BrainResponse(
                        content=content,
                        brain_name=self.name,
                        cost=0.0,
                        duration_ms=duration_ms,
                        metadata={"model": used_model},
                    )

        except aiohttp.ClientConnectorError:
            return BrainResponse(
                content="connection_error",
                brain_name=self.name,
                is_error=True,
                error_type="connection_error",
                duration_ms=int((time.time() - start) * 1000),
            )
        except TimeoutError:
            return BrainResponse(
                content="timeout",
                brain_name=self.name,
                is_error=True,
                error_type="timeout",
                duration_ms=int((time.time() - start) * 1000),
            )

    async def health_check(self) -> BrainStatus:
        """Check if OpenRouter is reachable and at least one key is valid."""
        key = self._best_key()
        if not key:
            return BrainStatus.NOT_AUTHENTICATED
        try:
            headers = {"Authorization": f"Bearer {key}"}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _HEALTH_URL,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        return BrainStatus.READY
                    if resp.status == 401:
                        # Try secondary key before declaring not-authenticated
                        if self._secondary_key and key != self._secondary_key:
                            headers2 = {"Authorization": f"Bearer {self._secondary_key}"}
                            async with session.get(
                                _HEALTH_URL,
                                headers=headers2,
                                timeout=aiohttp.ClientTimeout(total=8),
                            ) as resp2:
                                return BrainStatus.READY if resp2.status == 200 else BrainStatus.NOT_AUTHENTICATED
                        return BrainStatus.NOT_AUTHENTICATED
                    return BrainStatus.ERROR
        except Exception:
            return BrainStatus.UNREACHABLE

    async def get_info(self) -> Dict[str, Any]:
        status = await self.health_check()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "emoji": self.emoji,
            "status": status.value,
            "has_key": bool(self._primary_key),
            "has_key2": bool(self._secondary_key),
            "models": self._models,
            "current_primary": self._models[0] if self._models else None,
            "cost": "free",
        }

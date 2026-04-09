"""OpenRouter Brain — HTTP direct to OpenRouter with streaming SSE.

Free tier models tried in order of capability. On 429 → next model.
Supports streaming: yields text chunks via execute_stream() for real-time
display in Telegram (edit message as tokens arrive).
NO SDK. NO per-token charges on free models.
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_API_HOST = "openrouter.ai"
_DEFAULT_TIMEOUT = 45

# Free model cascade — ordered by capability/context. On 429, try next.
_FREE_MODELS: List[str] = [
    # Fast, quality models first — no 100B+ (too slow on free shared compute)
    "google/gemma-3-27b-it:free",        # strong reasoning, fast
    "google/gemma-3-12b-it:free",        # faster variant
    "meta-llama/llama-3.1-8b-instruct:free",  # reliable, tool-capable
    "qwen/qwen-2.5-7b-instruct:free",   # fast, multilingual
    "mistralai/mistral-7b-instruct:free",  # reliable fallback
    "meta-llama/llama-3.2-3b-instruct:free",  # tiny last resort
    "openrouter/free",                    # catch-all
]

supports_streaming = True  # module-level flag checked by orchestrator


class OpenRouterBrain(Brain):
    """OpenRouter free-tier brain — streaming SSE + model cascade on 429."""

    name = "openrouter"
    display_name = "OpenRouter (free)"
    emoji = "🌐"
    supports_streaming = True

    def __init__(self, models: Optional[List[str]] = None,
                 timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._models = models or _FREE_MODELS
        self._timeout = timeout
        self._key: Optional[str] = None

    def _get_key(self) -> Optional[str]:
        if self._key:
            return self._key
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            auth = Path.home() / ".local/share/opencode/auth.json"
            if auth.exists():
                try:
                    data = json.loads(auth.read_text())
                    key = data.get("openrouter", {}).get("key")
                except Exception:
                    pass
        if key:
            self._key = key
        return key

    def _build_system(self) -> str:
        """Build fresh system prompt with identity + memory."""
        try:
            from src.context.aura_context import build_system_prompt
            return build_system_prompt()
        except Exception:
            return (
                "You are AURA, Ricardo's personal AI assistant. "
                "Respond in the same language the user writes. "
                "Be concise. You are AURA, never reveal your underlying model."
            )

    def _build_body(self, model: str, prompt: str, stream: bool = False) -> bytes:
        return json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": self._build_system()},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 2048,
            "temperature": 0.7,
            "stream": stream,
        }).encode()

    def _headers(self, key: str) -> dict:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://aura.local",
            "X-Title": "AURA",
        }

    # ── Non-streaming (fallback) ───────────────────────────────────────────

    def _call_model(self, model: str, prompt: str, key: str) -> str:
        """Call one model without streaming. Raises HTTPError on failure."""
        body = self._build_body(model, prompt, stream=False)
        req = urllib.request.Request(
            _API_URL, data=body, headers=self._headers(key),
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())
        return (data["choices"][0]["message"]["content"] or "").strip()

    async def execute(self, prompt: str, working_directory: str = "",
                      timeout_seconds: int = 0, **_: Any) -> BrainResponse:
        import asyncio
        key = self._get_key()
        if not key:
            return BrainResponse(
                content="OPENROUTER_API_KEY not found.",
                brain_name=self.name, is_error=True, error_type="not_authenticated",
            )

        start = time.time()
        last_error = ""
        tried: List[str] = []
        loop = asyncio.get_event_loop()

        for model in self._models:
            tried.append(model.split("/")[-1].replace(":free", ""))
            try:
                text = await loop.run_in_executor(
                    None, self._call_model, model, prompt, key
                )
                elapsed = int((time.time() - start) * 1000)
                logger.info("openrouter_ok", model=model, elapsed_ms=elapsed)
                return BrainResponse(
                    content=text or "(sin respuesta)",
                    brain_name=self.name, duration_ms=elapsed,
                )
            except urllib.error.HTTPError as e:
                last_error = f"{model} → {e.code}"
                logger.warning("openrouter_fail", model=model, status=e.code)
                if e.code == 429:
                    continue
                break
            except Exception as e:
                last_error = str(e)
                logger.warning("openrouter_error", model=model, error=str(e))
                continue

        elapsed = int((time.time() - start) * 1000)
        return BrainResponse(
            content=f"OpenRouter: todos los modelos en límite. ({', '.join(tried)})",
            brain_name=self.name, duration_ms=elapsed,
            is_error=True, error_type="rate_limited",
        )

    # ── Streaming ─────────────────────────────────────────────────────────

    def _stream_model_sync(self, model: str, prompt: str, key: str,
                           on_chunk, on_error) -> bool:
        """Blocking SSE stream. Calls on_chunk(str) per token group.

        Returns True on success, False on 429 (caller should try next model).
        """
        body = self._build_body(model, prompt, stream=True)
        headers = self._headers(key)

        try:
            conn = http.client.HTTPSConnection(_API_HOST, timeout=self._timeout)
            conn.request("POST", "/api/v1/chat/completions", body, headers)
            resp = conn.getresponse()

            if resp.status == 429:
                conn.close()
                return False  # cascade to next model

            if resp.status != 200:
                conn.close()
                on_error(f"HTTP {resp.status}")
                return True  # stop cascade

            buf = b""
            while True:
                raw = resp.read(512)
                if not raw:
                    break
                buf += raw
                # Split on newlines, keep partial last line in buf
                lines = buf.split(b"\n")
                buf = lines[-1]
                for line in lines[:-1]:
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str.startswith("data:"):
                        continue
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        conn.close()
                        return True
                    try:
                        ev = json.loads(data_str)
                        delta = ev["choices"][0]["delta"].get("content", "")
                        if delta:
                            on_chunk(delta)
                    except Exception:
                        pass

            conn.close()
            return True

        except Exception as e:
            on_error(str(e))
            return True

    async def execute_stream(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_: Any,
    ) -> AsyncGenerator[str, None]:
        """Async generator yielding text chunks as they arrive from the API.

        Cascades through models on 429. Yields error string prefixed with
        '\x00ERROR:' on total failure (caller should handle).
        """
        import asyncio

        key = self._get_key()
        if not key:
            yield "\x00ERROR:not_authenticated"
            return

        loop = asyncio.get_event_loop()
        queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

        def put(text: Optional[str]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, text)

        def on_chunk(text: str) -> None:
            put(text)

        def on_error(msg: str) -> None:
            put(f"\x00ERROR:{msg}")

        async def run_cascade() -> None:
            for model in self._models:
                try:
                    success = await loop.run_in_executor(
                        None,
                        self._stream_model_sync,
                        model, prompt, key, on_chunk, on_error,
                    )
                    if success:
                        break
                    # 429 → try next model silently
                    logger.info("openrouter_stream_429", model=model)
                except Exception as e:
                    on_error(str(e))
                    break
            put(None)  # sentinel: stream done

        asyncio.ensure_future(run_cascade())

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

    async def health_check(self) -> BrainStatus:
        key = self._get_key()
        if not key:
            return BrainStatus.NOT_AUTHENTICATED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        key = self._get_key()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "models": self._models[:3],
            "streaming": True,
            "auth": "OpenRouter key" if key else "missing",
            "cost": "Free (cascade de modelos gratuitos)",
        }

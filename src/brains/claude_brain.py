"""Claude Brain — wraps the `claude` CLI (subscription auth, no API key).

Haiku  → cheap first layer  (~$0.001/msg): CHAT, DEEP, simple queries
Sonnet → main agent layer   (~$0.01/msg):  CODE, complex tasks with tools
Opus   → deep reasoning     (~$0.05/msg):  architecture, max capability

All tiers run via `claude -p "..." --model X --output-format text`, which
gives Claude Code full tool access (Read, Write, Bash, etc.) without SDK setup.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_CLI_FALLBACK_PATHS = [
    "/Users/oxyzen/.local/bin/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
]

# Model aliases (Claude Code supports short aliases)
_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

_DEFAULT_TIMEOUT = 120  # Haiku is fast; Sonnet/Opus may need more


def _find_claude_cli() -> Optional[str]:
    path = shutil.which("claude")
    if path:
        return path
    return next((p for p in _CLI_FALLBACK_PATHS if Path(p).exists()), None)


class ClaudeBrain(Brain):
    """Claude Code CLI brain — runs claude -p non-interactively.

    Uses subscription auth (no API key). Supports model escalation.
    """

    def __init__(self, model: str = "haiku", timeout: int = _DEFAULT_TIMEOUT) -> None:
        alias = _MODEL_ALIASES.get(model, model)
        self._model = alias
        self._model_alias = model
        self._timeout = timeout
        self._cli_path = _find_claude_cli()

    @property
    def name(self) -> str:
        return f"claude-{self._model_alias}"

    @property
    def display_name(self) -> str:
        names = {"haiku": "Claude Haiku", "sonnet": "Claude Sonnet", "opus": "Claude Opus"}
        return names.get(self._model_alias, f"Claude ({self._model_alias})")

    @property
    def emoji(self) -> str:
        emojis = {"haiku": "🟡", "sonnet": "🟠", "opus": "🔴"}
        return emojis.get(self._model_alias, "🤖")

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_kwargs: Any,
    ) -> BrainResponse:
        """Run claude -p prompt --model X --output-format text."""
        if not self._cli_path:
            return BrainResponse(
                content="claude CLI not found. Run: brew install claude",
                brain_name=self.name,
                is_error=True,
                error_type="cli_not_found",
            )

        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()

        cmd = [
            self._cli_path,
            "-p", prompt,
            "--model", self._model,
            "--output-format", "text",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = int((time.time() - start) * 1000)

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                error_msg = err or out or f"claude exited {proc.returncode}"
                logger.warning(
                    "claude_brain_nonzero",
                    model=self._model_alias,
                    returncode=proc.returncode,
                    stderr=err[:200],
                )
                return BrainResponse(
                    content=f"❌ {self.display_name} error:\n{error_msg[:500]}",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="nonzero_exit",
                )

            if not out:
                return BrainResponse(
                    content="(sin respuesta)",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="empty_output",
                )

            logger.info(
                "claude_brain_ok",
                model=self._model_alias,
                duration_ms=elapsed,
                output_len=len(out),
            )
            return BrainResponse(
                content=out,
                brain_name=self.name,
                duration_ms=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"⏱️ {self.display_name} timeout ({timeout}s)",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("claude_brain_error", model=self._model_alias, error=str(e))
            return BrainResponse(
                content=f"❌ {self.display_name}: {e}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        if not self._cli_path:
            return BrainStatus.NOT_INSTALLED
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                return BrainStatus.READY
            return BrainStatus.ERROR
        except Exception:
            return BrainStatus.ERROR

    async def get_info(self) -> Dict[str, Any]:
        version = "?"
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path or "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip().split("\n")[0]
        except Exception:
            pass

        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": self._model,
            "model_alias": self._model_alias,
            "version": version,
            "cli_path": self._cli_path or "not found",
            "auth": "Subscription (no API key)",
            "cost": {"haiku": "~$0/msg (Max plan)", "sonnet": "~$0/msg (Max plan)", "opus": "~$0/msg (Max plan)"}.get(self._model_alias, "Subscription"),
            "timeout_s": self._timeout,
        }

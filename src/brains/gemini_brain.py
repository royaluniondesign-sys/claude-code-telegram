"""Gemini Brain — Google Gemini CLI (gemini -p, --approval-mode yolo).

Uses the installed `gemini` CLI non-interactively with --approval-mode yolo
so it never hangs waiting for tool confirmations.

MCP startup bypass: GEMINI_CLI_HOME=/tmp/gemini_no_mcp skips the full
~/.gemini MCP server list, cutting startup from 80s → ~2s.

Falls back gracefully on timeout or error for escalation to Claude.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# Strip ANSI escape codes from CLI output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFJA-Za-z]|\x1b[()][AB012]")

_DEFAULT_TIMEOUT = 45  # was 30s — increased now that MCP overhead is removed

_CLI_FALLBACK_PATHS = [
    "/opt/homebrew/bin/gemini",
    "/usr/local/bin/gemini",
]

# Minimal Gemini home without MCP servers — prevents 80s startup overhead
_GEMINI_NO_MCP_HOME = Path("/tmp/gemini_no_mcp")


def _ensure_fast_gemini_home() -> None:
    """Create minimal ~/.gemini config at _GEMINI_NO_MCP_HOME (no MCP servers)."""
    config_dir = _GEMINI_NO_MCP_HOME / ".gemini"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps({
            "general": {"sessionRetention": {"enabled": False}},
            "security": {"auth": {"selectedType": "oauth-personal"}},
        }))
    # Copy OAuth credentials so gemini can authenticate
    real_gemini = Path.home() / ".gemini"
    for cred_file in ("oauth_creds.json", "google_accounts.json"):
        src = real_gemini / cred_file
        dst = config_dir / cred_file
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())


# One-time setup at import
try:
    _ensure_fast_gemini_home()
except Exception:
    pass

_GEMINI_FAST_ENV = {**os.environ, "GEMINI_CLI_HOME": str(_GEMINI_NO_MCP_HOME)}


def _find_gemini() -> Optional[str]:
    path = shutil.which("gemini")
    if path:
        return path
    return next((p for p in _CLI_FALLBACK_PATHS if Path(p).exists()), None)


class GeminiBrain(Brain):
    """Gemini CLI brain — gemini -p <prompt> --approval-mode yolo -o text."""

    name = "gemini"
    display_name = "Gemini (Google)"
    emoji = "🔵"

    def __init__(self, timeout: int = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._cli_path = _find_gemini()

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_: Any,
    ) -> BrainResponse:
        if not self._cli_path:
            return BrainResponse(
                content="gemini CLI not found. Install: npm i -g @google/gemini-cli",
                brain_name=self.name,
                is_error=True,
                error_type="not_installed",
            )

        # Hard-cap: Gemini CLI is an agentic tool that can run for minutes.
        # Never let it exceed _DEFAULT_TIMEOUT regardless of what the caller wants.
        timeout = min(timeout_seconds, self._timeout) if timeout_seconds else self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()

        # Prepend AURA identity + memory as context prefix (CLI has no system channel)
        try:
            from src.context.aura_context import build_system_prompt
            context_prefix = build_system_prompt()
            full_prompt = f"[CONTEXTO DE AURA]\n{context_prefix}\n\n[TAREA]\n{prompt}"
        except Exception:
            full_prompt = prompt

        cmd = [
            self._cli_path,
            "-p", full_prompt,
            "--approval-mode", "yolo",
            "-o", "text",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=_GEMINI_FAST_ENV,  # bypass MCP startup (~80s → ~2s)
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                elapsed = int((time.time() - start) * 1000)
                logger.warning("gemini_timeout", timeout=timeout)
                return BrainResponse(
                    content=f"⏱ Gemini timeout ({timeout}s)",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="timeout",
                )

            elapsed = int((time.time() - start) * 1000)
            out = _ANSI_RE.sub("", stdout.decode("utf-8", errors="replace")).strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0 and not out:
                is_rl = "429" in err or "quota" in err.lower() or "rate" in err.lower()
                logger.warning(
                    "gemini_nonzero",
                    returncode=proc.returncode,
                    stderr=err[:200],
                    is_rate_limited=is_rl,
                )
                return BrainResponse(
                    content=f"Gemini error: {err[:300] or f'exit {proc.returncode}'}",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="rate_limited" if is_rl else "nonzero_exit",
                )

            if not out:
                out = "(sin respuesta)"

            logger.info("gemini_ok", elapsed_ms=elapsed, output_len=len(out))
            return BrainResponse(content=out, brain_name=self.name, duration_ms=elapsed)

        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.error("gemini_brain_error", error=str(e))
            return BrainResponse(
                content=f"Gemini error: {e}",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        if not self._cli_path:
            return BrainStatus.NOT_INSTALLED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "model": "gemini-2.5-flash (CLI default)",
            "cli_path": self._cli_path or "not found",
            "auth": "Google account (gemini CLI auth)",
            "cost": "Free (Google Gemini Code Assist)",
        }

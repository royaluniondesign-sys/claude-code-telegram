"""Gemini Brain — Google Gemini CLI wrapper.

Uses `gemini` CLI non-interactively via subprocess with --prompt flag.
Authenticated via `gemini` login (Google account, 1000 req/day free).
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_DEFAULT_TIMEOUT = 300


class GeminiBrain(Brain):
    """Gemini brain — executes prompts via Gemini CLI."""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Gemini (Google)"

    @property
    def emoji(self) -> str:
        return "🔵"

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        **_: Any,
    ) -> BrainResponse:
        """Execute a prompt via Gemini CLI."""
        start = time.time()

        gemini_path = _find_gemini()
        if not gemini_path:
            return BrainResponse(
                content="Gemini CLI not installed. Run: npm install -g @google/gemini-cli",
                brain_name=self.name,
                is_error=True,
                error_type="not_installed",
            )

        try:
            # Gemini CLI accepts prompts via stdin in non-interactive mode
            # Using -p flag for prompt or piping stdin
            full_prompt = (
                "You are AURA, Ricardo's personal AI assistant. Rules:\n"
                "- Respond in the same language the user writes (Spanish or English).\n"
                "- Be concise — this is Telegram.\n"
                "- Never identify yourself as Gemini, Claude, or any other model — you are AURA.\n"
                "- CRITICAL: NEVER fabricate file contents, project status, system state, or technical details. "
                "If you haven't actually read a file or checked a system, say you don't have that information. "
                "Do NOT invent names of agents, skills counts, project structures, or version numbers.\n"
                "- You have internet access for search and web tasks. You do NOT have filesystem access.\n"
                f"\nUser: {prompt}"
            )
            proc = await asyncio.create_subprocess_exec(
                gemini_path, "-p", full_prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                env=_build_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )

            elapsed_ms = int((time.time() - start) * 1000)
            output = stdout.decode().strip()
            err = stderr.decode().strip()

            if proc.returncode != 0:
                error_msg = err or output or f"Gemini exited with code {proc.returncode}"
                if "login" in error_msg.lower() or "auth" in error_msg.lower():
                    return BrainResponse(
                        content="Gemini not authenticated. Run `gemini` from terminal to login.",
                        brain_name=self.name,
                        duration_ms=elapsed_ms,
                        is_error=True,
                        error_type="not_authenticated",
                    )
                return BrainResponse(
                    content=f"Gemini error: {error_msg}",
                    brain_name=self.name,
                    duration_ms=elapsed_ms,
                    is_error=True,
                    error_type="execution_error",
                )

            content = output if output else "(no output)"
            return BrainResponse(
                content=content,
                brain_name=self.name,
                duration_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            proc.kill()
            elapsed_ms = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"Gemini timed out after {timeout_seconds}s",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("gemini_brain_error", error=str(e))
            return BrainResponse(
                content=f"Gemini error: {e}",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        """Check Gemini CLI availability."""
        gemini_path = _find_gemini()
        if not gemini_path:
            return BrainStatus.NOT_INSTALLED

        try:
            proc = await asyncio.create_subprocess_exec(
                gemini_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_env(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return BrainStatus.READY
            return BrainStatus.NOT_AUTHENTICATED
        except Exception as e:
            logger.debug("gemini_health_check_error", error=str(e))
            return BrainStatus.ERROR

    async def get_info(self) -> Dict[str, Any]:
        """Get Gemini version and auth info."""
        gemini_path = _find_gemini()
        version = "not installed"

        if gemini_path:
            try:
                proc = await asyncio.create_subprocess_exec(
                    gemini_path, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_build_env(),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                version = stdout.decode().strip() if stdout else "unknown"
            except Exception as e:
                logger.debug("gemini_version_error", error=str(e))
                version = "error"

        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": version,
            "auth": "Google account (1000 req/day free)",
            "path": gemini_path or "not found",
        }


def _extended_path() -> str:
    """Get PATH with common bin directories included."""
    extra = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")
    return f"{extra}:{os.environ.get('PATH', '')}"


def _find_gemini() -> str | None:
    """Find gemini binary with extended PATH."""
    return shutil.which("gemini", path=_extended_path())


def _build_env() -> dict:
    """Build environment for Gemini subprocess."""
    env = os.environ.copy()
    env["PATH"] = _extended_path()
    # Load GEMINI_API_KEY from .env if not already in environment
    if "GEMINI_API_KEY" not in env:
        env_file = Path.home() / "claude-code-telegram" / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    env["GEMINI_API_KEY"] = line.split("=", 1)[1].strip()
                    break
    return env

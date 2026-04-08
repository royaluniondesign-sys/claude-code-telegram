"""Perplexity Brain — Perplexity AI search-augmented LLM.

Uses pplx-cli (PyPI) via subprocess.
Authenticated via PERPLEXITY_API_KEY env var.
Pro subscribers get $5/month API credits included.
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

_DEFAULT_TIMEOUT = 120


class PerplexityBrain(Brain):
    """Perplexity brain — search-augmented AI via pplx-cli."""

    @property
    def name(self) -> str:
        return "perplexity"

    @property
    def display_name(self) -> str:
        return "Perplexity (Search AI)"

    @property
    def emoji(self) -> str:
        return "🟣"

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = _DEFAULT_TIMEOUT,
    ) -> BrainResponse:
        """Execute a prompt via pplx-cli."""
        start = time.time()

        perplexity_path = shutil.which("perplexity")
        if not perplexity_path:
            return BrainResponse(
                content="Perplexity CLI not installed. Run: uv tool install pplx-cli",
                brain_name=self.name,
                is_error=True,
                error_type="not_installed",
            )

        api_key = os.environ.get("PERPLEXITY_API_KEY", "")
        if not api_key:
            return BrainResponse(
                content=(
                    "PERPLEXITY_API_KEY not set.\n"
                    "1. Go to perplexity.ai/settings → API\n"
                    "2. Generate key (Pro includes $5/mo credits)\n"
                    "3. Add to ~/.zshrc: export PERPLEXITY_API_KEY=pplx-..."
                ),
                brain_name=self.name,
                is_error=True,
                error_type="not_authenticated",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                perplexity_path, "ask", prompt,
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
                error_msg = err or output or f"pplx exited with code {proc.returncode}"
                return BrainResponse(
                    content=f"Perplexity error: {error_msg}",
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
            elapsed_ms = int((time.time() - start) * 1000)
            return BrainResponse(
                content=f"Perplexity timed out after {timeout_seconds}s",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type="timeout",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("perplexity_brain_error", error=str(e))
            return BrainResponse(
                content=f"Perplexity error: {e}",
                brain_name=self.name,
                duration_ms=elapsed_ms,
                is_error=True,
                error_type=type(e).__name__,
            )

    async def health_check(self) -> BrainStatus:
        """Check Perplexity CLI and API key."""
        perplexity_path = shutil.which("perplexity")
        if not perplexity_path:
            return BrainStatus.NOT_INSTALLED

        api_key = os.environ.get("PERPLEXITY_API_KEY", "")
        if not api_key:
            return BrainStatus.NOT_AUTHENTICATED

        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        """Get Perplexity CLI info."""
        perplexity_path = shutil.which("perplexity")
        version = "not installed"

        if perplexity_path:
            try:
                proc = await asyncio.create_subprocess_exec(
                    perplexity_path, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                version = stdout.decode().strip() if stdout else "unknown"
            except Exception as e:
                logger.debug("perplexity_version_error", error=str(e))
                version = "installed"

        has_key = bool(os.environ.get("PERPLEXITY_API_KEY", ""))

        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": version,
            "auth": "API key (Pro includes $5/mo credits)" if has_key else "API key needed",
            "path": perplexity_path or "not found",
        }


def _build_env() -> dict:
    """Build environment for pplx subprocess."""
    env = os.environ.copy()
    extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin")]
    current_path = env.get("PATH", "")
    for p in extra_paths:
        if p not in current_path:
            current_path = f"{p}:{current_path}"
    env["PATH"] = current_path
    return env

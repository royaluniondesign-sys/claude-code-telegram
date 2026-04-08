"""Executor Brains — direct CLI wrappers for sub-executor tools.

These expose opencode, cline, and codex as Brain instances so the router
can explicitly route to them when Ricardo says "usa opencode/cline/codex".

All are non-interactive (no prompts, no user input required).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")


def _env_with_path() -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{_EXTRA_PATH}:{env.get('PATH', '')}"
    return env


async def _run(args: list, cwd: str, timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=_env_with_path(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"timeout after {timeout}s"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


class OpenCodeBrain(Brain):
    """opencode — free tier via OpenRouter. Code gen and analysis."""

    name = "opencode"
    display_name = "OpenCode (free)"
    emoji = "🔶"

    def __init__(self, timeout: int = 300) -> None:
        self._timeout = timeout
        self._cli = shutil.which("opencode", path=_EXTRA_PATH)

    async def execute(self, prompt: str, working_directory: str = "",
                      timeout_seconds: int = 0, **_: Any) -> BrainResponse:
        if not self._cli:
            return BrainResponse(content="opencode not installed", brain_name=self.name,
                                 is_error=True, error_type="not_installed")
        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()
        # opencode run <message> — non-interactive, exits when done
        rc, out, err = await _run([self._cli, "run", prompt], cwd, timeout)
        elapsed = int((time.time() - start) * 1000)
        content = out or err or "no output"
        # opencode writes progress to stderr, result to stdout
        if rc != 0 and not out:
            return BrainResponse(content=content, brain_name=self.name,
                                 duration_ms=elapsed, is_error=True, error_type="nonzero_exit")
        # If stdout empty but stderr has content, use stderr (opencode logs there)
        if not out and err:
            content = err
        return BrainResponse(content=content, brain_name=self.name, duration_ms=elapsed)

    async def health_check(self) -> BrainStatus:
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name,
                "cli": self._cli or "not found", "cost": "Free (OpenRouter)"}


class ClineBrain(Brain):
    """cline — local Ollama (qwen2.5:7b). Code edits, refactoring. Zero cost."""

    name = "cline"
    display_name = "Cline (local Ollama)"
    emoji = "🟣"

    def __init__(self, model: str = "qwen2.5:7b", timeout: int = 300) -> None:
        self._model = model
        self._timeout = timeout
        self._cli = shutil.which("cline", path=_EXTRA_PATH)

    async def execute(self, prompt: str, working_directory: str = "",
                      timeout_seconds: int = 0, **_: Any) -> BrainResponse:
        if not self._cli:
            return BrainResponse(content="cline not installed", brain_name=self.name,
                                 is_error=True, error_type="not_installed")
        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()
        # cline -m qwen2.5:7b -a "prompt" -y  (act mode + yolo = non-interactive)
        rc, out, err = await _run(
            [self._cli, "-m", self._model, "-a", prompt, "-y"], cwd, timeout
        )
        elapsed = int((time.time() - start) * 1000)
        content = out or err or "no output"
        is_error = rc != 0 and not out
        return BrainResponse(content=content, brain_name=self.name,
                             duration_ms=elapsed, is_error=is_error)

    async def health_check(self) -> BrainStatus:
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        # Check Ollama is running
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=3):
                return BrainStatus.READY
        except Exception:
            return BrainStatus.NOT_AUTHENTICATED  # cline present but Ollama down

    async def get_info(self) -> Dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name,
                "cli": self._cli or "not found", "model": self._model,
                "cost": "Free (local Ollama)"}


class CodexBrain(Brain):
    """codex — OpenAI subscription. Fast single-file code generation."""

    name = "codex"
    display_name = "Codex (OpenAI)"
    emoji = "🟢"

    def __init__(self, timeout: int = 180) -> None:
        self._timeout = timeout
        self._cli = shutil.which("codex", path=_EXTRA_PATH)

    async def execute(self, prompt: str, working_directory: str = "",
                      timeout_seconds: int = 0, **_: Any) -> BrainResponse:
        if not self._cli:
            return BrainResponse(content="codex not installed", brain_name=self.name,
                                 is_error=True, error_type="not_installed")
        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()
        # codex exec <prompt> --full-auto (workspace-write sandbox, no confirmation)
        # --skip-git-repo-check: allow running outside git repos
        # -C <dir>: set working directory for the agent
        rc, out, err = await _run(
            [self._cli, "exec", prompt, "--full-auto",
             "--skip-git-repo-check", "-C", cwd],
            cwd, timeout,
        )
        elapsed = int((time.time() - start) * 1000)
        content = out or err or "no output"
        if rc != 0 and not out:
            return BrainResponse(content=content, brain_name=self.name,
                                 duration_ms=elapsed, is_error=True, error_type="nonzero_exit")
        return BrainResponse(content=content, brain_name=self.name, duration_ms=elapsed)

    async def health_check(self) -> BrainStatus:
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name,
                "cli": self._cli or "not found", "cost": "OpenAI subscription"}

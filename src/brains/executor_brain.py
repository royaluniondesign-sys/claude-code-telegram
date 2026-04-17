"""Executor Brains — direct CLI wrappers for sub-executor tools.

These expose opencode, cline, and codex as Brain instances so the router
can explicitly route to them when Ricardo says "usa opencode/cline/codex".

All are non-interactive (no prompts, no user input required).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
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


def execute_command(command: str) -> Optional[str]:
    """Execute a shell command with comprehensive file operation error handling.

    Args:
        command: Shell command to execute.

    Returns:
        Command stdout on success, None on error.
    """
    try:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise Exception(f"Command execution failed: {stderr.decode()}")
        return stdout.decode()
    except FileNotFoundError as e:
        logging.error(f"File not found error: {e}")
    except PermissionError as e:
        logging.error(f"Permission error: {e}")
    except IsADirectoryError as e:
        logging.error(f"Is a directory error: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
    return None


async def _validate_working_directory(cwd: str) -> tuple[bool, str]:
    """Validate that working directory exists and is accessible."""
    try:
        path = Path(cwd)
        if not path.exists():
            return False, f"Working directory does not exist: {cwd}"
        if not path.is_dir():
            return False, f"Path is not a directory: {cwd}"
        if not os.access(cwd, os.R_OK):
            return False, f"No read permission for directory: {cwd}"
        return True, ""
    except (OSError, ValueError) as e:
        return False, f"Error validating directory: {str(e)}"


async def _run(args: list, cwd: str, timeout: int) -> tuple[int, str, str]:
    """Execute subprocess with enhanced error detection in file operations."""
    # Validate working directory before execution
    valid, error_msg = await _validate_working_directory(cwd)
    if not valid:
        logger.error(f"Working directory validation failed: {error_msg}")
        return -1, "", error_msg

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_env_with_path(),
        )
    except FileNotFoundError as e:
        error_msg = f"Command not found: {str(e)}"
        logger.error(f"File not found error: {error_msg}")
        return -1, "", error_msg
    except PermissionError as e:
        error_msg = f"Permission denied: {str(e)}"
        logger.error(f"Permission error: {error_msg}")
        return -1, "", error_msg
    except OSError as e:
        error_msg = f"OS error during subprocess creation: {str(e)}"
        logger.error(f"OS error: {error_msg}")
        return -1, "", error_msg

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception as e:
            logger.error(f"Failed to kill process after timeout: {str(e)}")
        return -1, "", f"timeout after {timeout}s"
    except Exception as e:
        logger.error(f"Unexpected error during process communication: {str(e)}")
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"Process communication error: {str(e)}"

    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


class OpenCodeBrain(Brain):
    """opencode — OpenRouter (qwen3-235b). Code gen and analysis."""

    name = "opencode"
    display_name = "OpenCode (OpenRouter)"
    emoji = "🔶"

    # big-pickle = native OpenCode/ZED cloud model, free tokens included
    _MODEL = "opencode/big-pickle"

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

        # Detect file operation errors early
        if cwd and not Path(cwd).exists():
            error_msg = f"Working directory does not exist: {cwd}"
            logger.error(error_msg)
            return BrainResponse(content=error_msg, brain_name=self.name,
                                 is_error=True, error_type="file_not_found")

        start = time.time()
        # Pass -m explicitly so no session cache or config override can change the model
        rc, out, err = await _run([self._cli, "run", "-m", self._MODEL, prompt], cwd, timeout)
        elapsed = int((time.time() - start) * 1000)
        content = out or err or "no output"

        # Classify file operation errors
        error_type = None
        if rc == -1:  # Error from _run validation
            if "does not exist" in err or "No such file" in err:
                error_type = "file_not_found"
            elif "Permission denied" in err or "No read permission" in err:
                error_type = "permission_denied"
            elif "timeout" in err:
                error_type = "timeout"
            else:
                error_type = "file_operation_error"
            return BrainResponse(content=content, brain_name=self.name,
                                 duration_ms=elapsed, is_error=True, error_type=error_type)

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

    def __init__(self, model: str = "qwen2.5:7b", timeout: int = 60) -> None:
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

        # Detect file operation errors early
        if cwd and not Path(cwd).exists():
            error_msg = f"Working directory does not exist: {cwd}"
            logger.error(error_msg)
            return BrainResponse(content=error_msg, brain_name=self.name,
                                 is_error=True, error_type="file_not_found")

        start = time.time()
        # cline -m qwen2.5:7b -a "prompt" -y  (act mode + yolo = non-interactive)
        rc, out, err = await _run(
            [self._cli, "-m", self._model, "-a", prompt, "-y"], cwd, timeout
        )
        elapsed = int((time.time() - start) * 1000)
        content = out or err or "no output"

        # Classify file operation errors
        error_type = None
        if rc == -1:  # Error from _run validation
            if "does not exist" in err or "No such file" in err:
                error_type = "file_not_found"
            elif "Permission denied" in err or "No read permission" in err:
                error_type = "permission_denied"
            elif "timeout" in err:
                error_type = "timeout"
            else:
                error_type = "file_operation_error"
            is_error = True
        else:
            is_error = rc != 0 and not out

        return BrainResponse(content=content, brain_name=self.name,
                             duration_ms=elapsed, is_error=is_error, error_type=error_type)

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
    """Codex CLI — ChatGPT Team subscription (auth_mode: chatgpt, no API billing).

    Uses `codex exec` headless mode (gpt-5.4 via ChatGPT Team OAuth).
    No API key required — uses ~/.codex/auth.json from `codex login`.
    Primary brain for CODE intent. Falls back to sonnet on failure.
    """

    name = "codex"
    display_name = "Codex (ChatGPT Team)"
    emoji = "🟢"

    def __init__(self, timeout: int = 90) -> None:
        self._timeout = timeout
        self._cli = shutil.which("codex", path=_EXTRA_PATH)

    @staticmethod
    def _parse_output(raw: str) -> str:
        """Extract model response from codex exec output.

        Codex output format:
            OpenAI Codex v0.x.y ...
            --------
            workdir: ...  model: ...
            --------
            user
            <prompt>
            codex
            <RESPONSE>        ← we want this
            tokens used
            <count>
            done
        """
        # Strip everything up to and including the second "--------" header block
        parts = raw.split("--------")
        body = parts[-1] if len(parts) >= 3 else raw

        # Extract content between "codex" label and "tokens used" or end
        lines = body.splitlines()
        capturing = False
        result_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "codex":
                capturing = True
                continue
            if capturing:
                if stripped in ("tokens used", "done", "Shell cwd was reset"):
                    break
                result_lines.append(line)

        result = "\n".join(result_lines).strip()
        # Fallback: if parse fails, return cleaned raw output
        if not result:
            result = "\n".join(
                l for l in raw.splitlines()
                if l.strip() and not any(
                    l.strip().startswith(p) for p in
                    ("OpenAI Codex", "workdir:", "model:", "approval:", "sandbox:",
                     "reasoning", "session id:", "provider:", "tokens used", "--------",
                     "Shell cwd", "done", "user")
                )
            ).strip() or raw.strip()
        return result or "no output"

    async def execute(self, prompt: str, working_directory: str = "",
                      timeout_seconds: int = 0, **_: Any) -> BrainResponse:
        if not self._cli:
            return BrainResponse(content="codex not installed. Run: brew install codex",
                                 brain_name=self.name, is_error=True,
                                 error_type="not_installed")
        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())

        start = time.time()
        try:
            # stdin=DEVNULL prevents "Reading additional input from stdin..." hang
            proc = await asyncio.create_subprocess_exec(
                self._cli, "exec", prompt, "--skip-git-repo-check",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=_env_with_path(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return BrainResponse(
                    content=f"codex timeout after {timeout}s",
                    brain_name=self.name,
                    duration_ms=int((time.time() - start) * 1000),
                    is_error=True, error_type="timeout",
                )
        except Exception as exc:
            return BrainResponse(content=str(exc), brain_name=self.name,
                                 is_error=True, error_type="subprocess_error")

        elapsed = int((time.time() - start) * 1000)
        raw_out = stdout.decode("utf-8", errors="replace")
        raw_err = stderr.decode("utf-8", errors="replace").strip()

        # Auth / rate-limit errors
        combined = (raw_out + raw_err).lower()
        if any(k in combined for k in ("unauthorized", "401", "login", "not logged in")):
            return BrainResponse(content="Codex auth expired. Run: codex login",
                                 brain_name=self.name, duration_ms=elapsed,
                                 is_error=True, error_type="not_authenticated")
        if "429" in combined or "rate limit" in combined:
            return BrainResponse(content="Codex rate limited — cascading to sonnet",
                                 brain_name=self.name, duration_ms=elapsed,
                                 is_error=True, error_type="rate_limited")

        content = self._parse_output(raw_out) if raw_out.strip() else (raw_err or "no output")
        logger.info("codex_ok", duration_ms=elapsed, chars=len(content))
        return BrainResponse(content=content, brain_name=self.name, duration_ms=elapsed,
                             metadata={"model": "gpt-5.4", "auth": "chatgpt_team"})

    async def health_check(self) -> BrainStatus:
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        auth_file = Path.home() / ".codex" / "auth.json"
        if not auth_file.exists():
            return BrainStatus.NOT_AUTHENTICATED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "emoji": self.emoji,
            "cli": self._cli or "not found",
            "model": "gpt-5.4 (ChatGPT Team)",
            "auth": "ChatGPT OAuth — no API billing",
            "cost": "$0 extra (ChatGPT Team subscription)",
        }

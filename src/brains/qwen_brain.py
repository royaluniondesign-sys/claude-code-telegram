"""Qwen Code Brain — Alibaba Qwen coding model via qwen-code CLI.

# ── Integration guide (copy this block for any new CLI brain) ─────────────────
#
# 1. Install:   npm install -g @qwen-code/qwen-code@latest  (Node 20+)
#               Or: brew install qwen-code
#
# 2. Auth:      qwen auth   (OAuth free: 100 req/day)
#               Or: set API key in ~/.qwen/settings.json
#
# 3. Invoke:    qwen -p "prompt"  (headless, exits when done)
#               Similar to: claude -p "..." / gemini -p "..."
#
# 4. Config:    ~/.qwen/settings.json   (model, provider, API keys)
#
# 5. Register:  src/brains/router.py → BrainRouter.__init__()
#               Add to _FULL_CASCADE + _FREE_FALLBACK + _INTENT_BRAIN_MAP
#
# Strengths:    Code generation, analysis, debugging, refactoring
# Weaknesses:   100 req/day cap (free); requires Node.js runtime
# Position:     After cline (free local), before codex (paid) in cascade
# ─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from .base import Brain, BrainResponse, BrainStatus

logger = structlog.get_logger()

# Common Node.js install locations (nvm, homebrew, system)
_NODE_PATHS: list[str] = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".nvm" / "versions" / "node"),  # nvm root (scanned below)
]
_QWEN_SETTINGS = Path.home() / ".qwen" / "settings.json"
_QWEN_OAUTH = Path.home() / ".qwen" / "oauth_creds.json"  # Qwen OAuth token location


def _build_env() -> dict:
    """Build subprocess env with expanded PATH covering common Node.js installs."""
    env = os.environ.copy()
    # Collect all bin directories (including nvm version subdirs)
    extra: list[str] = list(_NODE_PATHS)
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        for ver_dir in sorted(nvm_root.iterdir(), reverse=True):
            bin_dir = ver_dir / "bin"
            if bin_dir.exists():
                extra.append(str(bin_dir))
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    # Ensure non-interactive / no TTY prompts
    env.setdefault("CI", "1")
    return env


def _find_qwen_binary() -> Optional[str]:
    """Locate the qwen binary across all common install locations."""
    # Standard PATH first
    found = shutil.which("qwen")
    if found:
        return found
    # Build extended PATH and retry
    extended = ":".join(_NODE_PATHS)
    found = shutil.which("qwen", path=extended)
    if found:
        return found
    # nvm: scan version directories newest-first
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        for ver_dir in sorted(nvm_root.iterdir(), reverse=True):
            candidate = ver_dir / "bin" / "qwen"
            if candidate.exists():
                return str(candidate)
    # Homebrew / system npm global
    for loc in [
        Path("/opt/homebrew/lib/node_modules/@qwen-code/qwen-code/bin/qwen"),
        Path("/usr/local/lib/node_modules/@qwen-code/qwen-code/bin/qwen"),
        Path.home() / ".npm-global" / "bin" / "qwen",
    ]:
        if loc.exists():
            return str(loc)
    return None


async def _run_subprocess(args: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run a subprocess, capture stdout/stderr, respect timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=_build_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"timeout after {timeout}s"
    except Exception as exc:
        return -1, "", str(exc)
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


class QwenCodeBrain(Brain):
    """Qwen Code (Alibaba) — code-specialized agentic CLI.

    Free tier: 100 requests/day via OAuth.
    Paid: API key for unlimited access.
    Strong at: code generation, analysis, debugging, refactoring.
    Cascade position: after cline (free local Ollama), before codex (paid).
    """

    name = "qwen-code"
    display_name = "Qwen Code (Alibaba)"
    emoji = "🐉"

    def __init__(self, timeout: int = 120) -> None:
        self._timeout = timeout
        self._cli: Optional[str] = _find_qwen_binary()
        authed = self._is_authed()
        logger.info(
            "qwen_brain_init",
            cli=self._cli or "not_found",
            authed=authed,
            limit="1000 req/day (OAuth free tier)",
        )

    def _is_authed(self) -> bool:
        """Return True if OAuth token or API key is configured."""
        # Primary: OAuth token file (created by `qwen auth qwen-oauth`)
        if _QWEN_OAUTH.exists():
            try:
                data = json.loads(_QWEN_OAUTH.read_text())
                return bool(data.get("access_token") or data.get("refresh_token"))
            except Exception:
                return True  # File exists but can't parse → assume valid

        # Secondary: settings.json with explicit auth type set
        if _QWEN_SETTINGS.exists():
            try:
                data = json.loads(_QWEN_SETTINGS.read_text())
                auth = data.get("security", {}).get("auth", {})
                if auth.get("selectedType"):
                    return True
                # Legacy format
                return bool(
                    data.get("apiKey")
                    or data.get("providers")
                    or data.get("auth")
                )
            except Exception:
                pass
        return False

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        **_kwargs: Any,
    ) -> BrainResponse:
        """Execute prompt in headless mode: qwen -p "prompt"."""
        if not self._cli:
            return BrainResponse(
                content=(
                    "qwen-code not installed.\n"
                    "Install: npm install -g @qwen-code/qwen-code@latest"
                ),
                brain_name=self.name,
                is_error=True,
                error_type="not_installed",
            )

        timeout = timeout_seconds or self._timeout
        cwd = working_directory or str(Path.home())
        start = time.time()

        # Headless invocation — mirrors: gemini -p "..." --approval-mode yolo -o text
        # Positional prompt (not -p which is deprecated), output-format text, yolo mode
        rc, out, err = await _run_subprocess(
            [self._cli, prompt, "--output-format", "text", "--approval-mode", "yolo"],
            cwd=cwd,
            timeout=timeout,
        )
        elapsed = int((time.time() - start) * 1000)
        combined = (out + " " + err).lower()

        # ── Error classification ──────────────────────────────────────────────

        if "rate limit" in combined or "429" in combined or "quota" in combined or "daily limit" in combined or "request limit" in combined:
            logger.warning("qwen_code_rate_limited", duration_ms=elapsed)
            return BrainResponse(
                content="Qwen-Code daily limit reached (1000 req/day free tier). Cascading…",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="rate_limited",
            )

        if "authentication" in combined or "unauthorized" in combined or "login" in combined or "api key" in combined.replace("_", " "):
            logger.warning("qwen_code_auth_error", duration_ms=elapsed)
            return BrainResponse(
                content="Qwen-Code auth required. Run: qwen auth",
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="not_authenticated",
            )

        # Use stdout if available, else fall back to stderr (some CLIs log to stderr)
        content = out if out else err if err else "no output"

        if rc != 0 and not out:
            logger.warning("qwen_code_error", rc=rc, stderr=err[:200], duration_ms=elapsed)
            return BrainResponse(
                content=content,
                brain_name=self.name,
                duration_ms=elapsed,
                is_error=True,
                error_type="nonzero_exit",
            )

        logger.info("qwen_code_ok", duration_ms=elapsed, chars=len(content))
        return BrainResponse(
            content=content,
            brain_name=self.name,
            duration_ms=elapsed,
            metadata={"model": "qwen-code", "cwd": cwd},
        )

    async def health_check(self) -> BrainStatus:
        """Check installation and authentication status."""
        # Re-scan binary on health check (user may have just installed it)
        if not self._cli:
            self._cli = _find_qwen_binary()
        if not self._cli:
            return BrainStatus.NOT_INSTALLED
        if not self._is_authed():
            return BrainStatus.NOT_AUTHENTICATED
        return BrainStatus.READY

    async def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "emoji": self.emoji,
            "cli": self._cli or "not found",
            "authed": self._is_authed(),
            "auth_method": "OAuth free (1000 req/day) or API key",
            "models": ["Qwen3.6-Plus", "Qwen3.5-Plus", "Qwen3-Coder"],
            "cost": "Free (1000 req/day OAuth) / Paid (API key)",
            "strengths": "Code gen, analysis, debugging, refactoring",
            "install": "npm install -g @qwen-code/qwen-code@latest",
            "config_path": str(_QWEN_SETTINGS),
            "auth_path": str(_QWEN_OAUTH),
        }

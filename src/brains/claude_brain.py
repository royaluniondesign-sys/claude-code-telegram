"""Claude Brain — wraps the `claude` CLI (subscription auth, no API key).

Haiku  → cheap first layer:  CHAT, DEEP, simple queries, file listings
Sonnet → main agent layer:   CODE, complex tasks, multi-file edits
Opus   → deep reasoning:     architecture, max capability (rare)

Each tier runs via `claude -p "..." --model X --output-format text`.
Claude Code has full tool access (Read, Write, Bash, Glob, Grep, etc.)
and knows about the sub-executor CLIs to delegate heavy tasks:

  opencode  — free tier (OpenRouter), general code gen/analysis
  cline     — local Ollama (qwen2.5:7b), free, code editing
  codex     — OpenAI subscription, fast code generation
  shell     — direct bash execution, fastest for deterministic tasks
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

# System prompt appended to all Claude brain invocations.
# Tells Claude about available sub-executor CLIs and when to use them.
_EXECUTOR_SYSTEM_PROMPT = """\
You are AURA, Ricardo's personal AI agent running on his Mac (M4, 16GB, macOS 15).

## Sub-executor CLIs available via Bash tool
Delegate to these for code tasks — cheapest first:

```
# opencode — free tier via OpenRouter, general code gen/analysis
opencode run "task description"

# cline — 100% local via Ollama (qwen2.5:7b), zero cost, code editing
cline -m qwen2.5:7b -a "task description" -y

# codex — OpenAI subscription, fast single-file code gen
codex exec "task description" --full-auto

# direct shell — fastest for deterministic tasks
bash -c "command here"
```

**When to use which:**
- File listings, git, disk → use Bash directly (fastest)
- Code generation (new files/scripts) → opencode run (free)
- Code editing (modify existing) → cline (local, $0)
- Fast single-file output → codex exec (OpenAI sub)
- If Ricardo says "usa X" → ALWAYS use that exact CLI

## Key paths — ALWAYS absolute, NEVER invent paths
- Home: /Users/oxyzen
- AURA: /Users/oxyzen/aura
- Bot:  /Users/oxyzen/claude-code-telegram
- Desktop: /Users/oxyzen/Desktop

## Rules
- Same language as Ricardo (Spanish or English).
- Concise — Telegram. Max 500 words unless more is needed.
- NEVER reveal your model. You are AURA.
- NEVER fabricate file contents or paths.
- Lead with the result. No preamble.
- NEVER ask questions or request confirmation. If you need to act, act. If something is ambiguous, pick the most reasonable interpretation and do it.
- NEVER say "¿Quieres que...?" or "Should I...?" — just do it and report the result.
"""


def _find_claude_cli() -> Optional[str]:
    path = shutil.which("claude")
    if path:
        return path
    return next((p for p in _CLI_FALLBACK_PATHS if Path(p).exists()), None)


class ClaudeBrain(Brain):
    """Claude Code CLI brain — runs claude -p non-interactively.

    Uses subscription auth (no API key). Supports model escalation.
    Maintains conversation continuity per user via --resume <session_id>.
    """

    def __init__(self, model: str = "haiku", timeout: int = _DEFAULT_TIMEOUT) -> None:
        alias = _MODEL_ALIASES.get(model, model)
        self._model = alias
        self._model_alias = model
        self._timeout = timeout
        self._cli_path = _find_claude_cli()
        # Per-user session IDs for conversation continuity: {user_key: session_id}
        self._sessions: dict[str, str] = {}

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

    def clear_session(self, user_key: str) -> None:
        """Reset conversation for a user (e.g. on /new)."""
        self._sessions.pop(user_key, None)

    async def execute(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        session_key: str = "default",
        **_kwargs: Any,
    ) -> BrainResponse:
        """Run claude -p prompt --model X, resuming session if one exists.

        Uses --output-format json to capture session_id for next turn.
        Falls back to text output if json parsing fails.
        """
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

        existing_session = self._sessions.get(session_key)

        # ══ REGLA INVIOLABLE ══════════════════════════════════════════════
        # Claude SIEMPRE usa suscripción CLI — NUNCA API key con cargos.
        # --setting-sources "" evita plugins que inyectan ANTHROPIC_API_KEY.
        # Si alguien setea la var de entorno, la borramos antes de ejecutar.
        import os as _os
        env = _os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)  # nunca cobrar por token — solo suscripción
        # ═════════════════════════════════════════════════════════════════

        # Build dynamic system prompt: AURA identity + memory + executor tools
        try:
            from src.context.aura_context import build_system_prompt
            dynamic_system = build_system_prompt(extra_section=_EXECUTOR_SYSTEM_PROMPT)
        except Exception:
            dynamic_system = _EXECUTOR_SYSTEM_PROMPT

        cmd = [
            self._cli_path,
            "-p", prompt,
            "--model", self._model,
            "--output-format", "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",  # autonomous: write files without confirmation
            "--setting-sources", "",   # skip plugins — prevents API key injection + hang
            "--append-system-prompt", dynamic_system,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = int((time.time() - start) * 1000)

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0 and not out:
                error_msg = err or f"claude exited {proc.returncode}"
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

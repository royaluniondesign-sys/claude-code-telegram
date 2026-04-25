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
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, Optional

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
# cline — 100% local via Ollama (qwen2.5:7b), zero cost, code editing
cline -m qwen2.5:7b -a "task description" -y

# codex — OpenAI subscription, fast single-file code gen
codex exec "task description" --full-auto

# direct shell — fastest for deterministic tasks
bash -c "command here"
```

**When to use which:**
- File listings, git, disk → use Bash directly (fastest)
- Code generation (new files/scripts) → codex exec (fast, subscription)
- Code editing (modify existing) → cline (local, $0)
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

## Anti-loop rules (CRITICAL)
- If you've already tried a tool call and it failed, try a different approach — NEVER retry the same failing command more than once.
- If a sub-CLI (codex/cline) fails, fall back to bash or answer from knowledge — don't loop.
- Complete the task in ONE pass. Do not re-read your own output and re-process it.
- If unsure after 2 tool calls, stop and report what you found so far.
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
        names = {
            "haiku": "Claude Haiku",
            "sonnet": "Claude Sonnet",
            "opus": "Claude Opus",
        }
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
        allowed_tools: list[str] | None = None,
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
            "-p",
            prompt,
            "--model",
            self._model,
            "--output-format",
            "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",  # autonomous: write files without confirmation
            "--setting-sources",
            "",  # skip plugins — prevents API key injection + hang
            "--max-turns",
            "5",  # hard cap on tool-use rounds — prevents Haiku loops
            "--append-system-prompt",
            dynamic_system,
        ]

        if allowed_tools:
            cmd += ["--allowedTools", ",".join(allowed_tools)]

        import os as _os2

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            elapsed = int((time.time() - start) * 1000)

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0 and not out:
                # returncode 143 = SIGTERM (macOS OOM killer or memory pressure).
                # Cascading to another Claude tier won't help — they'll get killed too.
                # Return a human-readable message instead of triggering cascade.
                if proc.returncode == 143:
                    logger.warning(
                        "claude_brain_oom_kill",
                        model=self._model_alias,
                        returncode=143,
                    )
                    return BrainResponse(
                        content="⚠️ RAM al límite — claude fue terminado por el SO. Cierra Chrome/apps pesadas y vuelve a intentarlo.",
                        brain_name=self.name,
                        duration_ms=elapsed,
                        is_error=False,  # Don't cascade — OOM kills all Claude tiers equally
                        error_type="oom_kill",
                    )
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

        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # Kill subprocess on timeout OR cancellation from outer wait_for
            if proc is not None:
                try:
                    import signal as _sig

                    _os2.killpg(_os2.getpgid(proc.pid), _sig.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            elapsed = int((time.time() - start) * 1000)
            if isinstance(exc, asyncio.TimeoutError):
                return BrainResponse(
                    content=f"⏱️ {self.display_name} timeout ({timeout}s)",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="timeout",
                )
            pass  # else block - no timeout occurred
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

    async def execute_streaming(
        self,
        prompt: str,
        working_directory: str = "",
        timeout_seconds: int = 0,
        session_key: str = "default",
        on_event: Optional[Callable[[str, str, str], None]] = None,
        **_kwargs: Any,
    ) -> BrainResponse:
        """Run claude with --output-format stream-json, emitting tool events live.

        Calls on_event(kind, name, detail) for each observable event:
          kind="tool"   → tool call (name=tool name, detail=short input summary)
          kind="text"   → assistant reasoning snippet (name="text", detail=snippet)
          kind="error"  → parse/stream error (name="error", detail=message)

        Falls back to execute() if stream-json fails.
        Returns a BrainResponse with the final assembled text.
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

        import os as _os
        env = _os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        try:
            from src.context.aura_context import build_system_prompt
            dynamic_system = build_system_prompt(extra_section=_EXECUTOR_SYSTEM_PROMPT)
        except Exception:
            dynamic_system = _EXECUTOR_SYSTEM_PROMPT

        cmd = [
            self._cli_path, "-p", prompt,
            "--model", self._model,
            "--output-format", "stream-json",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--setting-sources", "",
            "--append-system-prompt", dynamic_system,
        ]

        proc: Optional[asyncio.subprocess.Process] = None
        accumulated_text = ""
        session_id = ""
        cost = 0.0

        def _summarize(tool_name: str, inp: dict) -> str:
            """Short human-readable summary of tool input."""
            if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
                p = inp.get("file_path") or inp.get("path", "")
                return p.rsplit("/", 1)[-1] if p else ""
            if tool_name in ("Glob", "Grep"):
                return inp.get("pattern", inp.get("query", ""))[:50]
            if tool_name == "Bash":
                cmd_str = inp.get("command", "")
                # strip leading whitespace/newlines, cap length
                return cmd_str.strip()[:60]
            if tool_name in ("WebFetch", "WebSearch"):
                return (inp.get("url") or inp.get("query", ""))[:50]
            if tool_name == "Task":
                return inp.get("description", "")[:50]
            # generic: first non-empty string value
            for v in inp.values():
                if isinstance(v, str) and v:
                    return v[:50]
            return ""

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            assert proc.stdout is not None

            # Read line-by-line with overall timeout
            async def _read_lines() -> None:
                nonlocal accumulated_text, session_id, cost
                async for raw_line in proc.stdout:  # type: ignore[union-attr]
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type", "")

                    # ── Final result ───────────────────────────────────────
                    if msg_type == "result":
                        accumulated_text = obj.get("result", "") or ""
                        session_id = obj.get("session_id", "")
                        cost = float(obj.get("total_cost_usd") or 0.0)
                        continue

                    # ── Assistant message — scan content blocks ─────────
                    if msg_type == "assistant":
                        msg = obj.get("message", {})
                        content_blocks = msg.get("content", [])
                        for block in content_blocks:
                            btype = block.get("type", "")
                            if btype == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                detail = _summarize(tool_name, tool_input)
                                if on_event:
                                    on_event("tool", tool_name, detail)
                            elif btype == "text":
                                snippet = block.get("text", "").strip()
                                if snippet and on_event:
                                    # Only first line of thinking
                                    first = snippet.split("\n", 1)[0][:100]
                                    if first:
                                        on_event("text", "thinking", first)

            await asyncio.wait_for(_read_lines(), timeout=timeout)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            elapsed = int((time.time() - start) * 1000)

            if not accumulated_text:
                # Fallback: maybe stderr has info
                err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
                if proc.returncode == 143:
                    logger.warning("claude_brain_oom_kill", model=self._model_alias, returncode=143)
                    return BrainResponse(
                        content="⚠️ RAM al límite — claude fue terminado por el SO. Cierra Chrome/apps pesadas y vuelve a intentarlo.",
                        brain_name=self.name,
                        duration_ms=elapsed,
                        is_error=False,  # Don't cascade
                        error_type="oom_kill",
                    )
                if proc.returncode != 0:
                    return BrainResponse(
                        content=err or f"claude exited {proc.returncode}",
                        brain_name=self.name,
                        duration_ms=elapsed,
                        is_error=True,
                        error_type="nonzero_exit",
                    )
                return BrainResponse(
                    content="(sin respuesta)",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="empty_output",
                )

            logger.info(
                "claude_brain_stream_ok",
                model=self._model_alias,
                duration_ms=elapsed,
                output_len=len(accumulated_text),
                cost_usd=round(cost, 6),
            )
            return BrainResponse(
                content=accumulated_text,
                brain_name=self.name,
                duration_ms=elapsed,
                cost=cost,
            )

        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc is not None:
                try:
                    import os as _os2, signal as _sig
                    _os2.killpg(_os2.getpgid(proc.pid), _sig.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            elapsed = int((time.time() - start) * 1000)
            if isinstance(exc, asyncio.TimeoutError):
                return BrainResponse(
                    content=f"⏱️ {self.display_name} timeout ({timeout}s)",
                    brain_name=self.name,
                    duration_ms=elapsed,
                    is_error=True,
                    error_type="timeout",
                )
            raise
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            logger.warning(
                "claude_brain_stream_fallback",
                model=self._model_alias,
                error=str(e)[:120],
            )
            # Fall back to regular execute
            return await self.execute(
                prompt=prompt,
                working_directory=working_directory,
                timeout_seconds=timeout_seconds,
                session_key=session_key,
            )

    async def health_check(self) -> BrainStatus:
        if not self._cli_path:
            return BrainStatus.NOT_INSTALLED
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path,
                "--version",
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
                self._cli_path or "claude",
                "--version",
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
            "cost": {
                "haiku": "~$0/msg (Max plan)",
                "sonnet": "~$0/msg (Max plan)",
                "opus": "~$0/msg (Max plan)",
            }.get(self._model_alias, "Subscription"),
            "timeout_s": self._timeout,
        }

"""Brain dispatch logic — unknown commands and alt-brain execution.

Contains:
  _unknown_command   — catch-all for unregistered slash commands
  _handle_alt_brain  — streaming + non-streaming brain execution with fallback cascade
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..orchestrator_utils import escape_html, start_typing_heartbeat

if TYPE_CHECKING:
    from ..orchestrator import MessageOrchestrator

logger = structlog.get_logger()

_SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_PHASES = [
    (0, "pensando"),
    (8, "procesando"),
    (20, "trabajando"),
    (50, "aún trabajando ⏳"),
]


def _phase(elapsed: float) -> str:
    label = "pensando"
    for t, n in _PHASES:
        if elapsed >= t:
            label = n
    return label


def _dur(elapsed: float) -> str:
    s = int(elapsed)
    m, sec = divmod(s, 60)
    return f"{m}m{sec:02d}s" if m else f"{sec}s"


class BrainHandlerMixin:
    """Mixin providing unknown-command routing and alt-brain execution."""

    async def _unknown_command(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Catch-all for unregistered slash commands — route to haiku.

        Registered commands (start, git, health, etc.) are silently ignored
        here because they already ran in group=0 via CommandHandler.
        Only truly unknown commands (e.g. /seo-technical, /commit, /review-pr)
        get forwarded to haiku (Claude CLI can invoke skills natively).
        """
        text = update.message.text or ""
        if not text.startswith("/"):
            return

        # Extract command name (strip /, stop at space or @)
        cmd_part = text[1:].split()[0].split("@")[0].lower()

        # Skip if it's a known registered command — already handled in group=0
        known = getattr(self, "_registered_commands", set())
        if cmd_part in known:
            return

        router = context.bot_data.get("brain_router")
        if not router:
            return

        logger.info("unknown_command_to_haiku", command=text[:60])
        await self._handle_alt_brain(
            update, context, router, text,
            update.effective_user.id,
            brain_name="haiku",
        )

    async def _handle_alt_brain(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
        brain_name: str = "",
        intent: Any = None,
    ) -> None:
        """Handle messages via non-Claude brain.

        Streaming brains (OpenRouter): edits message progressively as tokens
        arrive — text appears word-by-word like Claude Code.

        Non-streaming brains (Claude CLI, Gemini CLI): spinner heartbeat with
        phase labels (pensando → procesando → trabajando).
        """
        from src.observability import get_tracer

        brain = (
            router.get_brain(brain_name) if brain_name else router.get_active_brain(user_id)
        )
        if brain is None:
            brain = router.get_active_brain(user_id)

        tracer = get_tracer()
        trace_ctx = tracer.trace_brain(
            brain_name=brain.name,
            user_id=user_id,
            message=message_text,
            metadata={"handler": "alt_brain"},
        )

        _start = time.time()
        current_dir = str(
            context.user_data.get("current_directory", self.settings.approved_directory)
        )
        rate_monitor = context.bot_data.get("rate_monitor")
        original_brain = brain

        # Initial status message — appears immediately
        progress_msg = await update.message.reply_text(
            f"{brain.emoji} <b>{brain.display_name}</b> · ⠋",
            parse_mode="HTML",
        )

        # Typing indicator — always visible in chat header throughout response
        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        # ── PATH A: Streaming (OpenRouter) ─────────────────────────────────
        if getattr(brain, "supports_streaming", False):
            accumulated = ""
            last_edit = 0.0
            is_error = False
            error_type = ""
            heartbeat_task_s: Optional["asyncio.Task[None]"] = None

            # Heartbeat for the first few seconds before first token arrives
            async def _pre_stream_heartbeat() -> None:
                try:
                    frame = 0
                    while True:
                        await asyncio.sleep(0.8)   # fast — show before first token
                        frame += 1
                        elapsed = time.time() - _start
                        spin = _SPIN[frame % len(_SPIN)]
                        phase = _phase(elapsed)
                        try:
                            await progress_msg.edit_text(
                                f"{brain.emoji} <b>{brain.display_name}</b> · {spin} {phase}",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass

            heartbeat_task_s = asyncio.ensure_future(_pre_stream_heartbeat())

            try:
                async for chunk in brain.execute_stream(
                    prompt=message_text,
                    working_directory=current_dir,
                    timeout_seconds=self.settings.claude_timeout_seconds,
                ):
                    if chunk.startswith("\x00ERROR:"):
                        is_error = True
                        error_type = chunk[7:]
                        break

                    # First token arrived — stop pre-stream heartbeat
                    if not accumulated and heartbeat_task_s and not heartbeat_task_s.done():
                        heartbeat_task_s.cancel()
                        try:
                            await heartbeat_task_s
                        except asyncio.CancelledError:
                            pass
                        heartbeat_task_s = None

                    accumulated += chunk

                    # Edit message at most every 1.0s (Telegram allows up to 1/s per chat)
                    now = time.time()
                    if now - last_edit >= 1.0:
                        elapsed = now - _start
                        header = (
                            f"{brain.emoji} <b>{brain.display_name}</b> · {_dur(elapsed)}"
                        )
                        display = accumulated[:3700]
                        try:
                            await progress_msg.edit_text(
                                f"{header}\n\n{escape_html(display)}▌",
                                parse_mode="HTML",
                            )
                            last_edit = now
                        except Exception as _edit_err:
                            logger.debug("stream_edit_fail", error=str(_edit_err))

            finally:
                if heartbeat_task_s and not heartbeat_task_s.done():
                    heartbeat_task_s.cancel()

            # Final edit — remove cursor, show total time
            elapsed_total = time.time() - _start
            header = f"{brain.emoji} <b>{brain.display_name}</b> · {_dur(elapsed_total)}"

            if is_error or not accumulated:
                # Escalate on streaming error — with animated heartbeat during fallback wait
                fallback_name = router.get_fallback_brain(brain.name) if router else None
                if fallback_name:
                    fallback = router.get_brain(fallback_name)
                    if fallback:
                        await progress_msg.edit_text(
                            f"↗️ {original_brain.emoji}→{fallback.emoji}"
                            f" <b>{original_brain.display_name} → {fallback.display_name}</b>",
                            parse_mode="HTML",
                        )

                        # Heartbeat while fallback executes (can take 30-120s for Claude CLI)
                        _esc_start = time.time()
                        fallback_task = asyncio.ensure_future(
                            fallback.execute(
                                prompt=message_text,
                                working_directory=current_dir,
                                timeout_seconds=self.settings.claude_timeout_seconds,
                            )
                        )
                        frame = 0
                        while not fallback_task.done():
                            await asyncio.sleep(1.5)
                            frame += 1
                            spin = _SPIN[frame % len(_SPIN)]
                            elapsed_esc = time.time() - _esc_start
                            phase = _phase(elapsed_esc)
                            try:
                                await progress_msg.edit_text(
                                    f"↗️ {original_brain.emoji}→{fallback.emoji}"
                                    f" <b>{fallback.display_name}</b>"
                                    f" · {spin} {phase} · {_dur(elapsed_esc)}",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass

                        response = await fallback_task
                        accumulated = response.content or "(sin respuesta)"
                        brain = fallback
                        if rate_monitor:
                            rate_monitor.record_request(fallback.name)
                        elapsed_total = time.time() - _start
                        header = (
                            f"↗️ {original_brain.emoji}→{brain.emoji}"
                            f" <b>{brain.display_name}</b> · {_dur(elapsed_total)}"
                        )
                else:
                    accumulated = f"Error: {error_type}"
                if rate_monitor:
                    rate_monitor.record_error(original_brain.name, is_rate_limit=True)
            else:
                if rate_monitor:
                    rate_monitor.record_request(brain.name)

            content = (accumulated or "(sin respuesta)")[:3900]
            try:
                await progress_msg.edit_text(
                    f"{header}\n\n{escape_html(content)}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            tracer.end_trace(
                ctx=trace_ctx,
                output=content[:500],
                duration_ms=int(elapsed_total * 1000),
            )

            # Record outcome in cortex for learning (streaming path)
            if self._cortex is not None:
                try:
                    _intent_str = "chat"
                    try:
                        if intent is not None and hasattr(intent, "intent"):
                            _intent_str = intent.intent.value
                    except Exception:
                        pass
                    self._cortex.record_outcome(
                        brain=brain.name,
                        intent=_intent_str,
                        success=not is_error,
                        duration_ms=int(elapsed_total * 1000),
                        error=error_type if is_error else "",
                        prompt=message_text,
                    )
                except Exception as _cx_err:
                    logger.debug("cortex_record_stream_error", error=str(_cx_err))

            # Background learning — fact extractor + Mem0
            if accumulated and not is_error:
                try:
                    from src.context.fact_extractor import learn_from_interaction
                    asyncio.ensure_future(
                        asyncio.get_event_loop().run_in_executor(
                            None, learn_from_interaction, message_text, content
                        )
                    )
                except Exception:
                    pass
                try:
                    from src.context.mempalace_memory import store_interaction
                    asyncio.ensure_future(store_interaction(message_text, content))
                except Exception:
                    pass

            if rate_monitor:
                warning = rate_monitor.should_warn(brain.name)
                if warning:
                    await update.message.reply_text(warning)
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()
            return

        # ── PATH B: Claude CLI / Gemini CLI ───────────────────────────────
        # Live tool-call display: shows each tool as it's called, like Claude Desktop.
        # Falls back to spinner-only for brains that don't support streaming.
        heartbeat_task: Optional["asyncio.Task[None]"] = None

        # Shared state for live tool log (mutated by on_event callback)
        _tool_log: list = []          # [(icon_str, line_str), ...]
        _last_progress_edit = [0.0]   # throttle edits

        from ..orchestrator_utils import tool_icon as _tool_icon, escape_html as _esc_html

        def _tool_event(kind: str, name: str, detail: str) -> None:
            """Called from execute_streaming() for each tool/text event."""
            if kind == "tool":
                icon = _tool_icon(name)
                line = f"{icon} <code>{name}</code>"
                if detail:
                    safe = _esc_html(detail[:60])
                    line += f" <i>{safe}</i>"
            else:
                # reasoning text snippet — shown dimmed
                line = f"<i>{_esc_html(detail[:80])}</i>"
            _tool_log.append(line)

        def _build_progress_text(current_brain: Any, elapsed: float, spin: str) -> str:
            """Build the live progress message: header + last N tool lines."""
            dur = _dur(elapsed) if elapsed >= 1 else ""
            phase = _phase(elapsed)
            header = (
                f"{current_brain.emoji} <b>{current_brain.display_name}</b>"
                f" · {spin} {phase}"
                + (f" · {dur}" if dur else "")
            )
            if not _tool_log:
                return header
            # Show up to 8 most recent tool lines
            recent = _tool_log[-8:]
            return header + "\n" + "\n".join(recent)

        async def _heartbeat(current_brain: Any) -> None:
            try:
                frame = 0
                while True:
                    await asyncio.sleep(1.5)
                    frame += 1
                    now = time.time()
                    elapsed = now - _start
                    spin = _SPIN[frame % len(_SPIN)]
                    # Throttle edits to ~1/2s when tool log is active
                    if _tool_log and (now - _last_progress_edit[0]) < 1.8:
                        continue
                    try:
                        await progress_msg.edit_text(
                            _build_progress_text(current_brain, elapsed, spin),
                            parse_mode="HTML",
                        )
                        _last_progress_edit[0] = now
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        heartbeat_task = asyncio.ensure_future(_heartbeat(brain))

        async def _stop() -> None:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        try:
            # Use streaming execution for ClaudeBrain (shows live tool calls)
            execute_fn = getattr(brain, "execute_streaming", None)
            if execute_fn is not None:
                response = await execute_fn(
                    prompt=message_text,
                    working_directory=current_dir,
                    timeout_seconds=self.settings.claude_timeout_seconds,
                    session_key=str(user_id),
                    on_event=_tool_event,
                )
            else:
                response = await brain.execute(
                    prompt=message_text,
                    working_directory=current_dir,
                    timeout_seconds=self.settings.claude_timeout_seconds,
                    session_key=str(user_id),
                )

            if rate_monitor:
                if response.is_error and "rate" in (response.error_type or "").lower():
                    rate_monitor.record_error(brain.name, is_rate_limit=True)
                else:
                    rate_monitor.record_request(brain.name)

            # ── Multi-level cascade on error ──────────────────────────────────
            if response.is_error and router:
                cascade_chain = (
                    router.get_cascade_chain(brain.name)
                    if hasattr(router, "get_cascade_chain")
                    else []
                )
                for fallback_name in cascade_chain:
                    fallback = router.get_brain(fallback_name)
                    if not fallback:
                        continue
                    # Skip rate-limited brains in cascade
                    if rate_monitor:
                        try:
                            if rate_monitor.get_usage(fallback_name).is_rate_limited:
                                logger.info("cascade_skip_ratelimited", brain=fallback_name)
                                continue
                        except Exception:
                            pass
                    reason = response.error_type or "error"
                    logger.info(
                        "brain_cascade",
                        from_brain=brain.name,
                        to_brain=fallback_name,
                        reason=reason,
                    )
                    await _stop()
                    await progress_msg.edit_text(
                        f"↗️ <b>{brain.display_name}</b> [{reason}]\n"
                        f"   → <b>{fallback.display_name}</b>...",
                        parse_mode="HTML",
                    )
                    heartbeat_task = asyncio.ensure_future(_heartbeat(fallback))  # type: ignore[assignment]
                    response = await fallback.execute(
                        prompt=message_text,
                        working_directory=current_dir,
                        timeout_seconds=self.settings.claude_timeout_seconds,
                    )
                    brain = fallback
                    if rate_monitor:
                        is_rl = "rate" in (response.error_type or "").lower()
                        if response.is_error:
                            rate_monitor.record_error(fallback.name, is_rate_limit=is_rl)
                        else:
                            rate_monitor.record_request(fallback.name)
                    if not response.is_error:
                        break  # success — stop cascade

            content = (response.content or "(sin respuesta)")[:3900]
            await _stop()

            elapsed_total = time.time() - _start
            header = f"{brain.emoji} <b>{brain.display_name}</b> · {_dur(elapsed_total)}"
            if brain.name != original_brain.name:
                header = (
                    f"↗️ {original_brain.emoji}→{brain.emoji}"
                    f" <b>{brain.display_name}</b> · {_dur(elapsed_total)}"
                )

            prefix = "❌ " if response.is_error else ""
            await progress_msg.edit_text(
                f"{prefix}{header}\n\n{escape_html(content)}",
                parse_mode="HTML",
            )

            tracer.end_trace(
                ctx=trace_ctx,
                output=content[:500],
                cost=response.cost,
                duration_ms=response.duration_ms,
            )

            # Record outcome in cortex for learning (non-streaming path)
            if self._cortex is not None:
                try:
                    _intent_str = "chat"
                    try:
                        if intent is not None and hasattr(intent, "intent"):
                            _intent_str = intent.intent.value
                    except Exception:
                        pass
                    self._cortex.record_outcome(
                        brain=brain.name,
                        intent=_intent_str,
                        success=not response.is_error,
                        duration_ms=int(elapsed_total * 1000),
                        error=str(response.error_type or "") if response.is_error else "",
                        prompt=message_text,
                    )
                except Exception as _cx_err:
                    logger.debug("cortex_record_nonstream_error", error=str(_cx_err))

            if not response.is_error:
                # ── Persist to SQLite ─────────────────────────────────────────
                try:
                    storage = context.bot_data.get("storage")
                    if storage:
                        asyncio.ensure_future(storage.save_message_raw(
                            user_id=user_id,
                            prompt=message_text,
                            response=content,
                            cost=response.cost or 0.0,
                            duration_ms=int(elapsed_total * 1000),
                            brain=brain.name,
                        ))
                except Exception:
                    pass
                # ── Background learning ───────────────────────────────────────
                try:
                    from src.context.fact_extractor import learn_from_interaction
                    asyncio.ensure_future(
                        asyncio.get_event_loop().run_in_executor(
                            None, learn_from_interaction, message_text, content
                        )
                    )
                except Exception:
                    pass
                try:
                    from src.context.mempalace_memory import store_interaction
                    asyncio.ensure_future(store_interaction(message_text, content))
                except Exception:
                    pass

            # ── Auto-voice: send audio if user has /voz on ────────────────
            try:
                voice_users = context.bot_data.get("voice_users", set())
                if user_id in voice_users and not response.is_error:
                    from src.bot.features.voice_tts import send_voice_response
                    asyncio.create_task(
                        send_voice_response(update, context, content),
                        name="voice_reply",
                    )
            except Exception:
                pass

            if rate_monitor:
                warning = rate_monitor.should_warn(brain.name)
                if warning:
                    await update.message.reply_text(warning)

        except Exception as e:
            if rate_monitor:
                rate_monitor.record_error(brain.name)
            logger.error("alt_brain_error", brain=brain.name, error=str(e))
            tracer.end_trace(ctx=trace_ctx, error=str(e))
            await _stop()
            try:
                await progress_msg.edit_text(
                    f"❌ {brain.emoji} {escape_html(str(e)[:400])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

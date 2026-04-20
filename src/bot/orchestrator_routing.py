"""Routing and brain-dispatch logic for MessageOrchestrator.

Contains:
  agentic_text              — main text handler with smart routing
  _handle_alt_brain         — non-Claude brain execution (streaming + non-streaming)
  _handle_conductor_task    — complex task → 3-layer conductor
  _handle_image_gen         — image generation via pollinations.ai
  _handle_email_native      — compose + send email natively
  _handle_social_post       — social media content pipeline
  _handle_video_gen         — cinematic / structured video generation
  _unknown_command          — catch-all for unregistered slash commands
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from .orchestrator_utils import escape_html, start_typing_heartbeat

if TYPE_CHECKING:
    from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()

# Spinner frames and phase labels for progress messages
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


class AgenticRoutingMixin:
    """Mixin providing smart routing and brain dispatch logic.

    Must be mixed into MessageOrchestrator which supplies:
      self.settings, self._cortex, self._escape_html(),
      self._start_typing_heartbeat(), self._summarize_tool_input()
    """

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

        from .orchestrator_utils import tool_icon as _tool_icon, escape_html as _esc_html

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

    async def _handle_conductor_task(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        task: str,
        user_id: int,
        route_decision: Any,
    ) -> None:
        """Ruta B — Route complex external task through the 3-layer conductor."""
        import time as _time
        from src.infra.task_router import write_external_outcome

        progress_msg = await update.message.reply_text(
            f"🧠 <b>Conductor</b> — analizando tarea compleja…\n"
            f"<i>{route_decision.reason}</i>",
            parse_mode="HTML",
        )
        t_start = _time.time()

        try:
            from src.brains.conductor import get_conductor, Conductor
            conductor = get_conductor(router)
            if conductor is None:
                conductor = Conductor(router, notify_fn=None)

            # Live progress via SSE events — update Telegram message
            last_update = _time.time()

            async def _progress_edit(text: str) -> None:
                nonlocal last_update
                if _time.time() - last_update < 8:  # max 1 edit per 8s (flood protection)
                    return
                last_update = _time.time()
                try:
                    await progress_msg.edit_text(text, parse_mode="HTML")
                except Exception:
                    pass

            conductor._notify = lambda msg: _progress_edit(
                f"🧠 <b>Conductor</b> — {msg[:200]}"
            )

            result = await asyncio.wait_for(
                conductor.run(task, source="external"),
                timeout=240,
            )

            duration_s = round(_time.time() - t_start, 1)
            output = result.final_output.strip() if result.final_output else ""

            if result.is_error or not output:
                await progress_msg.edit_text(
                    f"❌ Conductor no produjo output ({result.steps_failed} pasos fallaron)\n"
                    f"Tiempo: {duration_s}s",
                    parse_mode="HTML",
                )
                write_external_outcome(
                    task=task[:80],
                    route="complex",
                    success=False,
                    duration_s=duration_s,
                    output_preview=f"{result.steps_failed} steps failed",
                )
                return

            # Delete progress, send real answer
            try:
                await progress_msg.delete()
            except Exception:
                pass

            # Split long outputs (Telegram 4096 char limit)
            chunk_size = 3800
            chunks = [output[i:i + chunk_size] for i in range(0, len(output), chunk_size)]
            for i, chunk in enumerate(chunks):
                prefix = (
                    f"<b>🧠 Conductor</b> ({duration_s}s · {result.steps_completed}✓)\n\n"
                    if i == 0
                    else ""
                )
                await update.message.reply_text(prefix + chunk, parse_mode="HTML")

            write_external_outcome(
                task=task[:80],
                route="complex",
                success=True,
                duration_s=duration_s,
                output_preview=output[:200],
            )
            logger.info(
                "conductor_external_task_done",
                user_id=user_id,
                duration_s=duration_s,
                steps_ok=result.steps_completed,
                confidence=route_decision.confidence,
            )

        except asyncio.TimeoutError:
            try:
                await progress_msg.edit_text(
                    "⏱️ Conductor timeout (240s) — tarea muy larga. Intenta dividirla.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            write_external_outcome(
                task=task[:80],
                route="complex",
                success=False,
                duration_s=240.0,
                output_preview="timeout",
            )
        except Exception as exc:
            logger.error("conductor_external_task_error", error=str(exc), user_id=user_id)
            try:
                await progress_msg.edit_text(
                    f"❌ Error en conductor: {str(exc)[:200]}", parse_mode="HTML"
                )
            except Exception:
                pass
            write_external_outcome(
                task=task[:80],
                route="complex",
                success=False,
                duration_s=round(time.time() - t_start, 1),
                output_preview=str(exc)[:100],
            )

    async def _handle_image_gen(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Generate image via pollinations.ai (FLUX.1, free, no key) and send as photo."""
        import io
        import base64

        chat = update.message.chat
        await chat.send_action("upload_photo")
        progress_msg = await update.message.reply_text("🎨 Generando imagen...")

        try:
            brain = router.get_brain("image")
            if not brain:
                from src.brains.image_brain import ImageBrain
                brain = ImageBrain()

            response = await brain.execute(prompt=message_text)

            if response.is_error:
                await progress_msg.edit_text(f"❌ {response.content}")
                return

            if response.content.startswith("__IMAGE_B64__:"):
                b64_data = response.content[len("__IMAGE_B64__:"):]
                image_bytes = base64.b64decode(b64_data)
                elapsed_s = response.duration_ms // 1000

                await progress_msg.delete()
                await update.message.reply_photo(
                    photo=io.BytesIO(image_bytes),
                    caption=(
                        f"🎨 *{message_text[:80]}*\n⏱ {elapsed_s}s · FLUX.1 via pollinations.ai"
                    ),
                    parse_mode="Markdown",
                )
            else:
                await progress_msg.edit_text(response.content)

        except Exception as e:
            logger.error("image_gen_failed", error=str(e), user_id=user_id)
            await progress_msg.edit_text(f"❌ Error generando imagen: {e}")

    async def _handle_email_native(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Compose and send email natively — no Claude CLI subprocess.

        1. Uses openrouter (streaming) to compose email JSON from natural language
        2. Parses {to, subject, body} from the response
        3. Calls send_email() directly in Python
        """
        import json as _json
        import re as _re_e

        progress_msg = await update.message.reply_text(
            "📧 <b>Redactando email...</b> · ⠋",
            parse_mode="HTML",
        )
        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        try:
            compose_prompt = (
                f"Extrae y compone el email solicitado. Responde SOLO con JSON válido, sin markdown:\n"
                f'{{"to": "email@destinatario.com", "subject": "Asunto", "body": "Cuerpo del email"}}\n\n'
                f"Contexto del dueño: royaluniondesign@gmail.com es el email de Ricardo (yo mismo).\n"
                f"Si dice 'envíate', 'mándame', 'a mí', etc → to: royaluniondesign@gmail.com\n\n"
                f"Petición: {message_text}\n\n"
                f"Responde SOLO el JSON."
            )

            brain = router.get_brain("openrouter") if router else None
            if brain is None:
                brain = router.get_brain("haiku") if router else None

            composed: dict = {}  # type: ignore[type-arg]
            if brain and getattr(brain, "supports_streaming", False):
                # Stream compose
                accumulated = ""
                async for chunk in brain.execute_stream(
                    prompt=compose_prompt,
                    working_directory=str(self.settings.approved_directory),
                    timeout_seconds=30,
                ):
                    if chunk.startswith("\x00ERROR:"):
                        break
                    accumulated += chunk
                raw = accumulated.strip()
            else:
                resp = await brain.execute(prompt=compose_prompt) if brain else None
                raw = (resp.content if resp else "").strip()

            # Parse JSON from response (handles ```json ... ``` wrapping too)
            json_match = _re_e.search(r'\{[^{}]+\}', raw, _re_e.DOTALL)
            if json_match:
                try:
                    composed = _json.loads(json_match.group())
                except Exception:
                    pass

            if not composed.get("to") or not composed.get("subject"):
                await progress_msg.edit_text(
                    "❌ No pude extraer destinatario/asunto del mensaje. "
                    "Usa: <code>/email correo@x.com | Asunto | Cuerpo</code>",
                    parse_mode="HTML",
                )
                return

            await progress_msg.edit_text(
                f"📧 Enviando a <code>{escape_html(composed['to'])}</code>...",
                parse_mode="HTML",
            )

            from src.actions import call_tool
            result = await call_tool(
                "send_email",
                to=composed["to"],
                subject=composed["subject"],
                body=composed.get("body", ""),
            )

            await progress_msg.edit_text(
                f"📧 {escape_html(result)}",
                parse_mode="HTML",
            )
            logger.info(
                "email_native_sent",
                to=composed["to"],
                subject=composed["subject"],
            )

        except Exception as e:
            logger.error("email_native_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error enviando email: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def _handle_social_post(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> None:
        """Run social media content pipeline: generate brand image → show in Telegram → post.

        Flow:
          1. Parse request (topic, platform, format)
          2. Generate structured content via Gemini CMO prompt
          3. Render brand image (Anthropic fonts/colors)
          4. Send image as Telegram photo + caption
          5. Post via N8N in background
        """
        import io
        import re as _re_social

        progress_msg = await update.message.reply_text(
            "📱 <b>Generando imagen...</b>",
            parse_mode="HTML",
        )
        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        async def _notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            from src.social.image_gen import PostSpec, generate_post_image
            from src.workflows.social_post import generate_post_content, parse_social_request

            # Detect format from message
            lower = message_text.lower()
            if _re_social.search(r"\b(reel|reels|story|stories|vertical|9.16)\b", lower):
                fmt = "9:16"
            elif _re_social.search(r"\b(landscape|horizontal|wide|4.3|16.9)\b", lower):
                fmt = "4:3"
            else:
                fmt = "1:1"

            parsed = parse_social_request(message_text)
            topic = parsed["topic"] or message_text.strip()
            platform = parsed["platform"]

            await _notify(f"✍️ Generando contenido para <b>{platform}</b>...")

            content = await generate_post_content(topic, platform)

            await _notify("🎨 Renderizando imagen con tipografía Anthropic...")

            spec = PostSpec(
                headline=content["headline"],
                subheadline=content["subheadline"],
                caption=content["caption"],
                tag=content["tag"],
                format=fmt,
            )
            png_bytes = generate_post_image(spec)

            # Delete progress message — about to send photo
            try:
                await progress_msg.delete()
            except Exception:
                pass

            caption_text = content["caption"]
            # Truncate caption if too long for Telegram (max 1024 chars)
            if len(caption_text) > 1020:
                caption_text = caption_text[:1017] + "..."

            await update.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=caption_text,
                filename=f"aura_{platform}_{fmt.replace(':', '')}.png",
            )

            logger.info(
                "social_image_sent",
                platform=platform,
                format=fmt,
                topic=topic[:60],
                size_kb=len(png_bytes) // 1024,
            )

            # Log to publication database (SQLite + Drive/Sheets in background)
            async def _log_publication() -> None:
                try:
                    from src.integrations.publication_db import get_publication_db
                    db = get_publication_db()
                    await db.log_publication(
                        platform=platform,
                        format=fmt,
                        topic=topic,
                        headline=content["headline"],
                        subheadline=content["subheadline"],
                        caption=content["caption"],
                        tag=content["tag"],
                        image_bytes=png_bytes,
                        status="generated",
                    )
                except Exception as exc:
                    logger.warning("publication_log_error", error=str(exc))

            asyncio.create_task(_log_publication())

            # Detect "publicar" / "post now" intent — post directly to Instagram
            import re as _re2
            should_post = bool(_re2.search(
                r"\b(publica|publish|post(?:ea)?|sube?|upload|publicar)\b",
                message_text.lower(),
            ))

            if should_post and platform == "instagram":
                async def _ig_post_background() -> None:
                    try:
                        from src.workflows.instagram_direct import post_image as _ig_post
                        result = await _ig_post(png_bytes, content["caption"])
                        if result["ok"]:
                            await update.message.reply_text(
                                f"✅ Publicado en Instagram!\n🔗 {result['url']}",
                                disable_web_page_preview=True,
                            )
                        else:
                            await update.message.reply_text(
                                f"⚠️ No se pudo publicar: {result['error'][:200]}"
                            )
                    except Exception as exc:
                        logger.warning("ig_post_background_error", error=str(exc))

                asyncio.create_task(_ig_post_background())

        except Exception as e:
            logger.error("social_pipeline_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def _handle_video_gen(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Generate video — cinematic (video_brain) or structured slides (video_compose).

        Cinematic keywords: reel, clip, b-roll, animación, cinematic, AI video
        Structured keywords: slides, presentación, explainer, tutorial
        """
        import io
        import re as _re_v

        chat = update.message.chat
        await chat.send_action("upload_video")
        progress_msg = await update.message.reply_text("🎬 Generando video...")

        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        async def _notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            _structured_kw = r"(?i)\b(slides?|diapositivas?|presentaci[oó]n|explainer|tutorial)\b"
            _cinematic_kw = r"(?i)\b(reel|clip|b-?roll|animaci[oó]n|animation|cinematic|cinemático)\b"

            is_structured = bool(_re_v.search(_structured_kw, message_text))
            is_cinematic = bool(_re_v.search(_cinematic_kw, message_text))

            # Default to cinematic if ambiguous, but prefer structured when explicitly requested
            use_slides = is_structured and not is_cinematic

            if use_slides:
                from src.workflows.video_compose import run_video_pipeline
                result = await run_video_pipeline(
                    prompt=message_text,
                    notify_fn=_notify,
                )

                if result.startswith("http") and (
                    result.endswith(".mp4") or "json2video" in result or "cdn" in result
                ):
                    await _notify("📥 Descargando video...")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                result, timeout=aiohttp.ClientTimeout(total=60)
                            ) as resp:
                                video_bytes = await resp.read()
                        await progress_msg.delete()
                        await update.message.reply_video(
                            video=io.BytesIO(video_bytes),
                            caption=(
                                f"🎬 <b>{escape_html(message_text[:80])}</b>\n"
                                f"📊 json2video structured"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{result}",
                            parse_mode="HTML",
                        )
                else:
                    await progress_msg.edit_text(result, parse_mode="HTML")

            else:
                brain = router.get_brain("video") if router else None
                if not brain:
                    from src.brains.video_brain import VideoBrain
                    brain = VideoBrain()

                response = await brain.execute(prompt=message_text)

                if response.is_error:
                    await progress_msg.edit_text(
                        f"❌ {escape_html(response.content)}",
                        parse_mode="HTML",
                    )
                    return

                video_url: str = response.content
                if video_url.startswith("__VIDEO_URL__:"):
                    video_url = video_url[len("__VIDEO_URL__:"):]

                if video_url.startswith("http"):
                    await _notify("📥 Descargando video...")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                video_url, timeout=aiohttp.ClientTimeout(total=60)
                            ) as resp:
                                video_bytes = await resp.read()
                        provider = response.metadata.get("provider", "AI")
                        await progress_msg.delete()
                        await update.message.reply_video(
                            video=io.BytesIO(video_bytes),
                            caption=(
                                f"🎬 <b>{escape_html(message_text[:80])}</b>\n"
                                f"✨ {provider}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{video_url}",
                            parse_mode="HTML",
                        )
                else:
                    await progress_msg.edit_text(
                        escape_html(video_url),
                        parse_mode="HTML",
                    )

        except Exception as e:
            logger.error("video_gen_failed", error=str(e), user_id=user_id)
            try:
                await progress_msg.edit_text(
                    f"❌ Error generando video: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def agentic_text(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        # Pause AURA's self-improvement loop while Ricardo is sending tasks
        try:
            from ..infra.proactive_loop import set_external_task_active
            set_external_task_active(True)
        except Exception:
            pass  # non-critical — don't block message handling

        # --- Bash passthrough: prefix with ! or $ to skip Claude entirely ---
        if message_text and message_text[0] in ("!", "$"):
            cmd = message_text[1:].strip()
            if cmd:
                from .orchestrator_utils import bash_passthrough
                logger.info("Bash passthrough", user_id=user_id, command=cmd[:100])
                await bash_passthrough(update, cmd)
                return

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        # Telegram flood guard — check if bot is in global flood ban
        try:
            from .flood_guard import remaining_flood_wait
            flood_remaining = remaining_flood_wait()
            if flood_remaining > 0:
                mins = int(flood_remaining // 60)
                secs = int(flood_remaining % 60)
                wait_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
                logger.warning("agentic_text_flood_ban_active", remaining_s=flood_remaining)
                try:
                    await update.message.reply_text(
                        f"⏳ Telegram flood ban activo — reintentando en {wait_str}.\n"
                        f"Tu mensaje está registrado, respondo cuando se levante el ban.",
                    )
                except Exception:
                    pass  # if even this fails, silently ignore
                return
        except Exception:
            pass  # flood guard is non-critical

        chat = update.message.chat
        await chat.send_action("typing")

        # --- Multi-agent squad for complex multi-step/multi-domain tasks ---
        if self._squad is not None and self._squad.is_complex_task(message_text):
            logger.info(
                "squad_routing_activated",
                user_id=user_id,
                message_length=len(message_text),
            )
            squad_progress = await update.message.reply_text(
                "🏢 Squad activado...", parse_mode="Markdown"
            )

            async def _squad_notify(text: str) -> None:
                try:
                    await squad_progress.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass

            try:
                squad_result = await self._squad.run(
                    message_text, notify_fn=_squad_notify
                )
                await update.message.reply_text(squad_result, parse_mode="Markdown")
            except Exception as _sq_err:
                logger.error("squad_agentic_error", error=str(_sq_err))
                try:
                    await squad_progress.edit_text(
                        f"❌ Squad error: {str(_sq_err)[:200]}"
                    )
                except Exception:
                    pass
            return

        # --- Mem0: inject relevant memories into prompt ---
        try:
            from src.context.mempalace_memory import search_memories, format_memories_for_prompt
            memories = await search_memories(message_text, n=4)
            if memories:
                mem_context = format_memories_for_prompt(memories)
                enriched_text = message_text + "\n\n" + mem_context
            else:
                enriched_text = message_text
        except Exception:
            enriched_text = message_text

        # --- Recent conversation window (last 4 exchanges) ---
        try:
            storage = context.bot_data.get("storage")
            if storage and hasattr(storage, "messages"):
                recent = await storage.messages.get_user_messages(user_id, limit=4)
                if recent:
                    # messages come DESC, reverse for chronological order
                    recent = list(reversed(recent))
                    history_lines = []
                    for msg in recent:
                        prompt_preview = (getattr(msg, 'prompt', '') or '')[:120]
                        response_preview = (getattr(msg, 'response', '') or '')[:120]
                        if prompt_preview:
                            history_lines.append(f"[Tú]: {prompt_preview}")
                        if response_preview:
                            history_lines.append(f"[AURA]: {response_preview}")
                    if history_lines:
                        history_ctx = "[Conversación reciente]\n" + "\n".join(history_lines)
                        enriched_text = history_ctx + "\n\n" + enriched_text
        except Exception:
            pass  # history is non-critical

        # --- Smart routing: classify intent and pick optimal brain ---
        from src.observability import get_tracer

        router = context.bot_data.get("brain_router")
        rate_monitor = context.bot_data.get("rate_monitor")
        intent_info = ""
        if router:
            # Use cortex for intelligent routing if available
            if self._cortex is not None:
                try:
                    routed_brain, intent = self._cortex.route(
                        message_text, user_id, rate_monitor=rate_monitor, urgent=False
                    )
                except Exception as _cx_err:
                    logger.warning("cortex_route_fallback", error=str(_cx_err))
                    routed_brain, intent = router.smart_route(
                        message_text, user_id,
                        rate_monitor=rate_monitor, urgent=False,
                    )
            else:
                routed_brain, intent = router.smart_route(
                    message_text, user_id,
                    rate_monitor=rate_monitor, urgent=False,
                )
            try:
                intent_info = (
                    f"{intent.intent.value}:{intent.suggested_brain}({intent.confidence})"
                )
            except Exception:
                intent_info = str(intent)
            logger.info("smart_route_decision", routed=routed_brain, intent=intent_info)

            # ── Native actions: intercept before routing to any CLI brain ──
            from src.economy.intent import Intent as _Intent
            import re as _re2
            _is_send = bool(_re2.search(
                r"(?i)\b(envi[aá]|manda|send|escribe?|redacta?|compone?)\b",
                message_text,
            ))
            if intent is not None and intent.intent == _Intent.EMAIL and _is_send:
                await self._handle_email_native(
                    update, context, router, message_text, user_id,
                )
                return

            # Image generation — bypass LLM, call pollinations.ai directly
            if intent is not None and intent.intent == _Intent.IMAGE:
                await self._handle_image_gen(update, context, router, message_text, user_id)
                return

            # Social media pipeline
            if intent is not None and intent.intent == _Intent.SOCIAL:
                await self._handle_social_post(update, context, message_text)
                return

            # Video generation — cinematic AI video or structured slides
            if intent is not None and intent.intent == _Intent.VIDEO:
                await self._handle_video_gen(
                    update, context, router, message_text, user_id
                )
                return

            # ── Option C: Cognitive Task Router ─────────────────────────────
            try:
                from src.infra.task_router import classify_task, write_external_outcome
                route_decision = await classify_task(
                    message_text, brain_router=router, intent=intent,
                )
                logger.info(
                    "task_router_decision",
                    route=route_decision.route,
                    confidence=route_decision.confidence,
                    reason=route_decision.reason,
                    source=route_decision.source,
                )

                if route_decision.route == "complex" and route_decision.confidence >= 0.75:
                    # Ruta B — conductor 3-layer orchestration
                    await self._handle_conductor_task(
                        update, context, router, message_text, user_id,
                        route_decision=route_decision,
                    )
                    return
            except Exception as _rte:
                logger.debug("task_router_error", error=str(_rte))
                # Fall through to normal brain routing if router fails

            # Route to appropriate brain (haiku/sonnet/opus/gemini)
            if routed_brain != "zero-token":
                _t_start = time.time()
                await self._handle_alt_brain(
                    update, context, router, enriched_text, user_id,
                    brain_name=routed_brain,
                    intent=intent,
                )
                # Write Ruta A outcome to unified task memory
                try:
                    from src.infra.task_router import write_external_outcome
                    write_external_outcome(
                        task=message_text[:80],
                        route="simple",
                        success=True,
                        duration_s=round(time.time() - _t_start, 1),
                        output_preview=f"routed to {routed_brain}",
                    )
                except Exception:
                    pass
                return

        # No router available — use Gemini directly as fallback
        from src.brains.gemini_brain import GeminiBrain
        progress_msg = await update.message.reply_text("🔵 Thinking...")
        try:
            brain = GeminiBrain()
            response = await brain.execute(prompt=message_text)
            await progress_msg.delete()
            if response.is_error:
                await update.message.reply_text(f"❌ {response.content}")
            else:
                await update.message.reply_text(response.content)
        except Exception as e:
            logger.error("fallback_brain_error", error=str(e))
            try:
                await progress_msg.edit_text(f"❌ Error: {e}")
            except Exception:
                pass

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

import time
from typing import TYPE_CHECKING

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from .routing.brain_handler import BrainHandlerMixin
from .routing.conductor_handler import ConductorHandlerMixin
from .routing.media_handler import MediaHandlerMixin
from .routing.content_handler import ContentHandlerMixin

if TYPE_CHECKING:
    from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class AgenticRoutingMixin(
    BrainHandlerMixin,
    ConductorHandlerMixin,
    MediaHandlerMixin,
    ContentHandlerMixin,
):
    """Mixin providing smart routing and brain dispatch logic.

    Must be mixed into MessageOrchestrator which supplies:
      self.settings, self._cortex, self._escape_html(),
      self._start_typing_heartbeat(), self._summarize_tool_input()
    """

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
        from src.observability import get_tracer  # noqa: F401 (imported for side effects)

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

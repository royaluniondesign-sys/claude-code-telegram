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
import os
import re
import time
from typing import TYPE_CHECKING, Any, Dict, Set

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

_CHAT_LOCKS: Dict[int, asyncio.Lock] = {}
_CHAT_PENDING: Dict[int, int] = {}
_CHAT_TASKS: Set[asyncio.Task[Any]] = set()
_CHAT_LAST_SQUAD: Dict[int, float] = {}

# Conservative defaults based on current real usage:
# low volume + mostly chat/haiku paths => keep squad expensive paths tighter.
_SQUAD_COOLDOWN_S = 90
_SQUAD_USAGE_SKIP_THRESHOLD = 0.75
_SQUAD_COSTLY_BRAINS = ("sonnet", "opus", "gemini", "openrouter")

_NEW_MISSION_RE = re.compile(
    r"(?i)^\s*(?:"
    r"nueva?\s+(?:misi[oó]n|tarea|task|proyecto)|"
    r"otra\s+(?:misi[oó]n|tarea)|"
    r"cambiar\s+de\s+tarea|"
    r"empecemos\s+otra"
    r")\b[:\-\s]*"
)
_CONTINUE_MISSION_RE = re.compile(
    r"(?i)^\s*(?:"
    r"continua|contin[uú]a|seguir|sigue|retoma|seguimos|continuemos|continue"
    r")\b[:\-\s]*"
)
_IMPLICIT_CONTINUE_RE = re.compile(
    r"(?i)^\s*(?:y|adem[aá]s|tambi[eé]n|ahora|luego|despu[eé]s)\b"
)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _classify_mission_mode(text: str, has_active_mission: bool) -> tuple[str, str]:
    """Classify mission intent: new, continue, or auto."""
    raw = (text or "").strip()
    if not raw:
        return "auto", raw

    m_new = _NEW_MISSION_RE.match(raw)
    if m_new:
        cleaned = raw[m_new.end():].strip()
        return "new", cleaned or raw

    m_cont = _CONTINUE_MISSION_RE.match(raw)
    if m_cont:
        cleaned = raw[m_cont.end():].strip()
        return "continue", cleaned or raw

    if has_active_mission and len(raw) <= 180 and _IMPLICIT_CONTINUE_RE.match(raw):
        return "continue", raw

    return "auto", raw


def _squad_guardrail_decision(
    chat_id: int,
    message_text: str,
    rate_monitor: Any = None,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    """Return whether squad should run under cost/flow guardrails."""
    now = now_ts if now_ts is not None else time.time()
    cooldown_s = _env_int(
        "AURA_SQUAD_COOLDOWN_S",
        _SQUAD_COOLDOWN_S,
        minimum=10,
        maximum=900,
    )
    usage_skip_threshold = _env_float(
        "AURA_SQUAD_USAGE_SKIP_THRESHOLD",
        _SQUAD_USAGE_SKIP_THRESHOLD,
        minimum=0.50,
        maximum=0.98,
    )

    # Prevent over-fragmented flow: very short prompts rarely need squad.
    if len((message_text or "").strip()) < 60:
        return False, "short_prompt"

    # Avoid firing expensive orchestration repeatedly in bursts.
    last = _CHAT_LAST_SQUAD.get(chat_id)
    if last and (now - last) < cooldown_s:
        return False, "cooldown"

    # Cost-aware guardrail: if costly brains are near saturation, stay single-brain.
    if rate_monitor is not None:
        try:
            for brain in _SQUAD_COSTLY_BRAINS:
                usage = rate_monitor.get_usage(brain)
                if usage.is_rate_limited:
                    return False, f"brain_rate_limited:{brain}"
                pct = usage.usage_pct
                if pct is not None and pct >= usage_skip_threshold:
                    return False, f"brain_pressure:{brain}:{int(pct * 100)}"
        except Exception:
            # Non-critical: if monitor introspection fails, allow normal behavior.
            pass

    return True, "ok"


def _record_routing_trace(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    route: str,
    mission_mode: str,
    reason: str = "",
) -> None:
    """Store compact routing decision history in user_data for diagnostics."""
    try:
        traces = context.user_data.setdefault("routing_trace", [])
        traces.append(
            {
                "ts": int(time.time()),
                "user_id": user_id,
                "route": route,
                "mission_mode": mission_mode,
                "reason": reason[:120],
            }
        )
        if len(traces) > 40:
            del traces[:-40]
    except Exception:
        pass


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
        """Queue text messages per chat so long tasks don't block new intake."""
        if not update.message or update.effective_user is None:
            return

        chat_id = (
            update.effective_chat.id
            if update.effective_chat is not None
            else update.effective_user.id
        )
        lock = _CHAT_LOCKS.setdefault(chat_id, asyncio.Lock())
        pending_before = _CHAT_PENDING.get(chat_id, 0)
        _CHAT_PENDING[chat_id] = pending_before + 1

        async def _dequeue() -> None:
            remaining = _CHAT_PENDING.get(chat_id, 1) - 1
            if remaining <= 0:
                _CHAT_PENDING.pop(chat_id, None)
                if not lock.locked():
                    _CHAT_LOCKS.pop(chat_id, None)
            else:
                _CHAT_PENDING[chat_id] = remaining

        # Preserve original behavior for the first message in a chat:
        # process immediately. Extra messages while busy are queued.
        if pending_before == 0 and not lock.locked():
            try:
                async with lock:
                    await self._agentic_text_impl(update, context)
            finally:
                await _dequeue()
            return

        if pending_before >= 0:
            try:
                await update.message.reply_text(
                    f"📝 Mensaje recibido. Lo pongo en cola (#{pending_before + 1})."
                )
            except Exception:
                pass

        async def _run_serialized() -> None:
            try:
                async with lock:
                    await self._agentic_text_impl(update, context)
            except Exception as exc:
                logger.error(
                    "agentic_text_background_error",
                    chat_id=chat_id,
                    error=str(exc),
                )
                try:
                    if update.message:
                        await update.message.reply_text(
                            "❌ Ocurrió un error procesando tu mensaje. Reintenta en unos segundos."
                        )
                except Exception:
                    pass
            finally:
                await _dequeue()

        task = asyncio.create_task(_run_serialized(), name=f"agentic-text-{chat_id}")
        _CHAT_TASKS.add(task)
        task.add_done_callback(lambda t: _CHAT_TASKS.discard(t))

    async def _agentic_text_impl(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text
        mission_state = context.user_data.setdefault("mission_state", {})
        active_mission = str(mission_state.get("active_prompt", "") or "").strip()
        mode, normalized_text = _classify_mission_mode(
            message_text,
            has_active_mission=bool(active_mission),
        )

        if mode == "new":
            # Explicit user intent: start fresh mission context.
            context.user_data["claude_session_id"] = None
            context.user_data["force_new_session"] = True
            mission_state["active_prompt"] = normalized_text[:280] or message_text[:280]
            mission_state["mode"] = "new"
            message_text = normalized_text or message_text
            try:
                await update.message.reply_text("🆕 Nueva misión detectada. Arranco contexto limpio.")
            except Exception:
                pass
        elif mode == "continue":
            mission_state["mode"] = "continue"
            if active_mission:
                message_text = (
                    "[Misión activa]\n"
                    f"{active_mission}\n\n"
                    "[Seguimiento del usuario]\n"
                    f"{normalized_text or message_text}"
                )
        else:
            mission_state["mode"] = "auto"
            # Treat substantial prompts as potential new active mission memory.
            if len(normalized_text) >= 32:
                mission_state["active_prompt"] = normalized_text[:280]
            message_text = normalized_text

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
        rate_monitor = context.bot_data.get("rate_monitor")
        chat_id = update.effective_chat.id if update.effective_chat else user_id

        # --- Multi-agent squad for complex multi-step/multi-domain tasks ---
        if self._squad is not None and self._squad.is_complex_task(message_text):
            allow_squad, guard_reason = _squad_guardrail_decision(
                chat_id=chat_id,
                message_text=message_text,
                rate_monitor=rate_monitor,
            )
            if not allow_squad:
                logger.info(
                    "squad_guardrail_skip",
                    user_id=user_id,
                    reason=guard_reason,
                    message_length=len(message_text),
                )
                _record_routing_trace(
                    context,
                    user_id=user_id,
                    route="single_brain",
                    mission_mode=mission_state.get("mode", "auto"),
                    reason=f"squad_skipped:{guard_reason}",
                )
            else:
                _CHAT_LAST_SQUAD[chat_id] = time.time()
                _record_routing_trace(
                    context,
                    user_id=user_id,
                    route="squad",
                    mission_mode=mission_state.get("mode", "auto"),
                    reason="complex_task",
                )
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
            _record_routing_trace(
                context,
                user_id=user_id,
                route=routed_brain,
                mission_mode=mission_state.get("mode", "auto"),
                reason=f"intent:{intent_info}",
            )

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

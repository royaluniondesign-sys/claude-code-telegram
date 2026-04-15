"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


from .handlers.fleet_commands import FleetCommandsMixin
from .handlers.zero_token import ZeroTokenMixin


class MessageOrchestrator(ZeroTokenMixin, FleetCommandsMixin):
    """Routes messages based on mode. Single entry point for all Telegram updates.

    Zero-token and fleet command handlers are defined in mixin classes
    to keep this file under 800 lines. See:
      - handlers/zero_token.py  — system, workspace, brain, voice, workflow commands
      - handlers/fleet_commands.py — machines, ssh, fleet, nodes, dispatch commands
    """

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        # Initialize cortex if brain_router is available
        self._cortex: Any = None
        self._squad: Any = None
        router = deps.get("brain_router")
        if router is not None:
            try:
                from ..brains.cortex import AuraCortex
                self._cortex = AuraCortex(router)
                logger.info("cortex_attached", status="ok")
            except Exception as _ce:
                logger.warning("cortex_init_failed", error=str(_ce))
            try:
                from ..agents.squad import get_squad
                self._squad = get_squad(router)
                logger.info("agent_squad_attached", status="ok")
            except Exception as _se:
                logger.warning("agent_squad_init_failed", error=str(_se))

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            # ── Core ──────────────────────────────────────────────────────
            ("start",    self.agentic_start),     # greeting / session init
            ("new",      self.agentic_new),        # reset conversation
            ("help",     self._zt_help),           # command reference
            ("status",   self._zt_status_full),    # compact dashboard
            ("restart",  command.restart_command), # restart bot process
            # ── Shell & files ─────────────────────────────────────────────
            ("sh",       self._zt_sh),             # /sh <cmd> — direct shell
            ("git",      self._zt_git),            # /git [subcmd]
            ("repo",     self.agentic_repo),       # /repo [name] — switch project
            # ── Brains & routing ──────────────────────────────────────────
            ("brain",    self._zt_brain),          # /brain [name|auto]
            ("task",     self._zt_task),           # /task <brain> <prompt>
            ("queue",    self._zt_queue),           # /queue [urgent] <desc> — background + meta-router
            ("limits",   self._zt_limits),         # rate limits + usage
            ("costs",    self._zt_costs),          # token economy stats
            # ── Memory ────────────────────────────────────────────────────
            ("memory",   self._zt_memory),         # /memory [add|client|clear|...]
            # ── Web & search ──────────────────────────────────────────────
            ("web",      self._zt_web),            # /web <url> — analyze via gemini
            ("search",   self._zt_search),         # /search <query> — force web search
            # ── Communication ─────────────────────────────────────────────
            ("email",    self._zt_email),          # /email to | subject | body
            # ── System & services ─────────────────────────────────────────
            ("health",   self._zt_health),         # watchdog full health check
            ("terminal", self._zt_terminal),       # Termora one-tap link
            ("dashboard", self._zt_dashboard),     # dashboard URL
            # ── Workflows ─────────────────────────────────────────────────
            ("standup",  self._zt_standup),        # daily standup report
            ("report",   self._zt_report),         # weekly report
            ("triage",   self._zt_triage),         # email triage
            ("followup", self._zt_followup),       # client followup
            # ── Fleet & SuperNodes (registered but not in menu) ──────────
            ("machines", self._zt_machines),
            ("ssh",      self._zt_ssh),
            ("fleet",    self._zt_fleet),
            ("nodes",    self._zt_nodes),
            ("dispatch", self._zt_dispatch),
            # ── Social media ──────────────────────────────────────────────
            ("post",     self._zt_post),           # /post <platform> <type> <topic>
            ("posts",    self._zt_posts),          # /posts — recent publications list
            ("ig_auth",  self._zt_ig_auth),        # /ig-auth <app_secret> — Instagram OAuth
            # ── Google Drive / Sheets ─────────────────────────────────────
            ("drive",    self._zt_drive),          # /drive [setup|status|auth] — Drive integration
            # ── Video generation ──────────────────────────────────────────
            ("video",    self._zt_video),          # /video [cinematic|slides] <prompt>
            # ── Power user ────────────────────────────────────────────────
            ("verbose",  self.agentic_verbose),    # output verbosity 0|1|2
            ("speak",    self._zt_speak),          # TTS voice output
            ("voz",      self._voz_command),          # /voz [on|off] — voice toggle per user
            # ── Diagnostics ───────────────────────────────────────────────
            ("diagnose", self._zt_diagnose),       # full self-healer diagnostic
            # ── Agent Squad ───────────────────────────────────────────────
            ("team",     self._zt_team),           # /team [task] — multi-agent squad
            # ── 3-Layer Conductor ─────────────────────────────────────────
            ("c",        self._zt_conductor),      # /c <task> — 3-layer conductor shortcut
            ("conductor",self._zt_conductor),      # /conductor <task> — 3-layer orchestrator
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Track registered commands so _unknown_command can skip them
        self._registered_commands: set = {cmd for cmd, _ in handlers}

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands → route to haiku (Claude CLI handles skills)
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                # ── Core ──────────────────────────────────────────────────
                BotCommand("start",    "Iniciar AURA"),
                BotCommand("new",      "Nueva sesión"),
                BotCommand("help",     "Comandos disponibles"),
                BotCommand("status",   "Estado completo"),
                BotCommand("health",   "Salud del sistema"),
                BotCommand("diagnose", "Diagnóstico automático"),
                # ── Brains & Memoria ──────────────────────────────────────
                BotCommand("brain",    "Ver / cambiar brain activo"),
                BotCommand("limits",   "Uso y rate limits"),
                BotCommand("memory",   "Memoria Mem0 — hechos aprendidos"),
                # ── Shell & Dev ────────────────────────────────────────────
                BotCommand("sh",       "Ejecutar comando shell"),
                BotCommand("git",      "Git status / log / diff"),
                BotCommand("repo",     "Cambiar proyecto/workspace"),
                # ── Web ───────────────────────────────────────────────────
                BotCommand("web",      "Analizar URL con Gemini"),
                BotCommand("search",   "Búsqueda web"),
                # ── Comunicación ──────────────────────────────────────────
                BotCommand("email",    "Enviar email — /email to | asunto | cuerpo"),
                BotCommand("post",     "Social media — /post instagram carousel 5 sobre X"),
                BotCommand("standup",  "Daily standup — git + pendientes"),
                BotCommand("report",   "Reporte semanal"),
                # ── Herramientas ──────────────────────────────────────────
                BotCommand("terminal", "Abrir Termora (terminal web)"),
                BotCommand("dashboard","Dashboard en localhost:8080"),
                BotCommand("restart",  "Reiniciar bot"),
                BotCommand("team",     "Squad multi-agente — /team o /team <tarea>"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- ⚡ Zero-token handlers (no Claude, no tokens) ---

    # Zero-token commands (_zt_ls, _zt_pwd, _zt_git, _zt_health, _zt_terminal,
    # _zt_context, _zt_sh, _zt_inbox, _zt_calendar, _zt_limits, _zt_costs,
    # _zt_brain, _zt_brains, _zt_dashboard, _zt_speak, _zt_standup, _zt_report,
    # _zt_triage, _zt_followup) are defined in handlers/zero_token.py (ZeroTokenMixin).
    #
    # Fleet commands (_zt_machines, _zt_ssh, _zt_fleet, _zt_nodes, _zt_dispatch)
    # are defined in handlers/fleet_commands.py (FleetCommandsMixin).

    # (remaining _zt_ methods removed — now in mixin classes)

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        # Clear conversation history on /start
        context.user_data["ollama_history"] = []
        await update.message.reply_text(
            f"Hola {safe_name} 👋 AURA lista."
            f"{sync_line}",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        # Clear per-brain conversation sessions so next message starts fresh
        router = context.bot_data.get("brain_router")
        if router:
            user_key = str(update.effective_user.id)
            for brain in router._brains.values():
                if hasattr(brain, "clear_session"):
                    brain.clear_session(user_key)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact status — directory, brain, health."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )

        router = context.bot_data.get("brain_router")
        brain_name = "ollama"
        if router:
            brain_name = router.get_active_brain_name(update.effective_user.id)

        rate_monitor = context.bot_data.get("rate_monitor")
        usage_str = ""
        if rate_monitor:
            try:
                stats = rate_monitor.get_stats(brain_name)
                if stats:
                    usage_str = f" · {stats.get('requests', 0)} requests"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {current_dir}\n🧠 {brain_name}{usage_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def _zt_team(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🏢 /team — Show agent team or run a multi-agent task.

        Usage:
          /team              — show team status
          /team <task>       — run task with multi-agent squad
        """
        from ..agents.squad import get_squad

        router = context.bot_data.get("brain_router")
        squad = get_squad(router)

        if squad is None:
            await update.message.reply_text(
                "❌ Squad not initialized", parse_mode="HTML"
            )
            return

        args = (update.message.text or "").split(maxsplit=1)
        task = args[1].strip() if len(args) > 1 else ""

        if not task:
            await update.message.reply_text(squad.team_status(), parse_mode="HTML")
            return

        progress_msg = await update.message.reply_text(
            "🏢 <b>AURA Squad</b> activado...", parse_mode="HTML"
        )

        async def notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            result = await squad.run(task, notify_fn=notify)
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception as exc:
            logger.error("squad_run_error", error=str(exc))
            try:
                await progress_msg.edit_text(
                    f"❌ Squad error: {self._escape_html(str(exc)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    async def _zt_conductor(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎼 /conductor <task> — Run task through the 3-layer brain orchestrator.

        Claude analyzes the task, assigns brains to 3 layers (Analysis →
        Synthesis → Execution), runs them, and returns the final output.
        Live progress visible in the dashboard at /api/stream/orchestration.

        Usage:
          /conductor research latest AI trends and write a summary
          /c analyze this Python file and suggest optimizations
        """
        from ..brains.conductor import get_conductor, Conductor, set_conductor

        router = context.bot_data.get("brain_router")
        if not router:
            await update.message.reply_text("❌ Brain router not available")
            return

        args = (update.message.text or "").split(maxsplit=1)
        task = args[1].strip() if len(args) > 1 else ""

        if not task:
            await update.message.reply_text(
                "🎼 <b>Conductor — Orquestador 3 capas</b>\n\n"
                "Uso: <code>/conductor &lt;tarea&gt;</code>\n"
                "Ejemplo: <code>/conductor investiga tendencias de IA y escribe un resumen</code>\n\n"
                "Claude analiza → asigna brains → ejecuta en capas → entrega resultado.\n"
                "Ve el progreso en vivo en el dashboard.",
                parse_mode="HTML",
            )
            return

        progress_msg = await update.message.reply_text(
            "🎼 <b>Conductor iniciando…</b>\nClaude está diseñando el plan de ejecución.",
            parse_mode="HTML",
        )

        # Build notify fn that updates the progress message
        _last_text = [""]

        async def notify(text: str) -> None:
            try:
                if text != _last_text[0]:
                    _last_text[0] = text
                    await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        conductor = get_conductor(router, notify_fn=notify)
        if conductor is None:
            conductor = Conductor(router, notify_fn=notify)
            set_conductor(conductor)
        else:
            # Update notify fn for this run
            conductor._notify = notify

        try:
            result = await conductor.run(task, source="manual")

            duration_s = round(result.total_duration_ms / 1000, 1)
            plan_info = ""
            if result.plan:
                layers = result.plan.layers_used
                plan_info = (
                    f"\n<i>{result.plan.total_steps} steps · "
                    f"{len(layers)} layer(s) · {duration_s}s</i>"
                )

            if result.is_error or not result.final_output:
                await update.message.reply_text(
                    f"⚠️ <b>Conductor completó con errores</b>{plan_info}\n\n"
                    f"{self._escape_html(result.error or 'No output produced')}",
                    parse_mode="HTML",
                )
            else:
                # Send final output
                output = result.final_output
                header = f"🎼 <b>Conductor completó</b>{plan_info}\n\n"
                full = header + self._escape_html(output)
                # Telegram limit is 4096 chars
                if len(full) > 4000:
                    await update.message.reply_text(
                        header + self._escape_html(output[:3600]) + "\n\n<i>…truncado</i>",
                        parse_mode="HTML",
                    )
                else:
                    await update.message.reply_text(full, parse_mode="HTML")

            try:
                await progress_msg.delete()
            except Exception:
                pass

        except Exception as exc:
            logger.error("conductor_run_error", error=str(exc))
            try:
                await progress_msg.edit_text(
                    f"❌ Conductor error: {self._escape_html(str(exc)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    async def _voz_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/voz [on|off] — toggle voice responses for this user."""
        from .features.voice_tts import handle_voz_command
        await handle_voz_command(update, context)

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(new_text)
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def _unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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

    async def _bash_passthrough(
        self, update: Update, command: str
    ) -> bool:
        """Execute shell command directly without Claude. Returns True if handled."""
        import asyncio

        try:
            current_dir = str(Path.home())
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=current_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode().strip()
            err = stderr.decode().strip()

            result = output if output else err if err else "(no output)"
            # Truncate for Telegram's 4096 char limit
            if len(result) > 3900:
                result = result[:3900] + "\n... (truncated)"

            await update.message.reply_text(
                f"<pre>{self._escape_html(result)}</pre>",
                parse_mode="HTML",
            )
            return True
        except asyncio.TimeoutError:
            await update.message.reply_text("⏱ Command timed out (30s)")
            return True
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return True

    async def _handle_alt_brain(
        self,
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

        brain = router.get_brain(brain_name) if brain_name else router.get_active_brain(user_id)
        if brain is None:
            brain = router.get_active_brain(user_id)

        tracer = get_tracer()
        trace_ctx = tracer.trace_brain(
            brain_name=brain.name, user_id=user_id,
            message=message_text, metadata={"handler": "alt_brain"},
        )

        _SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        _PHASES = [(0, "pensando"), (8, "procesando"), (20, "trabajando"), (50, "aún trabajando ⏳")]

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

        _start = time.time()
        current_dir = str(context.user_data.get("current_directory", self.settings.approved_directory))
        rate_monitor = context.bot_data.get("rate_monitor")
        original_brain = brain

        # Initial status message — appears immediately
        progress_msg = await update.message.reply_text(
            f"{brain.emoji} <b>{brain.display_name}</b> · ⠋",
            parse_mode="HTML",
        )

        # Typing indicator — always visible in chat header throughout response
        _typing_task = self._start_typing_heartbeat(update.effective_chat, interval=3.0)

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
                                f"{header}\n\n{self._escape_html(display)}▌",
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
                                prompt=message_text, working_directory=current_dir,
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
                    f"{header}\n\n{self._escape_html(content)}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            tracer.end_trace(ctx=trace_ctx, output=content[:500], duration_ms=int(elapsed_total * 1000))

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

        # ── PATH B: Non-streaming (Claude CLI, Gemini CLI) ─────────────────
        heartbeat_task: Optional["asyncio.Task[None]"] = None

        async def _heartbeat(current_brain: Any) -> None:
            try:
                frame = 0
                while True:
                    await asyncio.sleep(1.5)   # 1.5s — same cadence as streaming edits
                    frame += 1
                    elapsed = time.time() - _start
                    spin = _SPIN[frame % len(_SPIN)]
                    phase = _phase(elapsed)
                    dur = _dur(elapsed) if elapsed >= 1.5 else ""
                    suffix = f" · {dur}" if dur else ""
                    try:
                        await progress_msg.edit_text(
                            f"{current_brain.emoji} <b>{current_brain.display_name}</b>"
                            f" · {spin} {phase}{suffix}",
                            parse_mode="HTML",
                        )
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
                cascade_chain = router.get_cascade_chain(brain.name) if hasattr(router, "get_cascade_chain") else []
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
                    logger.info("brain_cascade", from_brain=brain.name,
                                to_brain=fallback_name, reason=reason)
                    await _stop()
                    await progress_msg.edit_text(
                        f"↗️ <b>{brain.display_name}</b> [{reason}]\n"
                        f"   → <b>{fallback.display_name}</b>...",
                        parse_mode="HTML",
                    )
                    heartbeat_task = asyncio.ensure_future(_heartbeat(fallback))  # type: ignore[assignment]
                    response = await fallback.execute(
                        prompt=message_text, working_directory=current_dir,
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
                f"{prefix}{header}\n\n{self._escape_html(content)}",
                parse_mode="HTML",
            )

            tracer.end_trace(ctx=trace_ctx, output=content[:500],
                             cost=response.cost, duration_ms=response.duration_ms)

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
                    asyncio.ensure_future(send_voice_response(update, context, content))
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
                    f"❌ {brain.emoji} {self._escape_html(str(e)[:400])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special chars for Telegram."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def _handle_image_gen(
        self,
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
                    caption=f"🎨 *{message_text[:80]}*\n⏱ {elapsed_s}s · FLUX.1 via pollinations.ai",
                    parse_mode="Markdown",
                )
            else:
                await progress_msg.edit_text(response.content)

        except Exception as e:
            logger.error("image_gen_failed", error=str(e), user_id=user_id)
            await progress_msg.edit_text(f"❌ Error generando imagen: {e}")

    async def _handle_email_native(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Compose and send email natively — no Claude CLI subprocess, no harness prompts.

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
        _typing_task = self._start_typing_heartbeat(update.effective_chat, interval=3.0)

        try:
            # Step 1: ask brain to extract/compose email as JSON
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

            composed: dict = {}
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
                f"📧 Enviando a <code>{self._escape_html(composed['to'])}</code>...",
                parse_mode="HTML",
            )

            # Step 2: send directly via Python
            from src.actions import call_tool
            result = await call_tool(
                "send_email",
                to=composed["to"],
                subject=composed["subject"],
                body=composed.get("body", ""),
            )

            await progress_msg.edit_text(
                f"📧 {self._escape_html(result)}",
                parse_mode="HTML",
            )
            logger.info("email_native_sent", to=composed["to"], subject=composed["subject"])

        except Exception as e:
            logger.error("email_native_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error enviando email: {self._escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def _handle_social_post(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> None:
        """Run social media content pipeline: generate brand image → show in Telegram → post via N8N.

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
        _typing_task = self._start_typing_heartbeat(update.effective_chat, interval=3.0)

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

            # Parse platform + topic
            parsed = parse_social_request(message_text)
            topic = parsed["topic"] or message_text.strip()
            platform = parsed["platform"]

            await _notify(f"✍️ Generando contenido para <b>{platform}</b>...")

            # Generate structured content via Gemini CMO
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

            # Send the image as a Telegram photo with caption
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
            import asyncio as _asyncio
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
            _asyncio.create_task(_log_publication())

            # Detect "publicar" / "post now" intent — post directly to Instagram
            import asyncio as _asyncio
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
                _asyncio.create_task(_ig_post_background())

        except Exception as e:
            logger.error("social_pipeline_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error: {self._escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def _handle_video_gen(
        self,
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

        _typing_task = self._start_typing_heartbeat(update.effective_chat, interval=3.0)

        async def _notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            # Decide route: structured (slides) vs cinematic
            _structured_kw = r"(?i)\b(slides?|diapositivas?|presentaci[oó]n|explainer|tutorial)\b"
            _cinematic_kw = r"(?i)\b(reel|clip|b-?roll|animaci[oó]n|animation|cinematic|cinemático)\b"

            is_structured = bool(_re_v.search(_structured_kw, message_text))
            is_cinematic = bool(_re_v.search(_cinematic_kw, message_text))

            # Default to cinematic if ambiguous, but prefer structured when explicitly requested
            use_slides = is_structured and not is_cinematic

            if use_slides:
                # Structured video via json2video
                from src.workflows.video_compose import run_video_pipeline

                result = await run_video_pipeline(
                    prompt=message_text,
                    notify_fn=_notify,
                )

                # If result looks like a URL, download and send as video
                if result.startswith("http") and (
                    result.endswith(".mp4") or "json2video" in result or "cdn" in result
                ):
                    await _notify("📥 Descargando video...")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(result, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                                video_bytes = await resp.read()
                        await progress_msg.delete()
                        await update.message.reply_video(
                            video=io.BytesIO(video_bytes),
                            caption=f"🎬 <b>{self._escape_html(message_text[:80])}</b>\n📊 json2video structured",
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{result}",
                            parse_mode="HTML",
                        )
                else:
                    # Likely a mock preview or error text
                    await progress_msg.edit_text(result, parse_mode="HTML")

            else:
                # Cinematic video via VideoBrain cascade
                brain = router.get_brain("video") if router else None
                if not brain:
                    from src.brains.video_brain import VideoBrain
                    brain = VideoBrain()

                response = await brain.execute(prompt=message_text)

                if response.is_error:
                    await progress_msg.edit_text(
                        f"❌ {self._escape_html(response.content)}",
                        parse_mode="HTML",
                    )
                    return

                video_url: str = response.content
                if video_url.startswith("__VIDEO_URL__:"):
                    video_url = video_url[len("__VIDEO_URL__:"):]

                # Try to download and send as Telegram video
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
                                f"🎬 <b>{self._escape_html(message_text[:80])}</b>\n"
                                f"✨ {provider}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        # Fall back to URL text
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{video_url}",
                            parse_mode="HTML",
                        )
                else:
                    await progress_msg.edit_text(
                        self._escape_html(video_url),
                        parse_mode="HTML",
                    )

        except Exception as e:
            logger.error("video_gen_failed", error=str(e), user_id=user_id)
            try:
                await progress_msg.edit_text(
                    f"❌ Error generando video: {self._escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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
                logger.info(
                    "Bash passthrough",
                    user_id=user_id,
                    command=cmd[:100],
                )
                await self._bash_passthrough(update, cmd)
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
                    routed_brain, intent = router.smart_route(message_text, user_id,
                                                              rate_monitor=rate_monitor,
                                                              urgent=False)
            else:
                routed_brain, intent = router.smart_route(message_text, user_id,
                                                          rate_monitor=rate_monitor,
                                                          urgent=False)
            try:
                intent_info = f"{intent.intent.value}:{intent.suggested_brain}({intent.confidence})"
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

            # Social media pipeline — generate images + captions → N8N → Instagram/Twitter/LinkedIn
            if intent is not None and intent.intent == _Intent.SOCIAL:
                await self._handle_social_post(update, context, message_text)
                return

            # Video generation — cinematic AI video or structured slides
            if intent is not None and intent.intent == _Intent.VIDEO:
                await self._handle_video_gen(update, context, router, message_text, user_id)
                return

            # Route to appropriate brain (haiku/sonnet/opus/gemini)
            if routed_brain != "zero-token":
                await self._handle_alt_brain(
                    update, context, router, enriched_text, user_id,
                    brain_name=routed_brain,
                    intent=intent,
                )
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

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with active brain (Ollama/Gemini — no Claude)
        router = context.bot_data.get("brain_router")
        if router:
            await self._handle_alt_brain(
                update, context, router, prompt, user_id,
                brain_name=router.active_brain_name,
            )
        else:
            from src.brains.ollama_brain import OllamaBrain
            brain = OllamaBrain()
            response = await brain.execute(prompt=prompt)
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await update.message.reply_text(
                response.content if not response.is_error else f"❌ {response.content}"
            )

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo via active brain (Ollama/Gemini)."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "photo_processing_failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> brain, with local Whisper (no API key)."""
        user_id = update.effective_user.id
        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("🎤 Transcribiendo...")

        try:
            voice = update.message.voice

            # Download voice data
            file = await voice.get_file()
            voice_bytes = bytes(await file.download_as_bytearray())

            # Try local Whisper first (zero API, runs on M4)
            transcription = None
            try:
                from ..voice.transcriber import transcribe_audio

                transcription = await transcribe_audio(voice_bytes)
                logger.info("local_whisper_ok", length=len(transcription))
            except Exception as whisper_err:
                logger.warning("local_whisper_failed", error=str(whisper_err))

                # Fallback to API-based voice handler if configured
                features = context.bot_data.get("features")
                voice_handler = features.get_voice_handler() if features else None
                if voice_handler:
                    processed = await voice_handler.process_voice_message(
                        voice, update.message.caption
                    )
                    transcription = processed.transcription
                else:
                    await progress_msg.edit_text(
                        "❌ Transcripción no disponible. "
                        "Whisper local falló y no hay API configurada."
                    )
                    return

            if not transcription or not transcription.strip():
                await progress_msg.edit_text("No se pudo transcribir el audio.")
                return

            # Build prompt with transcription
            caption = update.message.caption or "Mensaje de voz"
            prompt = f"{caption}:\n\n{transcription}"

            await progress_msg.edit_text(
                f"🎤 _{transcription[:100]}{'...' if len(transcription) > 100 else ''}_\n\n⏳ Procesando...",
                parse_mode="Markdown",
            )

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "voice_processing_failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
    ) -> None:
        """Run a media-derived prompt through active brain (Ollama/Gemini)."""
        router = context.bot_data.get("brain_router")
        if router:
            # Delete progress message, _handle_alt_brain shows its own
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await self._handle_alt_brain(
                update, context, router, prompt, user_id,
                brain_name=router.active_brain_name,
            )
        else:
            from src.brains.ollama_brain import OllamaBrain
            brain = OllamaBrain()
            response = await brain.execute(prompt=prompt)
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await update.message.reply_text(
                response.content if not response.is_error else f"❌ {response.content}"
            )

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )


# ---------------------------------------------------------------------------
# Orchestrator delegation helpers (Ollama → CLI)
# ---------------------------------------------------------------------------

_DELEGATE_RE = re.compile(r"<<DELEGATE:(\w+)>>\s*(.*)", re.DOTALL)

_CLI_MAP: dict[str, dict[str, Any]] = {
    # shell — fastest, no LLM, deterministic
    "sh":       {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    "bash":     {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    "shell":    {"cmd": "bash",     "mode": "sh",       "emoji": "⚡"},
    # cline — local Ollama, zero cost, code editing
    "cline":    {"cmd": "cline",    "mode": "cline",    "emoji": "🟣"},
    # opencode — free tier via OpenRouter, code gen/analysis
    "opencode": {"cmd": "opencode", "mode": "opencode", "emoji": "🔶"},
    # codex — OpenAI subscription, fast single-file code gen
    "codex":    {"cmd": "codex",    "mode": "codex",    "emoji": "🟢"},
    # claude — Anthropic subscription (escalation only)
    "claude":   {"cmd": "claude",   "mode": "claude",   "emoji": "🟠"},
}


def _parse_delegation(content: str) -> tuple[str, str] | None:
    """Parse <<DELEGATE:cli_name>> from Ollama response."""
    m = _DELEGATE_RE.search(content)
    if not m:
        return None
    cli_name = m.group(1).lower().strip()
    cli_prompt = m.group(2).strip()
    if cli_name not in _CLI_MAP or not cli_prompt:
        return None
    return cli_name, cli_prompt


async def _execute_cli(
    cli_name: str, prompt: str, cwd: str, timeout: int = 120
) -> str:
    """Execute a CLI tool and return its output."""
    import os
    import shutil

    info = _CLI_MAP.get(cli_name)
    if not info:
        return f"Unknown CLI: {cli_name}"

    extra_paths = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin")
    env_path = f"{extra_paths}:{os.environ.get('PATH', '')}"
    cmd_path = shutil.which(info["cmd"], path=env_path)
    if not cmd_path:
        return f"{cli_name} not installed."

    env = os.environ.copy()
    env["PATH"] = env_path

    # Build command per CLI type (all non-interactive)
    mode = info["mode"]
    if mode == "sh":
        # bash -c "command"
        args = [cmd_path, "-c", prompt]
    elif mode == "cline":
        # cline -m qwen2.5:7b -a "prompt" -y  (act + yolo = non-interactive)
        args = [cmd_path, "-m", "qwen2.5:7b", "-a", prompt, "-y"]
    elif mode == "opencode":
        # opencode run "prompt"
        args = [cmd_path, "run", prompt]
    elif mode == "codex":
        # codex exec "prompt" --full-auto
        args = [cmd_path, "exec", prompt, "--full-auto"]
    elif mode == "claude":
        # claude -p "prompt" --model sonnet
        args = [cmd_path, "-p", prompt, "--model", "sonnet", "--output-format", "text"]
    else:
        args = [cmd_path, prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip()
        if not output and stderr:
            output = stderr.decode().strip()
        # Parse opencode JSON output to extract text
        if cli_name == "opencode" and output:
            output = _parse_opencode_json(output)
        return output or "(no output)"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return f"{cli_name} timed out after {timeout}s"
    except Exception as e:
        return f"{cli_name} error: {e}"


def _parse_opencode_json(raw: str) -> str:
    """Extract text parts from opencode --format json output."""
    import json as _json
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
            if event.get("type") == "text":
                part = event.get("part", {})
                text = part.get("text", "")
                if text:
                    texts.append(text)
        except _json.JSONDecodeError:
            continue
    return "\n".join(texts) if texts else raw

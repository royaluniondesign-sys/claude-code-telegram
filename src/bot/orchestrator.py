"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface with commands, no inline keyboards. In
classic mode, delegates to existing full-featured handlers.

Actual handler logic lives in focused sibling modules:
  orchestrator_commands.py  — agentic command handlers
  orchestrator_media.py     — document/photo/voice handlers
  orchestrator_routing.py   — smart routing + brain dispatch (agentic_text)
  orchestrator_utils.py     — shared utilities, helpers, delegation
"""

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config.settings import Settings
from ..infra.launch_agent import ensure_launch_agent_is_running
from .handlers.fleet_commands import FleetCommandsMixin
from .handlers.zero_token import ZeroTokenMixin
from .orchestrator_commands import AgenticCommandsMixin
from .orchestrator_media import AgenticMediaMixin
from .orchestrator_routing import AgenticRoutingMixin
from .orchestrator_utils import (
    escape_html,
    format_verbose_progress,
    make_stream_callback,
    send_images,
    start_typing_heartbeat,
    summarize_tool_input,
)

logger = structlog.get_logger()


class MessageOrchestrator(
    ZeroTokenMixin,
    FleetCommandsMixin,
    AgenticCommandsMixin,
    AgenticMediaMixin,
    AgenticRoutingMixin,
):
    """Routes messages based on mode. Single entry point for all Telegram updates.

    Handler logic is organised across mixin modules to keep files focused:
      - handlers/zero_token.py    — system, workspace, brain, voice, workflow commands
      - handlers/fleet_commands.py — machines, ssh, fleet, nodes, dispatch commands
      - orchestrator_commands.py  — agentic command handlers (start, new, repo, etc.)
      - orchestrator_media.py     — document/photo/voice media handlers
      - orchestrator_routing.py   — smart routing + brain dispatch (agentic_text)
      - orchestrator_utils.py     — shared utilities, helpers, delegation
    """

    def __init__(self, settings: Settings, deps: Dict[str, Any]) -> None:
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

    # ------------------------------------------------------------------
    # Utility shims — expose shared helpers as instance methods so mixins
    # can call self._escape_html() / self._start_typing_heartbeat() etc.
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special chars for Telegram."""
        return escape_html(text)

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any, interval: float = 2.0
    ) -> "Any":
        """Start a background typing indicator task."""
        return start_typing_heartbeat(chat, interval)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        return summarize_tool_input(tool_name, tool_input)

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        return format_verbose_progress(activity_log, verbose_level, start_time)

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        mcp_images: Optional[List[Any]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[Any] = None,
    ) -> Optional[Callable[..., Any]]:
        """Create a stream callback for verbose progress updates."""
        return make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images,
            approved_directory,
            draft_streamer,
        )

    async def _send_images(
        self,
        update: Update,
        images: List[Any],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents."""
        return await send_images(
            update, images, reply_to_message_id, caption, caption_parse_mode
        )

    # ------------------------------------------------------------------
    # Dependency injection wrapper
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Thread routing helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

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
            ("start",     self.agentic_start),      # greeting / session init
            ("new",       self.agentic_new),         # reset conversation
            ("help",      self._zt_help),            # command reference
            ("status",    self._zt_status_full),     # compact dashboard
            ("restart",   command.restart_command),  # restart bot process
            # ── Shell & files ─────────────────────────────────────────────
            ("sh",        self._zt_sh),              # /sh <cmd> — direct shell
            ("git",       self._zt_git),             # /git [subcmd]
            ("repo",      self.agentic_repo),        # /repo [name] — switch project
            # ── Brains & routing ──────────────────────────────────────────
            ("brain",     self._zt_brain),           # /brain [name|auto]
            ("task",      self._zt_task),            # /task <brain> <prompt>
            ("queue",     self._zt_queue),            # /queue [urgent] <desc>
            ("limits",    self._zt_limits),          # rate limits + usage
            ("costs",     self._zt_costs),           # token economy stats
            # ── Memory ────────────────────────────────────────────────────
            ("memory",    self._zt_memory),          # /memory [add|client|clear|...]
            # ── Web & search ──────────────────────────────────────────────
            ("web",       self._zt_web),             # /web <url> — analyze via gemini
            ("search",    self._zt_search),          # /search <query> — force web search
            # ── Communication ─────────────────────────────────────────────
            ("email",     self._zt_email),           # /email to | subject | body
            # ── System & services ─────────────────────────────────────────
            ("health",    self._zt_health),          # watchdog full health check
            ("terminal",  self._zt_terminal),        # Termora one-tap link
            ("dashboard", self._zt_dashboard),       # dashboard URL
            # ── Workflows ─────────────────────────────────────────────────
            ("standup",   self._zt_standup),         # daily standup report
            ("report",    self._zt_report),          # weekly report
            ("triage",    self._zt_triage),          # email triage
            ("followup",  self._zt_followup),        # client followup
            # ── Fleet & SuperNodes (registered but not in menu) ──────────
            ("machines",  self._zt_machines),
            ("ssh",       self._zt_ssh),
            ("fleet",     self._zt_fleet),
            ("nodes",     self._zt_nodes),
            ("dispatch",  self._zt_dispatch),
            # ── Social media ──────────────────────────────────────────────
            ("post",      self._zt_post),            # /post <platform> <type> <topic>
            ("posts",     self._zt_posts),           # /posts — recent publications list
            ("ig_auth",   self._zt_ig_auth),         # /ig-auth <app_secret>
            # ── Google Drive / Sheets ─────────────────────────────────────
            ("drive",     self._zt_drive),           # /drive [setup|status|auth]
            # ── Video generation ──────────────────────────────────────────
            ("video",     self._zt_video),           # /video [cinematic|slides] <prompt>
            # ── Power user ────────────────────────────────────────────────
            ("verbose",   self.agentic_verbose),     # output verbosity 0|1|2
            ("speak",     self._zt_speak),           # TTS voice output
            ("voz",       self._voz_command),        # /voz [on|off] — voice toggle per user
            # ── Diagnostics ───────────────────────────────────────────────
            ("diagnose",  self._zt_diagnose),        # full self-healer diagnostic
            # ── Agent Squad ───────────────────────────────────────────────
            ("team",      self._zt_team),            # /team [task] — multi-agent squad
            # ── 3-Layer Conductor ─────────────────────────────────────────
            ("c",         self._zt_conductor),       # /c <task> — conductor shortcut
            ("conductor", self._zt_conductor),       # /conductor <task>
            # ── Emergency ─────────────────────────────────────────────────
            ("stop",      self.agentic_stop),        # kill all Claude subprocesses
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
            ("start",    command.start_command),
            ("help",     command.help_command),
            ("new",      command.new_session),
            ("continue", command.continue_session),
            ("end",      command.end_session),
            ("ls",       command.list_files),
            ("cd",       command.change_directory),
            ("pwd",      command.print_working_directory),
            ("projects", command.show_projects),
            ("status",   command.session_status),
            ("export",   command.export_session),
            ("actions",  command.quick_actions),
            ("git",      command.git_command),
            ("restart",  command.restart_command),
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
                BotCommand("start",     "Iniciar AURA"),
                BotCommand("new",       "Nueva sesión"),
                BotCommand("help",      "Comandos disponibles"),
                BotCommand("status",    "Estado completo"),
                BotCommand("health",    "Salud del sistema"),
                BotCommand("diagnose",  "Diagnóstico automático"),
                # ── Brains & Memoria ──────────────────────────────────────
                BotCommand("brain",     "Ver / cambiar brain activo"),
                BotCommand("limits",    "Uso y rate limits"),
                BotCommand("memory",    "Memoria Mem0 — hechos aprendidos"),
                # ── Shell & Dev ────────────────────────────────────────────
                BotCommand("sh",        "Ejecutar comando shell"),
                BotCommand("git",       "Git status / log / diff"),
                BotCommand("repo",      "Cambiar proyecto/workspace"),
                # ── Web ───────────────────────────────────────────────────
                BotCommand("web",       "Analizar URL con Gemini"),
                BotCommand("search",    "Búsqueda web"),
                # ── Comunicación ──────────────────────────────────────────
                BotCommand("email",     "Enviar email — /email to | asunto | cuerpo"),
                BotCommand("post",      "Social media — /post instagram carousel 5 sobre X"),
                BotCommand("standup",   "Daily standup — git + pendientes"),
                BotCommand("report",    "Reporte semanal"),
                # ── Herramientas ──────────────────────────────────────────
                BotCommand("terminal",  "Abrir Termora (terminal web)"),
                BotCommand("dashboard", "Dashboard en localhost:8080"),
                BotCommand("restart",   "Reiniciar bot"),
                BotCommand("stop",     "🛑 Matar tareas colgadas"),
                BotCommand("team",      "Squad multi-agente — /team o /team <tarea>"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start",    "Start bot and show help"),
                BotCommand("help",     "Show available commands"),
                BotCommand("new",      "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end",      "End current session and clear context"),
                BotCommand("ls",       "List files in current directory"),
                BotCommand("cd",       "Change directory (resumes project session)"),
                BotCommand("pwd",      "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status",   "Show session status"),
                BotCommand("export",   "Export current session"),
                BotCommand("actions",  "Show quick actions"),
                BotCommand("git",      "Git repository commands"),
                BotCommand("restart",  "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands


def read_file(file_path: str) -> Optional[str]:
    """Read file contents with comprehensive error handling.

    Args:
        file_path: Path to the file to read

    Returns:
        File contents as string, or None if read fails
    """
    try:
        with open(file_path, 'r') as file:
            return file.read()
    except FileNotFoundError as e:
        log_error(f"File not found: {e}")
    except IOError as e:
        log_error(f"IOError: {e}")
    except Exception as e:
        log_error(f"Unexpected error: {e}")
    return None


def write_file(file_path: str, content: str) -> None:
    """Write content to file with comprehensive error handling.

    Args:
        file_path: Path to the file to write
        content: Content to write to the file
    """
    try:
        with open(file_path, 'w') as file:
            file.write(content)
    except FileNotFoundError as e:
        log_error(f"File not found: {e}")
    except IOError as e:
        log_error(f"IOError: {e}")
    except Exception as e:
        log_error(f"Unexpected error: {e}")


def log_error(message: str) -> None:
    """Log error message to error log file.

    Args:
        message: Error message to log
    """
    log_path = os.path.expanduser('~/.aura/memory/error.log')
    try:
        with open(log_path, 'a') as log_file:
            log_file.write(f"{message}\n")
    except Exception as e:
        logger.error("failed_to_write_error_log", error=str(e))

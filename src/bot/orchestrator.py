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
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("restart", command.restart_command),
            # ⚡ Zero-token commands (no Claude, direct execution)
            ("ls", self._zt_ls),
            ("pwd", self._zt_pwd),
            ("git", self._zt_git),
            ("health", self._zt_health),
            ("terminal", self._zt_terminal),
            ("context", self._zt_context),
            ("sh", self._zt_sh),
            ("brain", self._zt_brain),
            ("brains", self._zt_brains),
            ("email", self._zt_email),
            ("inbox", self._zt_inbox),
            ("calendar", self._zt_calendar),
            ("limits", self._zt_limits),
            ("costs", self._zt_costs),
            # 🖥️ Dashboard (Phase 9)
            ("dashboard", self._zt_dashboard),
            # 🎤 Voice (Phase 8)
            ("speak", self._zt_speak),
            # 📋 Workflow commands (Phase 6)
            ("standup", self._zt_standup),
            ("report", self._zt_report),
            ("triage", self._zt_triage),
            ("followup", self._zt_followup),
            # 🖥️ Fleet & SuperNodes (Phase 10)
            ("machines", self._zt_machines),
            ("ssh", self._zt_ssh),
            ("fleet", self._zt_fleet),
            ("nodes", self._zt_nodes),
            ("dispatch", self._zt_dispatch),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

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
                # Core
                BotCommand("start", "Iniciar AURA"),
                BotCommand("status", "Estado actual"),
                BotCommand("health", "Servicios y sistema"),
                BotCommand("brain", "Estado del cerebro y CLIs"),
                BotCommand("brains", "Ver cerebros disponibles"),
                # Filesystem
                BotCommand("ls", "Listar archivos"),
                BotCommand("pwd", "Directorio actual"),
                BotCommand("sh", "Ejecutar comando shell"),
                BotCommand("git", "Estado de git"),
                BotCommand("repo", "Cambiar workspace"),
                # Tools
                BotCommand("terminal", "Terminal web (clsh)"),
                BotCommand("dashboard", "Dashboard URL"),
                BotCommand("speak", "Texto a voz"),
                # Workflows
                BotCommand("standup", "Standup diario"),
                BotCommand("report", "Reporte semanal"),
                BotCommand("limits", "Uso y límites"),
                BotCommand("restart", "Reiniciar bot"),
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
    ) -> None:
        """Handle messages via non-Claude brain (Codex/Gemini)."""
        from src.observability import get_tracer

        brain = router.get_brain(brain_name) if brain_name else router.get_active_brain(user_id)
        if brain is None:
            brain = router.get_active_brain(user_id)

        tracer = get_tracer()
        trace_ctx = tracer.trace_brain(
            brain_name=brain.name, user_id=user_id,
            message=message_text, metadata={"handler": "alt_brain"},
        )

        # Show thinking indicator
        progress_msg = await update.message.reply_text(
            f"{brain.emoji} {brain.display_name}..."
        )

        current_dir = str(
            context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
        )

        rate_monitor = context.bot_data.get("rate_monitor")

        try:
            kwargs = {
                "prompt": message_text,
                "working_directory": current_dir,
                "timeout_seconds": self.settings.claude_timeout_seconds,
                "session_key": str(user_id),  # per-user conversation continuity
            }

            response = await brain.execute(**kwargs)

            # Track usage
            if rate_monitor:
                if response.is_error and "rate" in (response.error_type or "").lower():
                    rate_monitor.record_error(brain.name, is_rate_limit=True)
                else:
                    rate_monitor.record_request(brain.name)

            content = response.content

            # Escalate if brain errored (haiku → sonnet → opus)
            if response.is_error and router:
                fallback_name = router.get_fallback_brain(brain.name)
                if fallback_name:
                    fallback = router.get_brain(fallback_name)
                    if fallback:
                        logger.info(
                            "brain_escalation",
                            from_brain=brain.name,
                            to_brain=fallback_name,
                            reason=response.error_type,
                        )
                        await progress_msg.edit_text(
                            f"{fallback.emoji} {fallback.display_name} (escalado)..."
                        )
                        response = await fallback.execute(
                            prompt=message_text,
                            working_directory=current_dir,
                            timeout_seconds=self.settings.claude_timeout_seconds,
                        )
                        content = response.content
                        brain = fallback

            content = response.content

            if len(content) > 3900:
                content = content[:3900] + "\n… (truncado)"

            duration = f"{response.duration_ms / 1000:.1f}s" if response.duration_ms else ""
            header = f"{brain.emoji} <b>{brain.display_name}</b>"
            if duration:
                header += f" · {duration}"

            if response.is_error:
                await progress_msg.edit_text(
                    f"{header}\n\n{self._escape_html(content)}",
                    parse_mode="HTML",
                )
            else:
                await progress_msg.edit_text(
                    f"{header}\n\n{self._escape_html(content)}",
                    parse_mode="HTML",
                )

            tracer.end_trace(ctx=trace_ctx, output=content[:500],
                             cost=response.cost, duration_ms=response.duration_ms)

            # Warn if approaching limits
            if rate_monitor:
                warning = rate_monitor.should_warn(brain.name)
                if warning:
                    await update.message.reply_text(warning)

        except Exception as e:
            if rate_monitor:
                rate_monitor.record_error(brain.name)
            logger.error("alt_brain_error", brain=brain.name, error=str(e))
            tracer.end_trace(ctx=trace_ctx, error=str(e))
            await progress_msg.edit_text(
                f"❌ {brain.display_name} error: {self._escape_html(str(e))}",
                parse_mode="HTML",
            )

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special chars for Telegram."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

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

        # --- Smart routing: classify intent and pick optimal brain ---
        from src.observability import get_tracer

        router = context.bot_data.get("brain_router")
        intent_info = ""
        if router:
            routed_brain, intent = router.smart_route(message_text, user_id)
            intent_info = f"{intent.intent.value}:{intent.suggested_brain}({intent.confidence})"
            logger.info("smart_route_decision", routed=routed_brain, intent=intent_info)

            # Route to appropriate brain (haiku/sonnet/opus/gemini)
            if routed_brain != "zero-token":
                await self._handle_alt_brain(
                    update, context, router, message_text, user_id,
                    brain_name=routed_brain,
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

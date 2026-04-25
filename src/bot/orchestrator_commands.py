"""Agentic command handlers for MessageOrchestrator.

Contains handlers registered as Telegram commands in agentic mode:
  agentic_start, agentic_new, agentic_status, agentic_verbose,
  _zt_team, _zt_conductor, _voz_command, agentic_repo, _agentic_callback
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, List

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .utils.html_format import escape_html
from ..projects import PrivateTopicsUnavailableError

if TYPE_CHECKING:
    from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class AgenticCommandsMixin:
    """Mixin providing agentic command handlers.

    Must be mixed into MessageOrchestrator which supplies:
      self.settings, self._cortex, self._escape_html()
    """

    async def agentic_start(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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

        safe_name = escape_html(user.first_name)
        # Clear conversation history on /start
        context.user_data["ollama_history"] = []
        await update.message.reply_text(
            f"Hola {safe_name} 👋 AURA lista."
            f"{sync_line}",
        )

    async def agentic_new(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True
        context.user_data["mission_state"] = {}

        # Clear per-brain conversation sessions so next message starts fresh
        router = context.bot_data.get("brain_router")
        if router:
            user_key = str(update.effective_user.id)
            for brain in router._brains.values():
                if hasattr(brain, "clear_session"):
                    brain.clear_session(user_key)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Delegate to _zt_status_full (which uses rate_monitor.format_status)."""
        await self._zt_status_full(update, context)

    def _get_verbose_level(
        self: "MessageOrchestrator",
        context: ContextTypes.DEFAULT_TYPE,
    ) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """/voz [on|off] — toggle voice responses for this user."""
        from .features.voice_tts import handle_voz_command
        await handle_voz_command(update, context)

    async def agentic_repo(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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

    async def agentic_stop(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """🛑 /stop — Kill all running Claude CLI subprocesses immediately."""
        import os
        import signal
        import subprocess

        try:
            result = subprocess.run(
                ["pgrep", "-f", "claude"],
                capture_output=True,
                text=True,
            )
            pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip()]
        except Exception as exc:
            await update.message.reply_text(f"❌ Error buscando procesos: {exc}")
            return

        # Exclude the current bot process and this process
        current_pid = os.getpid()
        pids_to_kill = [p for p in pids if p != current_pid]

        if not pids_to_kill:
            await update.message.reply_text("✅ No hay procesos Claude corriendo.")
            return

        killed: list[int] = []
        failed: list[tuple[int, str]] = []
        for pid in pids_to_kill:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass  # already gone
            except PermissionError as exc:
                failed.append((pid, str(exc)))

        lines = [f"🛑 <b>Stop ejecutado</b>"]
        if killed:
            lines.append(f"Matados: {', '.join(str(p) for p in killed)}")
        if failed:
            lines.append(f"Sin permisos: {', '.join(str(p) for p, _ in failed)}")
        lines.append("AURA libre — manda tu mensaje.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

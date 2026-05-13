"""Command handlers for bot operations."""

import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...projects import PrivateTopicsUnavailableError, load_project_registry
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...storage.models import SessionModel
from ..utils.html_format import escape_html
from ._handler_utils import (
    _escape_markdown,
    _format_file_size,
    _get_thread_project_root,
    _is_private_chat,
    _is_within_root,
)

# Re-export navigation commands so external callers still resolve from this module
from .cmd_navigation import change_directory, list_files, print_working_directory

# Re-export project commands so external callers still resolve from this module
from .cmd_project import export_session, session_status, show_projects

# Re-export admin commands so external callers still resolve from this module
from .cmd_admin import end_session, git_command, quick_actions, restart_command

logger = structlog.get_logger()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    manager = context.bot_data.get("project_threads_manager")
    sync_section = ""

    if settings.enable_project_threads and settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await update.message.reply_text(
                "🚫 <b>Private Topics Mode</b>\n\n"
                "Use this bot in a private chat and run <code>/start</code> there.",
                parse_mode="HTML",
            )
            return

    if (
        settings.enable_project_threads
        and settings.project_threads_mode == "private"
        and _is_private_chat(update)
    ):
        if manager is None:
            await update.message.reply_text(
                "❌ <b>Project thread mode is misconfigured</b>\n\n"
                "Thread manager is not initialized.",
                parse_mode="HTML",
            )
            return

        try:
            sync_result = await manager.sync_topics(
                context.bot,
                chat_id=update.effective_chat.id,
            )
            sync_section = (
                "\n\n🧵 <b>Project Topics Synced</b>\n"
                f"• Created: <b>{sync_result.created}</b>\n"
                f"• Reused: <b>{sync_result.reused}</b>\n"
                f"• Renamed: <b>{sync_result.renamed}</b>\n"
                f"• Failed: <b>{sync_result.failed}</b>\n\n"
                "Use a project topic thread to start coding."
            )
        except PrivateTopicsUnavailableError:
            await update.message.reply_text(
                manager.private_topics_unavailable_message(),
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user.id,
                    command="start",
                    args=[],
                    success=False,
                )
            return
        except Exception as e:
            sync_section = (
                "\n\n⚠️ <b>Topic Sync Warning</b>\n"
                f"{escape_html(str(e))}\n\n"
                "Run <code>/sync_threads</code> to retry."
            )

    welcome_message = (
        f"👋 Welcome to Claude Code Telegram Bot, {escape_html(user.first_name)}!\n\n"
        f"🤖 I help you access Claude Code remotely through Telegram.\n\n"
        f"<b>Available Commands:</b>\n"
        f"• <code>/help</code> - Show detailed help\n"
        f"• <code>/new</code> - Start a new Claude session\n"
        f"• <code>/ls</code> - List files in current directory\n"
        f"• <code>/cd &lt;dir&gt;</code> - Change directory\n"
        f"• <code>/projects</code> - Show available projects\n"
        f"• <code>/status</code> - Show session status\n"
        f"• <code>/actions</code> - Show quick actions\n"
        f"• <code>/git</code> - Git repository commands\n\n"
        f"<b>Quick Start:</b>\n"
        f"1. Use <code>/projects</code> to see available projects\n"
        f"2. Use <code>/cd &lt;project&gt;</code> to navigate to a project\n"
        f"3. Send any message to start coding with Claude!\n\n"
        f"🔒 Your access is secured and all actions are logged.\n"
        f"📊 Use <code>/status</code> to check your usage limits."
        f"{sync_section}"
    )

    # Add quick action buttons
    keyboard = [
        [
            InlineKeyboardButton(
                "📁 Show Projects", callback_data="action:show_projects"
            ),
            InlineKeyboardButton("❓ Get Help", callback_data="action:help"),
        ],
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton("📊 Check Status", callback_data="action:status"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        welcome_message, parse_mode="HTML", reply_markup=reply_markup
    )

    # Log command
    if audit_logger:
        await audit_logger.log_command(
            user_id=user.id, command="start", args=[], success=True
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "🤖 <b>Claude Code Telegram Bot Help</b>\n\n"
        "<b>Navigation Commands:</b>\n"
        "• <code>/ls</code> - List files and directories\n"
        "• <code>/cd &lt;directory&gt;</code> - Change to directory\n"
        "• <code>/pwd</code> - Show current directory\n"
        "• <code>/projects</code> - Show available projects\n\n"
        "<b>Session Commands:</b>\n"
        "• <code>/new</code> - Clear context and start a fresh session\n"
        "• <code>/continue [message]</code> - Explicitly continue last session\n"
        "• <code>/end</code> - End current session and clear context\n"
        "• <code>/status</code> - Show session and usage status\n"
        "• <code>/export</code> - Export session history\n"
        "• <code>/actions</code> - Show context-aware quick actions\n"
        "• <code>/git</code> - Git repository information\n\n"
        "<b>Session Behavior:</b>\n"
        "• Sessions are automatically maintained per project directory\n"
        "• Switching directories with <code>/cd</code> resumes the session for that project\n"
        "• Use <code>/new</code> or <code>/end</code> to explicitly clear session context\n"
        "• Sessions persist across bot restarts\n\n"
        "<b>Usage Examples:</b>\n"
        "• <code>cd myproject</code> - Enter project directory\n"
        "• <code>ls</code> - See what's in current directory\n"
        "• <code>Create a simple Python script</code> - Ask Claude to code\n"
        "• Send a file to have Claude review it\n\n"
        "<b>File Operations:</b>\n"
        "• Send text files (.py, .js, .md, etc.) for review\n"
        "• Claude can read, modify, and create files\n"
        "• All file operations are within your approved directory\n\n"
        "<b>Security Features:</b>\n"
        "• 🔒 Path traversal protection\n"
        "• ⏱️ Rate limiting to prevent abuse\n"
        "• 📊 Usage tracking and limits\n"
        "• 🛡️ Input validation and sanitization\n\n"
        "<b>Tips:</b>\n"
        "• Use specific, clear requests for best results\n"
        "• Check <code>/status</code> to monitor your usage\n"
        "• Use quick action buttons when available\n"
        "• File uploads are automatically processed by Claude\n\n"
        "Need more help? Contact your administrator."
    )

    await update.message.reply_text(help_text, parse_mode="HTML")


async def sync_threads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Synchronize project topics in the configured forum chat."""
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    if not settings.enable_project_threads:
        await update.message.reply_text(
            "ℹ️ <b>Project thread mode is disabled.</b>", parse_mode="HTML"
        )
        return

    manager = context.bot_data.get("project_threads_manager")
    if not manager:
        await update.message.reply_text(
            "❌ <b>Project thread manager not initialized.</b>", parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text(
        "🔄 <b>Syncing project topics...</b>", parse_mode="HTML"
    )

    if settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await status_msg.edit_text(
                "❌ <b>Private Thread Mode</b>\n\n"
                "Run <code>/sync_threads</code> in your private chat with the bot.",
                parse_mode="HTML",
            )
            return
        target_chat_id = update.effective_chat.id
    else:
        if settings.project_threads_chat_id is None:
            await status_msg.edit_text(
                "❌ <b>Group Thread Mode Misconfigured</b>\n\n"
                "Set <code>PROJECT_THREADS_CHAT_ID</code> first.",
                parse_mode="HTML",
            )
            return
        if (
            not update.effective_chat
            or update.effective_chat.id != settings.project_threads_chat_id
        ):
            await status_msg.edit_text(
                "❌ <b>Group Thread Mode</b>\n\n"
                "Run <code>/sync_threads</code> in the configured project threads group.",
                parse_mode="HTML",
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        if not settings.projects_config_path:
            await status_msg.edit_text(
                "❌ <b>Project thread mode is misconfigured</b>\n\n"
                "Set <code>PROJECTS_CONFIG_PATH</code> to a valid YAML file.",
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_threads", [], False)
            return

        registry = load_project_registry(
            config_path=settings.projects_config_path,
            approved_directory=settings.approved_directory,
        )
        manager.registry = registry
        context.bot_data["project_registry"] = registry

        result = await manager.sync_topics(context.bot, chat_id=target_chat_id)
        await status_msg.edit_text(
            "✅ <b>Project topic sync complete</b>\n\n"
            f"• Created: <b>{result.created}</b>\n"
            f"• Reused: <b>{result.reused}</b>\n"
            f"• Renamed: <b>{result.renamed}</b>\n"
            f"• Reopened: <b>{result.reopened}</b>\n"
            f"• Closed: <b>{result.closed}</b>\n"
            f"• Deactivated: <b>{result.deactivated}</b>\n"
            f"• Failed: <b>{result.failed}</b>",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], True)
    except PrivateTopicsUnavailableError:
        await status_msg.edit_text(
            manager.private_topics_unavailable_message(),
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Project topic sync failed</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - explicitly starts a fresh session, clearing previous context."""
    settings: Settings = context.bot_data["settings"]

    # Get current directory (default to approved directory)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Track what was cleared for user feedback
    old_session_id = context.user_data.get("claude_session_id")

    # Clear existing session data - this is the explicit way to reset context
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True
    context.user_data["force_new_session"] = True

    cleared_info = ""
    if old_session_id:
        cleared_info = (
            f"\n🗑️ Previous session <code>{old_session_id[:8]}...</code> cleared."
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Start Coding", callback_data="action:start_coding"
            ),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Quick Actions", callback_data="action:quick_actions"
            ),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🆕 <b>New Claude Code Session</b>\n\n"
        f"📂 Working directory: <code>{relative_path}/</code>{cleared_info}\n\n"
        f"Context has been cleared. Send a message to start fresh, "
        f"or use the buttons below:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /continue command with optional prompt."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse optional prompt from command arguments
    # If no prompt provided, use a default to continue the conversation
    prompt = " ".join(context.args) if context.args else None
    default_prompt = "Please continue where we left off"

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await update.message.reply_text(
                "❌ <b>Claude Integration Not Available</b>\n\n"
                "Claude integration is not properly configured."
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # We have a session in context, continue it directly
            status_msg = await update.message.reply_text(
                f"🔄 <b>Continuing Session</b>\n\n"
                f"Session ID: <code>{claude_session_id[:8]}...</code>\n"
                f"Directory: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"{'Processing your message...' if prompt else 'Continuing where you left off...'}",
                parse_mode="HTML",
            )

            # Continue with the existing session
            # Use default prompt if none provided (Claude CLI requires a prompt)
            claude_response = await claude_integration.run_command(
                prompt=prompt or default_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            status_msg = await update.message.reply_text(
                "🔍 <b>Looking for Recent Session</b>\n\n"
                "Searching for your most recent session in this directory...",
                parse_mode="HTML",
            )

            # Use default prompt if none provided
            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=prompt or default_prompt,
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Delete status message and send response
            await status_msg.delete()

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            for msg in formatted_messages:
                await update.message.reply_text(
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=msg.reply_markup,
                )

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=context.args or [],
                    success=True,
                )

        else:
            # No session found to continue
            await status_msg.edit_text(
                "❌ <b>No Session Found</b>\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"<b>What you can do:</b>\n"
                f"• Use <code>/new</code> to start a fresh session\n"
                f"• Use <code>/status</code> to check your sessions\n"
                f"• Navigate to a different directory with <code>/cd</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 New Session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 Status", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await status_msg.delete()
        except Exception:
            pass

        # Send error response
        await update.message.reply_text(
            f"❌ <b>Error Continuing Session</b>\n\n"
            f"An error occurred while trying to continue your session:\n\n"
            f"<code>{error_msg}</code>\n\n"
            f"<b>Suggestions:</b>\n"
            f"• Try starting a new session with <code>/new</code>\n"
            f"• Check your session status with <code>/status</code>\n"
            f"• Contact support if the issue persists",
            parse_mode="HTML",
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=context.args or [],
                success=False,
            )

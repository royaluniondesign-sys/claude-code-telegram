"""Admin command handlers: /end, /actions, /git, /restart."""

import os
import signal
from datetime import datetime, timezone

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...storage.models import SessionModel
from ..utils.html_format import escape_html

logger = structlog.get_logger()


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /end command to terminate the current session."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await update.message.reply_text(
            "ℹ️ <b>No Active Session</b>\n\n"
            "There's no active Claude session to end.\n\n"
            "<b>What you can do:</b>\n"
            "• Use <code>/new</code> to start a new session\n"
            "• Use <code>/status</code> to check your session status\n"
            "• Send any message to start a conversation"
        )
        return

    # Get current directory for display
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Clear session data
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = False
    context.user_data["last_message"] = None

    # Create quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="action:status"),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✅ <b>Session Ended</b>\n\n"
        f"Your Claude session has been terminated.\n\n"
        f"<b>Current Status:</b>\n"
        f"• Directory: <code>{relative_path}/</code>\n"
        f"• Session: None\n"
        f"• Ready for new commands\n\n"
        f"<b>Next Steps:</b>\n"
        f"• Start a new session with <code>/new</code>\n"
        f"• Check status with <code>/status</code>\n"
        f"• Send any message to begin a new conversation",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

    logger.info("Session ended by user", user_id=user_id, session_id=claude_session_id)


async def quick_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /actions command to show quick actions."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("quick_actions"):
        await update.message.reply_text(
            "❌ <b>Quick Actions Disabled</b>\n\n"
            "Quick actions feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        quick_action_manager = features.get_quick_actions()
        if not quick_action_manager:
            await update.message.reply_text(
                "❌ <b>Quick Actions Unavailable</b>\n\n"
                "Quick actions service is not available."
            )
            return

        # Get context-aware actions
        now = datetime.now(timezone.utc)
        actions = await quick_action_manager.get_suggestions(
            session=SessionModel(
                session_id="",  # ephemeral session for quick actions context
                user_id=user_id,
                project_path=str(current_dir),
                created_at=now,
                last_used=now,
            )
        )

        if not actions:
            await update.message.reply_text(
                "🤖 <b>No Actions Available</b>\n\n"
                "No quick actions are available for the current context.\n\n"
                "<b>Try:</b>\n"
                "• Navigating to a project directory with <code>/cd</code>\n"
                "• Creating some code files\n"
                "• Starting a Claude session with <code>/new</code>"
            )
            return

        # Create inline keyboard
        keyboard = quick_action_manager.create_inline_keyboard(actions, max_columns=2)

        relative_path = current_dir.relative_to(settings.approved_directory)
        await update.message.reply_text(
            f"⚡ <b>Quick Actions</b>\n\n"
            f"📂 Context: <code>{relative_path}/</code>\n\n"
            f"Select an action to execute:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Error Loading Actions</b>\n\n{str(e)}")
        logger.error("Error in quick_actions command", error=str(e), user_id=user_id)


async def git_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /git command to show git repository information."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await update.message.reply_text(
            "❌ <b>Git Integration Disabled</b>\n\n"
            "Git integration feature is not enabled.\n"
            "Contact your administrator to enable this feature."
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await update.message.reply_text(
                "❌ <b>Git Integration Unavailable</b>\n\n"
                "Git integration service is not available."
            )
            return

        # Check if current directory is a git repository
        if not (current_dir / ".git").exists():
            await update.message.reply_text(
                f"📂 <b>Not a Git Repository</b>\n\n"
                f"Current directory <code>{current_dir.relative_to(settings.approved_directory)}/</code> is not a git repository.\n\n"
                f"<b>Options:</b>\n"
                f"• Navigate to a git repository with <code>/cd</code>\n"
                f"• Initialize a new repository (ask Claude to help)\n"
                f"• Clone an existing repository (ask Claude to help)"
            )
            return

        # Get git status
        git_status = await git_integration.get_status(current_dir)

        # Format status message
        relative_path = current_dir.relative_to(settings.approved_directory)
        status_message = "🔗 <b>Git Repository Status</b>\n\n"
        status_message += f"📂 Directory: <code>{relative_path}/</code>\n"
        status_message += f"🌿 Branch: <code>{git_status.branch}</code>\n"

        if git_status.ahead > 0:
            status_message += f"⬆️ Ahead: {git_status.ahead} commits\n"
        if git_status.behind > 0:
            status_message += f"⬇️ Behind: {git_status.behind} commits\n"

        # Show file changes
        if not git_status.is_clean:
            status_message += "\n<b>Changes:</b>\n"
            if git_status.modified:
                status_message += f"📝 Modified: {len(git_status.modified)} files\n"
            if git_status.added:
                status_message += f"➕ Added: {len(git_status.added)} files\n"
            if git_status.deleted:
                status_message += f"➖ Deleted: {len(git_status.deleted)} files\n"
            if git_status.untracked:
                status_message += f"❓ Untracked: {len(git_status.untracked)} files\n"
        else:
            status_message += "\n✅ Working directory clean\n"

        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="git:status"),
                InlineKeyboardButton("📁 Files", callback_data="action:ls"),
            ],
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            status_message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Git Error</b>\n\n{str(e)}")
        logger.error("Error in git_command", error=str(e), user_id=user_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sends a confirmation message then triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>Restarting bot…</b>\n\nBack shortly.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    logger.info("Restart requested via /restart command", user_id=user_id)

    # SIGTERM triggers the existing graceful-shutdown handler in main.py;
    # systemd Restart=always will bring the process back up.
    os.kill(os.getpid(), signal.SIGTERM)

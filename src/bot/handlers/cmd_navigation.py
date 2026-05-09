"""Navigation command handlers: /ls, /cd, /pwd."""

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ..utils.html_format import escape_html
from ._handler_utils import (
    _escape_markdown,
    _format_file_size,
    _get_thread_project_root,
    _is_within_root,
)

logger = structlog.get_logger()


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ls command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            # Skip hidden files (starting with .)
            if item.name.startswith("."):
                continue

            # Escape HTML special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                # Get file size
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        # Combine directories first, then files
        items = directories + files

        # Format response
        relative_path = current_dir.relative_to(settings.approved_directory)
        if not items:
            message = f"📂 <code>{relative_path}/</code>\n\n<i>(empty directory)</i>"
        else:
            message = f"📂 <code>{relative_path}/</code>\n\n"

            # Limit items shown to prevent message being too long
            max_items = 50
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>... and {len(items) - max_items} more items</i>"
            else:
                message += "\n".join(items)

        # Add navigation buttons if not at root
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Go to Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📁 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await update.message.reply_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], True)

    except Exception as e:
        error_msg = f"❌ Error listing directory: {str(e)}"
        await update.message.reply_text(error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], False)

        logger.error("Error in list_files command", error=str(e), user_id=user_id)


async def change_directory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse arguments
    if not context.args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/cd &lt;directory&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/cd myproject</code> - Enter subdirectory\n"
            "• <code>/cd ..</code> - Go up one level\n"
            "• <code>/cd /</code> - Go to root of approved directory\n\n"
            "<b>Tips:</b>\n"
            "• Use <code>/ls</code> to see available directories\n"
            "• Use <code>/projects</code> to see all projects",
            parse_mode="HTML",
        )
        return

    target_path = " ".join(context.args)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    project_root = _get_thread_project_root(settings, context)
    directory_root = project_root or settings.approved_directory

    try:
        # Handle known navigation shortcuts first
        if target_path == "/":
            resolved_path = directory_root
        elif target_path == "..":
            resolved_path = current_dir.parent
            if not _is_within_root(resolved_path, directory_root):
                resolved_path = directory_root
        else:
            # Validate path using security validator
            if security_validator:
                valid, resolved_path, error = security_validator.validate_path(
                    target_path, current_dir
                )

                if not valid:
                    await update.message.reply_text(
                        f"❌ <b>Access Denied</b>\n\n{error}"
                    )

                    # Log security violation
                    if audit_logger:
                        await audit_logger.log_security_violation(
                            user_id=user_id,
                            violation_type="path_traversal_attempt",
                            details=f"Attempted path: {target_path}",
                            severity="medium",
                        )
                    return
            else:
                resolved_path = current_dir / target_path
                resolved_path = resolved_path.resolve()

        if project_root and not _is_within_root(resolved_path, project_root):
            await update.message.reply_text(
                "❌ <b>Access Denied</b>\n\n"
                "In thread mode, navigation is limited to the current project root.",
                parse_mode="HTML",
            )
            return

        # Check if directory exists and is actually a directory
        if not resolved_path.exists():
            await update.message.reply_text(
                f"❌ <b>Directory Not Found</b>\n\n<code>{target_path}</code> does not exist."
            )
            return

        if not resolved_path.is_dir():
            await update.message.reply_text(
                f"❌ <b>Not a Directory</b>\n\n<code>{target_path}</code> is not a directory."
            )
            return

        # Update current directory in user data
        context.user_data["current_directory"] = resolved_path

        # Look up existing session for the new directory instead of clearing
        claude_integration: ClaudeIntegration = context.bot_data.get(
            "claude_integration"
        )
        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, resolved_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 Resumed session <code>{existing_session.session_id[:8]}...</code> "
                    f"({existing_session.message_count} messages)"
                )
            else:
                # No session for this directory - clear the current one
                context.user_data["claude_session_id"] = None
                resumed_session_info = (
                    "\n🆕 No existing session. Send a message to start a new one."
                )

        # Send confirmation
        relative_base = project_root or settings.approved_directory
        relative_path = resolved_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"
        await update.message.reply_text(
            f"✅ <b>Directory Changed</b>\n\n"
            f"📂 Current directory: <code>{relative_display}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], True)

    except Exception as e:
        error_msg = f"❌ <b>Error changing directory</b>\n\n{str(e)}"
        await update.message.reply_text(error_msg, parse_mode="HTML")

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], False)

        logger.error("Error in change_directory command", error=str(e), user_id=user_id)


async def print_working_directory(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /pwd command."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    relative_path = current_dir.relative_to(settings.approved_directory)
    absolute_path = str(current_dir)

    # Add quick navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
            InlineKeyboardButton("📋 Projects", callback_data="action:show_projects"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📍 <b>Current Directory</b>\n\n"
        f"Relative: <code>{relative_path}/</code>\n"
        f"Absolute: <code>{absolute_path}</code>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

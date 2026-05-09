"""Handle inline keyboard callbacks."""

from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ..utils.html_format import escape_html
from ._handler_utils import _get_thread_project_root, _is_within_root

# Re-export action handlers so external callers can still import from this module
from .callback_actions import (
    _handle_continue_action,
    _handle_end_session_action,
    _handle_export_action,
    _handle_help_action,
    _handle_ls_action,
    _handle_new_session_action,
    _handle_quick_actions_action,
    _handle_refresh_ls_action,
    _handle_refresh_status_action,
    _handle_show_projects_action,
    _handle_start_coding_action,
    _handle_status_action,
)

# Re-export special handlers so external callers can still import from this module
from .callback_special import (
    _escape_markdown,
    _format_file_size,
    handle_conversation_callback,
    handle_export_callback,
    handle_followup_callback,
    handle_git_callback,
    handle_quick_action_callback,
)

logger = structlog.get_logger()


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback

    user_id = query.from_user.id
    data = query.data

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param, context)
        else:
            await query.edit_message_text(
                "❌ <b>Unknown Action</b>\n\n"
                "This button action is not recognized. "
                "The bot may have been updated since this message was sent.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await query.edit_message_text(
                "❌ <b>Error Processing Action</b>\n\n"
                "An error occurred while processing your request.\n"
                "Please try again or use text commands.",
                parse_mode="HTML",
            )
        except Exception:
            # If we can't edit the message, send a new one
            await query.message.reply_text(
                "❌ <b>Error Processing Action</b>\n\n"
                "An error occurred while processing your request.",
                parse_mode="HTML",
            )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    try:
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        project_root = _get_thread_project_root(settings, context)
        directory_root = project_root or settings.approved_directory

        # Handle special paths
        if project_name == "/":
            new_path = directory_root
        elif project_name == "..":
            new_path = current_dir.parent
            if not _is_within_root(new_path, directory_root):
                new_path = directory_root
        else:
            if project_root:
                new_path = current_dir / project_name
            else:
                new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await query.edit_message_text(
                    f"❌ <b>Access Denied</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )
                return
            # Use the validated path
            new_path = resolved_path

        if project_root and not _is_within_root(new_path, project_root):
            await query.edit_message_text(
                "❌ <b>Access Denied</b>\n\n"
                "In thread mode, navigation is limited to the current project root.",
                parse_mode="HTML",
            )
            return

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await query.edit_message_text(
                f"❌ <b>Directory Not Found</b>\n\n"
                f"The directory <code>{escape_html(project_name)}</code> no longer exists or is not accessible.",
                parse_mode="HTML",
            )
            return

        # Update directory and resume session for that directory when available
        context.user_data["current_directory"] = new_path

        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, new_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 Resumed session <code>{escape_html(existing_session.session_id[:8])}...</code> "
                    f"({existing_session.message_count} messages)"
                )
            else:
                context.user_data["claude_session_id"] = None
                resumed_session_info = (
                    "\n🆕 No existing session. Send a message to start a new one."
                )
        else:
            context.user_data["claude_session_id"] = None
            resumed_session_info = "\n🆕 Send a message to start a new session."

        # Send confirmation with new directory info
        relative_base = project_root or settings.approved_directory
        relative_path = new_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ <b>Directory Changed</b>\n\n"
            f"📂 Current directory: <code>{escape_html(str(relative_display))}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>Error changing directory</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    actions = {
        "help": _handle_help_action,
        "show_projects": _handle_show_projects_action,
        "new_session": _handle_new_session_action,
        "continue": _handle_continue_action,
        "end_session": _handle_end_session_action,
        "status": _handle_status_action,
        "ls": _handle_ls_action,
        "start_coding": _handle_start_coding_action,
        "quick_actions": _handle_quick_actions_action,
        "refresh_status": _handle_refresh_status_action,
        "refresh_ls": _handle_refresh_ls_action,
        "export": _handle_export_action,
    }

    handler = actions.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await query.edit_message_text(
            f"❌ <b>Unknown Action: {escape_html(action_type)}</b>\n\n"
            "This action is not implemented yet.",
            parse_mode="HTML",
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await query.edit_message_text(
            "✅ <b>Confirmed</b>\n\nAction will be processed.",
            parse_mode="HTML",
        )
    elif confirmation_type == "no":
        await query.edit_message_text(
            "❌ <b>Cancelled</b>\n\nAction was cancelled.",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            "❓ <b>Unknown confirmation response</b>",
            parse_mode="HTML",
        )

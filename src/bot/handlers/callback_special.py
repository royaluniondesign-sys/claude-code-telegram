"""Special handler functions for inline keyboard callbacks (quick actions, followup, conversation, git, export)."""

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ..utils.html_format import escape_html

logger = structlog.get_logger()


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"


def _escape_markdown(text: str) -> str:
    """Escape HTML-special characters in text for Telegram.

    Legacy name kept for compatibility with callers; actually escapes HTML.
    """
    return escape_html(text)


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id

    # Get quick actions manager from bot data if available
    quick_actions = context.bot_data.get("quick_actions")

    if not quick_actions:
        await query.edit_message_text(
            "❌ <b>Quick Actions Not Available</b>\n\n"
            "Quick actions feature is not available.",
            parse_mode="HTML",
        )
        return

    # Get Claude integration
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ <b>Claude Integration Not Available</b>\n\n"
            "Claude integration is not properly configured.",
            parse_mode="HTML",
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await query.edit_message_text(
                f"❌ <b>Action Not Found</b>\n\n"
                f"Quick action '{escape_html(action_id)}' is not available.",
                parse_mode="HTML",
            )
            return

        # Execute the action
        await query.edit_message_text(
            f"🚀 <b>Executing {action.icon} {escape_html(action.name)}</b>\n\n"
            f"Running quick action in directory: <code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
            f"Please wait...",
            parse_mode="HTML",
        )

        # Run the action through Claude
        claude_response = await claude_integration.run_command(
            prompt=action.prompt, working_directory=current_dir, user_id=user_id
        )

        if claude_response:
            # Format and send the response
            response_text = escape_html(claude_response.content)
            if len(response_text) > 4000:
                response_text = (
                    response_text[:4000] + "...\n\n<i>(Response truncated)</i>"
                )

            await query.message.reply_text(
                f"✅ <b>{action.icon} {escape_html(action.name)} Complete</b>\n\n{response_text}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ <b>Action Failed</b>\n\n"
                f"Failed to execute {escape_html(action.name)}. Please try again.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error("Quick action execution failed", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>Action Error</b>\n\n"
            f"An error occurred while executing {escape_html(action_id)}: {escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks."""
    user_id = query.from_user.id

    # Get conversation enhancer from bot data if available
    conversation_enhancer = context.bot_data.get("conversation_enhancer")

    if not conversation_enhancer:
        await query.edit_message_text(
            "❌ <b>Follow-up Not Available</b>\n\n"
            "Conversation enhancement features are not available.",
            parse_mode="HTML",
        )
        return

    try:
        # Get stored suggestions (this would need to be implemented in the enhancer)
        # For now, we'll provide a generic response
        await query.edit_message_text(
            "💡 <b>Follow-up Suggestion Selected</b>\n\n"
            "This follow-up suggestion will be implemented once the conversation "
            "enhancement system is fully integrated with the message handler.\n\n"
            "<b>Current Status:</b>\n"
            "• Suggestion received ✅\n"
            "• Integration pending 🔄\n\n"
            "<i>You can continue the conversation by sending a new message.</i>",
            parse_mode="HTML",
        )

        logger.info(
            "Follow-up suggestion selected",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.error(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await query.edit_message_text(
            "❌ <b>Error Processing Follow-up</b>\n\n"
            "An error occurred while processing your follow-up suggestion.",
            parse_mode="HTML",
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await query.edit_message_text(
            "✅ <b>Continuing Conversation</b>\n\n"
            "Send me your next message to continue coding!\n\n"
            "I'm ready to help with:\n"
            "• Code review and debugging\n"
            "• Feature implementation\n"
            "• Architecture decisions\n"
            "• Testing and optimization\n"
            "• Documentation\n\n"
            "<i>Just type your request or upload files.</i>",
            parse_mode="HTML",
        )

    elif action_type == "end":
        # End the current session
        conversation_enhancer = context.bot_data.get("conversation_enhancer")
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        # Clear session data
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = False

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        relative_path = current_dir.relative_to(settings.approved_directory)

        # Create quick action buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
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

        await query.edit_message_text(
            "✅ <b>Conversation Ended</b>\n\n"
            f"Your Claude session has been terminated.\n\n"
            f"<b>Current Status:</b>\n"
            f"• Directory: <code>{escape_html(str(relative_path))}/</code>\n"
            f"• Session: None\n"
            f"• Ready for new commands\n\n"
            f"<b>Next Steps:</b>\n"
            f"• Start a new session\n"
            f"• Check status\n"
            f"• Send any message to begin a new conversation",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await query.edit_message_text(
            f"❌ <b>Unknown Conversation Action: {escape_html(action_type)}</b>\n\n"
            "This conversation action is not recognized.",
            parse_mode="HTML",
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await query.edit_message_text(
            "❌ <b>Git Integration Disabled</b>\n\n"
            "Git integration feature is not enabled.",
            parse_mode="HTML",
        )
        return

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await query.edit_message_text(
                "❌ <b>Git Integration Unavailable</b>\n\n"
                "Git integration service is not available.",
                parse_mode="HTML",
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

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

            await query.edit_message_text(
                status_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 <b>Git Diff</b>\n\n<i>No changes to show.</i>"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Limit diff output (leave room for header + HTML tags within
                # Telegram's 4096-char message limit)
                max_length = 3500
                if len(clean_diff) > max_length:
                    clean_diff = (
                        clean_diff[:max_length] + "\n\n... output truncated ..."
                    )

                escaped_diff = escape_html(clean_diff)
                diff_message = (
                    f"📊 <b>Git Diff</b>\n\n<pre><code>{escaped_diff}</code></pre>"
                )

            keyboard = [
                [
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                diff_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 <b>Git Log</b>\n\n<i>No commits found.</i>"
            else:
                log_message = "📜 <b>Git Log</b>\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = escape_html(commit.message[:60])
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• <code>{short_hash}</code> {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                log_message, parse_mode="HTML", reply_markup=reply_markup
            )

        else:
            await query.edit_message_text(
                f"❌ <b>Unknown Git Action: {escape_html(git_action)}</b>\n\n"
                "This git action is not recognized.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await query.edit_message_text(
            f"❌ <b>Git Error</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await query.edit_message_text(
            "📤 <b>Export Cancelled</b>\n\n" "Session export has been cancelled.",
            parse_mode="HTML",
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await query.edit_message_text(
            "❌ <b>Export Unavailable</b>\n\n"
            "Session export service is not available.",
            parse_mode="HTML",
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ <b>No Active Session</b>\n\n" "There's no active session to export.",
            parse_mode="HTML",
        )
        return

    try:
        # Show processing message
        await query.edit_message_text(
            f"📤 <b>Exporting Session</b>\n\n"
            f"Generating {escape_html(export_format.upper())} export...",
            parse_mode="HTML",
        )

        # Export session
        exported_session = await session_exporter.export_session(
            claude_session_id, export_format
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        await query.message.reply_document(
            document=file_bytes,
            filename=exported_session.filename,
            caption=(
                f"📤 <b>Session Export Complete</b>\n\n"
                f"Format: {escape_html(exported_session.format.upper())}\n"
                f"Size: {exported_session.size_bytes:,} bytes\n"
                f"Created: {exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="HTML",
        )

        # Update the original message
        await query.edit_message_text(
            f"✅ <b>Export Complete</b>\n\n"
            f"Your session has been exported as {escape_html(exported_session.filename)}.\n"
            f"Check the file above for your complete conversation history.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await query.edit_message_text(
            f"❌ <b>Export Failed</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )

"""Message handlers for non-command inputs."""

import asyncio
from typing import Optional

import structlog
from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

from ...claude.exceptions import (
    ClaudeError,
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.rate_limiter import RateLimiter
from ...security.validators import SecurityValidator
from ..utils.html_format import escape_html
from ..utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

# Re-export formatter functions so external callers still resolve from this module
from .msg_formatters import (
    _format_error_message,
    _format_process_error,
    _format_progress_update,
)

# Re-export media handlers so external callers still resolve from this module
from .msg_media import handle_document, handle_photo, handle_voice

# Re-export utility functions so external callers still resolve from this module
from .msg_utils import (
    _estimate_file_processing_cost,
    _estimate_text_processing_cost,
    _generate_placeholder_response,
    _update_working_directory_from_claude_response,
)

logger = structlog.get_logger()


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages as Claude prompts."""
    user_id = update.effective_user.id
    message_text = update.message.text
    settings: Settings = context.bot_data["settings"]

    # Get services
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    logger.info(
        "Processing text message", user_id=user_id, message_length=len(message_text)
    )

    try:
        # Check rate limit with estimated cost for text processing
        estimated_cost = _estimate_text_processing_cost(message_text)

        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(
                user_id, estimated_cost
            )
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        # Send typing indicator
        await update.message.chat.send_action("typing")

        # Create progress message
        progress_msg = await update.message.reply_text(
            "🤔 Processing your request...",
            reply_to_message_id=update.message.message_id,
        )

        # Get Claude integration and storage from context
        claude_integration = context.bot_data.get("claude_integration")
        storage = context.bot_data.get("storage")

        if not claude_integration:
            await update.message.reply_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured. "
                "Please contact the administrator.",
                parse_mode="HTML",
            )
            return

        # Get current directory
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )

        # Get existing session ID
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # MCP image collection via stream intercept
        mcp_images: list[ImageAttachment] = []

        # Enhanced stream updates handler with progress tracking
        async def stream_handler(update_obj):
            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>".
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, settings.approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            try:
                progress_text = await _format_progress_update(update_obj)
                if progress_text:
                    await progress_msg.edit_text(progress_text, parse_mode="HTML")
            except Exception as e:
                logger.warning("Failed to update progress message", error=str(e))

        # Run Claude command
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=stream_handler,
                force_new=force_new,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            # Update session ID
            context.user_data["claude_session_id"] = claude_response.session_id

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            # Log interaction to storage
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,  # Telegram doesn't provide IP
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction to storage", error=str(e))

            # Format response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except Exception as e:
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from ..utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]

        # Delete progress message
        await progress_msg.delete()

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: list[ImageAttachment] = mcp_images

        # Try to combine text + images when response fits in a caption
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                photos = [i for i in images if should_send_as_photo(i.path)]
                documents = [i for i in images if not should_send_as_photo(i.path)]
                if photos and not documents:
                    try:
                        if len(photos) == 1:
                            with open(photos[0].path, "rb") as f:
                                await update.message.reply_photo(
                                    photo=f,
                                    caption=msg.text,
                                    parse_mode=msg.parse_mode,
                                    reply_to_message_id=update.message.message_id,
                                )
                            caption_sent = True
                        else:
                            media = []
                            file_handles = []
                            for idx, img in enumerate(photos[:10]):
                                fh = open(img.path, "rb")  # noqa: SIM115
                                file_handles.append(fh)
                                media.append(
                                    InputMediaPhoto(
                                        media=fh,
                                        caption=msg.text if idx == 0 else None,
                                        parse_mode=(
                                            msg.parse_mode if idx == 0 else None
                                        ),
                                    )
                                )
                            try:
                                await update.message.chat.send_media_group(
                                    media=media,
                                    reply_to_message_id=update.message.message_id,
                                )
                                caption_sent = True
                            finally:
                                for fh in file_handles:
                                    fh.close()
                    except Exception as album_err:
                        logger.warning(
                            "Failed to send photo+caption", error=str(album_err)
                        )

        if not caption_sent:
            # Send formatted responses (may be multiple messages)
            for i, message in enumerate(formatted_messages):
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=message.reply_markup,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=message.reply_markup,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        logger.error(
                            "Failed to send plain text fallback response",
                            error=str(plain_err),
                        )
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately
            if images:
                photos = [i for i in images if should_send_as_photo(i.path)]
                documents = [i for i in images if not should_send_as_photo(i.path)]
                if photos:
                    try:
                        if len(photos) == 1:
                            with open(photos[0].path, "rb") as f:
                                await update.message.reply_photo(
                                    photo=f,
                                    reply_to_message_id=update.message.message_id,
                                )
                        else:
                            media = []
                            file_handles = []
                            for img in photos[:10]:
                                fh = open(img.path, "rb")  # noqa: SIM115
                                file_handles.append(fh)
                                media.append(InputMediaPhoto(media=fh))
                            try:
                                await update.message.chat.send_media_group(
                                    media=media,
                                    reply_to_message_id=update.message.message_id,
                                )
                            finally:
                                for fh in file_handles:
                                    fh.close()
                    except Exception as album_err:
                        logger.warning(
                            "Failed to send photo album", error=str(album_err)
                        )
                for img in documents:
                    try:
                        with open(img.path, "rb") as f:
                            await update.message.reply_document(
                                document=f,
                                filename=img.path.name,
                                reply_to_message_id=update.message.message_id,
                            )
                        await asyncio.sleep(0.5)
                    except Exception as doc_err:
                        logger.warning(
                            "Failed to send document image",
                            path=str(img.path),
                            error=str(doc_err),
                        )

        # Update session info
        context.user_data["last_message"] = update.message.text

        # Add conversation enhancements if available
        features = context.bot_data.get("features")
        conversation_enhancer = (
            features.get_conversation_enhancer() if features else None
        )

        if conversation_enhancer and claude_response:
            try:
                # Update conversation context
                conversation_context = conversation_enhancer.update_context(
                    session_id=claude_response.session_id,
                    user_id=user_id,
                    working_directory=str(current_dir),
                    tools_used=claude_response.tools_used or [],
                    response_content=claude_response.content,
                )

                # Check if we should show follow-up suggestions
                if conversation_enhancer.should_show_suggestions(
                    claude_response.tools_used or [], claude_response.content
                ):
                    # Generate follow-up suggestions
                    suggestions = conversation_enhancer.generate_follow_up_suggestions(
                        claude_response.content,
                        claude_response.tools_used or [],
                        conversation_context,
                    )

                    if suggestions:
                        # Create keyboard with suggestions
                        suggestion_keyboard = (
                            conversation_enhancer.create_follow_up_keyboard(suggestions)
                        )

                        # Send follow-up suggestions
                        await update.message.reply_text(
                            "💡 <b>What would you like to do next?</b>",
                            parse_mode="HTML",
                            reply_markup=suggestion_keyboard,
                        )

            except Exception as e:
                logger.warning(
                    "Conversation enhancement failed", error=str(e), user_id=user_id
                )

        # Log successful message processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[update.message.text[:100]],  # First 100 chars
                success=True,
            )

        logger.info("Text message processed successfully", user_id=user_id)

    except Exception as e:
        # Clean up progress message if it exists
        try:
            await progress_msg.delete()
        except Exception as delete_error:
            logger.debug("Failed to delete progress message", error=str(delete_error))

        await update.message.reply_text(_format_error_message(e), parse_mode="HTML")

        # Log failed processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[update.message.text[:100]],
                success=False,
            )

        logger.error("Error processing text message", error=str(e), user_id=user_id)

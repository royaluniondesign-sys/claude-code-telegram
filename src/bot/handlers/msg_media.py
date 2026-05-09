"""Media message handlers: handle_document, handle_photo, handle_voice."""

import asyncio
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.rate_limiter import RateLimiter
from ...security.validators import SecurityValidator
from ..utils.html_format import escape_html
from .msg_formatters import _format_error_message
from .msg_utils import (
    _estimate_file_processing_cost,
    _update_working_directory_from_claude_response,
)

logger = structlog.get_logger()


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads."""
    user_id = update.effective_user.id
    document = update.message.document
    settings: Settings = context.bot_data["settings"]

    # Initialize prompt to avoid UnboundLocalError
    prompt: str = ""

    # Get services
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")

    logger.info(
        "Processing document upload",
        user_id=user_id,
        filename=document.file_name,
        file_size=document.file_size,
    )

    try:
        # Validate filename using security validator
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(
                    f"❌ <b>File Upload Rejected</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )

                # Log security violation
                if audit_logger:
                    await audit_logger.log_security_violation(
                        user_id=user_id,
                        violation_type="invalid_file_upload",
                        details=f"Filename: {document.file_name}, Error: {error}",
                        severity="medium",
                    )
                return

        # Check file size limits
        max_size = 10 * 1024 * 1024  # 10MB
        if document.file_size > max_size:
            await update.message.reply_text(
                f"❌ <b>File Too Large</b>\n\n"
                f"Maximum file size: {max_size // 1024 // 1024}MB\n"
                f"Your file: {document.file_size / 1024 / 1024:.1f}MB",
                parse_mode="HTML",
            )
            return

        # Check rate limit for file processing
        file_cost = _estimate_file_processing_cost(document.file_size)
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(
                user_id, file_cost
            )
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        # Send processing indicator
        await update.message.chat.send_action("upload_document")

        progress_msg = await update.message.reply_text(
            f"📄 Processing file: <code>{document.file_name}</code>...",
            parse_mode="HTML",
        )

        # Check if enhanced file handler is available
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None

        if file_handler:
            # Use enhanced file handler
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt

                # Update progress message with file type info
                await progress_msg.edit_text(
                    f"📄 Processing {processed_file.type} file: <code>{document.file_name}</code>...",
                    parse_mode="HTML",
                )

            except Exception as e:
                logger.warning(
                    "Enhanced file handler failed, falling back to basic handler",
                    error=str(e),
                )
                file_handler = None  # Fall back to basic handling

        if not file_handler:
            # Fall back to basic file handling
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()

            # Try to decode as text
            try:
                content = file_bytes.decode("utf-8")

                # Check content length
                max_content_length = 50000  # 50KB of text
                if len(content) > max_content_length:
                    content = (
                        content[:max_content_length]
                        + "\n... (file truncated for processing)"
                    )

                # Create prompt with file content
                caption = update.message.caption or "Please review this file:"
                prompt = f"{caption}\n\n**File:** `{document.file_name}`\n\n```\n{content}\n```"

            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "❌ <b>File Format Not Supported</b>\n\n"
                    "File must be text-based and UTF-8 encoded.\n\n"
                    "<b>Supported formats:</b>\n"
                    "• Source code files (.py, .js, .ts, etc.)\n"
                    "• Text files (.txt, .md)\n"
                    "• Configuration files (.json, .yaml, .toml)\n"
                    "• Documentation files",
                    parse_mode="HTML",
                )
                return

        # Delete progress message
        await progress_msg.delete()

        # Create a new progress message for Claude processing
        claude_progress_msg = await update.message.reply_text(
            "🤖 Processing file with Claude...", parse_mode="HTML"
        )

        # Get Claude integration from context
        claude_integration = context.bot_data.get("claude_integration")

        if not claude_integration:
            await claude_progress_msg.edit_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured.",
                parse_mode="HTML",
            )
            return

        # Get current directory and session
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Process with Claude
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

            # Update session ID
            context.user_data["claude_session_id"] = claude_response.session_id

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            # Format and send response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Delete progress message
            await claude_progress_msg.delete()

            # Send responses
            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )

                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            await claude_progress_msg.edit_text(
                _format_error_message(e), parse_mode="HTML"
            )
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)

        # Log successful file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=document.file_name,
                action="upload_processed",
                success=True,
                file_size=document.file_size,
            )

    except Exception as e:
        try:
            await progress_msg.delete()
        except Exception as delete_error:
            logger.debug("Failed to delete progress message", error=str(delete_error))

        error_msg = f"❌ <b>Error processing file</b>\n\n{escape_html(str(e))}"
        await update.message.reply_text(error_msg, parse_mode="HTML")

        # Log failed file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=document.file_name,
                action="upload_failed",
                success=False,
                file_size=document.file_size,
            )

        logger.error("Error processing document", error=str(e), user_id=user_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if enhanced image handler is available
    features = context.bot_data.get("features")
    image_handler = features.get_image_handler() if features else None

    if image_handler:
        try:
            # Send processing indicator
            progress_msg = await update.message.reply_text(
                "📸 Processing image...", parse_mode="HTML"
            )

            # Get the largest photo size
            photo = update.message.photo[-1]

            # Process image with enhanced handler
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )

            # Delete progress message
            await progress_msg.delete()

            # Create Claude progress message
            claude_progress_msg = await update.message.reply_text(
                "🤖 Analyzing image with Claude...", parse_mode="HTML"
            )

            # Get Claude integration
            claude_integration = context.bot_data.get("claude_integration")

            if not claude_integration:
                await claude_progress_msg.edit_text(
                    "❌ <b>Claude integration not available</b>\n\n"
                    "The Claude Code integration is not properly configured.",
                    parse_mode="HTML",
                )
                return

            # Get current directory and session
            current_dir = context.user_data.get(
                "current_directory", settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

            # Process with Claude
            try:
                claude_response = await claude_integration.run_command(
                    prompt=processed_image.prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=session_id,
                )

                # Update session ID
                context.user_data["claude_session_id"] = claude_response.session_id

                # Format and send response
                from ..utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

                # Delete progress message
                await claude_progress_msg.delete()

                # Send responses
                for i, message in enumerate(formatted_messages):
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

            except Exception as e:
                await claude_progress_msg.edit_text(
                    _format_error_message(e), parse_mode="HTML"
                )
                logger.error(
                    "Claude image processing failed", error=str(e), user_id=user_id
                )

        except Exception as e:
            logger.error("Image processing failed", error=str(e), user_id=user_id)
            await update.message.reply_text(
                _format_error_message(e),
                parse_mode="HTML",
            )
    else:
        # Fall back to unsupported message
        await update.message.reply_text(
            "📸 <b>Photo Upload</b>\n\n"
            "Photo processing is not yet supported.\n\n"
            "<b>Currently supported:</b>\n"
            "• Text files (.py, .js, .md, etc.)\n"
            "• Configuration files\n"
            "• Documentation files\n\n"
            "<b>Coming soon:</b>\n"
            "• Image analysis\n"
            "• Screenshot processing\n"
            "• Diagram interpretation",
            parse_mode="HTML",
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice message uploads."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    features = context.bot_data.get("features")
    voice_handler = features.get_voice_handler() if features else None

    if not voice_handler:
        await update.message.reply_text(
            "🎙️ <b>Voice Messages</b>\n\n"
            "Voice transcription is not available.\n"
            f"Provider: <code>{settings.voice_provider_display_name}</code>\n"
            f"Set <code>{settings.voice_provider_api_key_env}</code> to enable.\n"
            "Install optional voice deps with "
            '<code>pip install "claude-code-telegram[voice]"</code>.',
            parse_mode="HTML",
        )
        return

    try:
        progress_msg = await update.message.reply_text(
            "🎙️ Transcribing voice message...", parse_mode="HTML"
        )

        voice = update.message.voice
        processed_voice = await voice_handler.process_voice_message(
            voice, update.message.caption
        )

        await progress_msg.edit_text(
            "🤖 Processing transcription with Claude...", parse_mode="HTML"
        )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured.",
                parse_mode="HTML",
            )
            return

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        try:
            # Keep classic mode aligned with handle_photo: single progress message,
            # no streaming callback or typing heartbeat.
            claude_response = await claude_integration.run_command(
                prompt=processed_voice.prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

            context.user_data["claude_session_id"] = claude_response.session_id

            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    except Exception as e:
        logger.error("Voice processing failed", error=str(e), user_id=user_id)
        await update.message.reply_text(
            _format_error_message(e),
            parse_mode="HTML",
        )

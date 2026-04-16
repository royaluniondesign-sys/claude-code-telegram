"""Media/voice/photo handlers for MessageOrchestrator (agentic mode).

Contains:
  agentic_document  — file upload → brain
  agentic_photo     — photo → brain
  agentic_voice     — voice message transcription → brain
  _handle_agentic_media_message — shared dispatch after transcription/processing
  _voice_unavailable_message    — provider-aware guidance string
"""

from typing import TYPE_CHECKING, Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class AgenticMediaMixin:
    """Mixin providing agentic media handlers.

    Must be mixed into MessageOrchestrator which supplies:
      self.settings, self._handle_alt_brain()
    """

    async def agentic_document(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        prompt = None

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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
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
        self: "MessageOrchestrator",
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

    def _voice_unavailable_message(self: "MessageOrchestrator") -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

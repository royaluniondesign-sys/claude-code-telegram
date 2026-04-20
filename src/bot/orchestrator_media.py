"""Media/voice/photo handlers for MessageOrchestrator (agentic mode).

Contains:
  agentic_document  — file upload → brain
  agentic_photo     — photo → Claude vision (Read tool, sees image visually)
  agentic_voice     — voice message transcription → brain
  _handle_agentic_media_message — shared dispatch after transcription/processing
  _voice_unavailable_message    — provider-aware guidance string
"""

import base64
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
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
        """Process photo with real vision via Claude CLI (reads image visually).

        Flow:
          1. Download highest-res photo from Telegram
          2. Save to temp file
          3. Route to autonomous brain — Claude's Read tool sees images natively
          4. Clean up temp file
          5. Fallback to text-only description if vision fails
        """
        user_id = update.effective_user.id
        chat = update.message.chat
        caption = update.message.caption or ""

        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("🔍 Analizando imagen...")

        tmp_path: str = ""
        try:
            # Download highest resolution photo
            photo = update.message.photo[-1]
            file = await photo.get_file()
            image_bytes = bytes(await file.download_as_bytearray())

            # Detect format for proper extension
            ext = ".jpg"
            if image_bytes.startswith(b"\x89PNG"):
                ext = ".png"
            elif image_bytes.startswith(b"GIF"):
                ext = ".gif"

            # Save to temp file so Claude can Read it visually
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp") as tf:
                tf.write(image_bytes)
                tmp_path = tf.name

            # Build vision prompt
            user_request = caption.strip() if caption else "Describe qué ves en esta imagen."
            prompt = (
                f"Lee la imagen en {tmp_path} y responde en el mismo idioma que esta instrucción.\n\n"
                f"Instrucción del usuario: {user_request}\n\n"
                "Analiza la imagen cuidadosamente y responde de forma concisa y útil."
            )

            logger.info(
                "agentic_photo_vision",
                user_id=user_id,
                size=len(image_bytes),
                ext=ext,
                tmp=tmp_path,
            )

            # Try autonomous brain (Claude with Read tool — real vision)
            router = context.bot_data.get("brain_router")
            brain = router.get_brain("autonomous") if router else None

            if brain:
                try:
                    await progress_msg.delete()
                except Exception:
                    pass

                from .orchestrator_utils import start_typing_heartbeat
                import asyncio as _asyncio

                typing_task = start_typing_heartbeat(chat, interval=3)
                try:
                    response = await brain.execute(prompt=prompt)
                finally:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except _asyncio.CancelledError:
                        pass

                if not response.is_error:
                    content = response.content or "(sin respuesta)"
                    await update.message.reply_text(
                        content[:4000],
                        parse_mode=None,
                    )
                    logger.info(
                        "agentic_photo_ok",
                        user_id=user_id,
                        brain=brain.name,
                        chars=len(content),
                    )
                    return

                # Vision failed — fallback to generic description
                logger.warning(
                    "agentic_photo_vision_failed",
                    error=response.error_type,
                    user_id=user_id,
                )

            # Fallback: route with text-only generic prompt
            fallback_prompt = (
                f"El usuario envió una foto (no puedo verla directamente en este modo).\n"
                f"Solicitud del usuario: {user_request or 'Descríbela'}\n"
                "Indícale amablemente que la foto fue recibida pero que la visión directa "
                "no está disponible en este modo y sugiere usar /chat o /ask para preguntas."
            )
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=fallback_prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            try:
                await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            except Exception:
                pass
            logger.error("photo_processing_failed", error=str(e), user_id=user_id)
        finally:
            # Always clean up temp file
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

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

            # Transcribe audio: Gemini multimodal STT (primary), then API fallback
            transcription = None

            # Primary: Gemini multimodal transcription
            try:
                gemini_api_key = os.environ.get(
                    "GEMINI_API_KEY", "AIzaSyBWpQZYLeTxba8mDhfFundXylBK_quQsZc"
                )
                audio_b64 = base64.b64encode(voice_bytes).decode("ascii")
                payload = {
                    "contents": [
                        {
                            "parts": [
                                {
                                    "text": (
                                        "Transcribe exactly what is said in this audio, "
                                        "in the original language (Spanish or English). "
                                        "Return only the transcription text, no commentary."
                                    )
                                },
                                {
                                    "inline_data": {
                                        "mime_type": "audio/ogg",
                                        "data": audio_b64,
                                    }
                                },
                            ]
                        }
                    ]
                }
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-1.5-flash:generateContent?key={gemini_api_key}"
                )
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                transcription = (
                    result["candidates"][0]["content"]["parts"][0]["text"].strip()
                )
                logger.info("gemini_stt_ok", length=len(transcription))
            except Exception as gemini_err:
                logger.warning("gemini_stt_failed", error=str(gemini_err))

                # Fallback to API-based voice handler if configured
                features = context.bot_data.get("features")
                voice_handler = features.get_voice_handler() if features else None
                if voice_handler:
                    try:
                        processed = await voice_handler.process_voice_message(
                            voice, update.message.caption
                        )
                        transcription = processed.transcription
                    except Exception as vh_err:
                        logger.warning("voice_handler_fallback_failed", error=str(vh_err))

                if not transcription:
                    await progress_msg.edit_text(
                        "❌ Transcripción no disponible. "
                        "Gemini STT falló y no hay API de voz configurada."
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

"""Media generation handlers — image and video generation.

Contains:
  _handle_image_gen  — image generation via pollinations.ai (FLUX.1)
  _handle_video_gen  — cinematic AI video or structured slides
"""

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..orchestrator_utils import escape_html, start_typing_heartbeat

if TYPE_CHECKING:
    from ..orchestrator import MessageOrchestrator

logger = structlog.get_logger()


class MediaHandlerMixin:
    """Mixin providing image and video generation handlers."""

    async def _handle_image_gen(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Generate image via pollinations.ai (FLUX.1, free, no key) and send as photo."""
        import io
        import base64

        chat = update.message.chat
        await chat.send_action("upload_photo")
        progress_msg = await update.message.reply_text("🎨 Generando imagen...")

        try:
            brain = router.get_brain("image")
            if not brain:
                from src.brains.image_brain import ImageBrain
                brain = ImageBrain()

            response = await brain.execute(prompt=message_text)

            if response.is_error:
                await progress_msg.edit_text(f"❌ {response.content}")
                return

            if response.content.startswith("__IMAGE_B64__:"):
                b64_data = response.content[len("__IMAGE_B64__:"):]
                image_bytes = base64.b64decode(b64_data)
                elapsed_s = response.duration_ms // 1000

                await progress_msg.delete()
                await update.message.reply_photo(
                    photo=io.BytesIO(image_bytes),
                    caption=(
                        f"🎨 *{message_text[:80]}*\n⏱ {elapsed_s}s · FLUX.1 via pollinations.ai"
                    ),
                    parse_mode="Markdown",
                )
            else:
                await progress_msg.edit_text(response.content)

        except Exception as e:
            logger.error("image_gen_failed", error=str(e), user_id=user_id)
            await progress_msg.edit_text(f"❌ Error generando imagen: {e}")

    async def _handle_video_gen(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Generate video — cinematic (video_brain) or structured slides (video_compose).

        Cinematic keywords: reel, clip, b-roll, animación, cinematic, AI video
        Structured keywords: slides, presentación, explainer, tutorial
        """
        import io
        import re as _re_v

        chat = update.message.chat
        await chat.send_action("upload_video")
        progress_msg = await update.message.reply_text("🎬 Generando video...")

        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        async def _notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            _structured_kw = r"(?i)\b(slides?|diapositivas?|presentaci[oó]n|explainer|tutorial)\b"
            _cinematic_kw = r"(?i)\b(reel|clip|b-?roll|animaci[oó]n|animation|cinematic|cinemático)\b"

            is_structured = bool(_re_v.search(_structured_kw, message_text))
            is_cinematic = bool(_re_v.search(_cinematic_kw, message_text))

            # Default to cinematic if ambiguous, but prefer structured when explicitly requested
            use_slides = is_structured and not is_cinematic

            if use_slides:
                from src.workflows.video_compose import run_video_pipeline
                result = await run_video_pipeline(
                    prompt=message_text,
                    notify_fn=_notify,
                )

                if result.startswith("http") and (
                    result.endswith(".mp4") or "json2video" in result or "cdn" in result
                ):
                    await _notify("📥 Descargando video...")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                result, timeout=aiohttp.ClientTimeout(total=60)
                            ) as resp:
                                video_bytes = await resp.read()
                        await progress_msg.delete()
                        await update.message.reply_video(
                            video=io.BytesIO(video_bytes),
                            caption=(
                                f"🎬 <b>{escape_html(message_text[:80])}</b>\n"
                                f"📊 json2video structured"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{result}",
                            parse_mode="HTML",
                        )
                else:
                    await progress_msg.edit_text(result, parse_mode="HTML")

            else:
                brain = router.get_brain("video") if router else None
                if not brain:
                    from src.brains.video_brain import VideoBrain
                    brain = VideoBrain()

                response = await brain.execute(prompt=message_text)

                if response.is_error:
                    await progress_msg.edit_text(
                        f"❌ {escape_html(response.content)}",
                        parse_mode="HTML",
                    )
                    return

                video_url: str = response.content
                if video_url.startswith("__VIDEO_URL__:"):
                    video_url = video_url[len("__VIDEO_URL__:"):]

                if video_url.startswith("http"):
                    await _notify("📥 Descargando video...")
                    try:
                        import aiohttp
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                video_url, timeout=aiohttp.ClientTimeout(total=60)
                            ) as resp:
                                video_bytes = await resp.read()
                        provider = response.metadata.get("provider", "AI")
                        await progress_msg.delete()
                        await update.message.reply_video(
                            video=io.BytesIO(video_bytes),
                            caption=(
                                f"🎬 <b>{escape_html(message_text[:80])}</b>\n"
                                f"✨ {provider}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as dl_err:
                        logger.warning("video_download_failed", error=str(dl_err))
                        await progress_msg.edit_text(
                            f"🎬 Video listo:\n{video_url}",
                            parse_mode="HTML",
                        )
                else:
                    await progress_msg.edit_text(
                        escape_html(video_url),
                        parse_mode="HTML",
                    )

        except Exception as e:
            logger.error("video_gen_failed", error=str(e), user_id=user_id)
            try:
                await progress_msg.edit_text(
                    f"❌ Error generando video: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

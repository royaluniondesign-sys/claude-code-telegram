"""Content handlers — email composition and social media pipeline.

Contains:
  _handle_email_native  — compose + send email natively
  _handle_social_post   — social media content pipeline (image + optional post)
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


class ContentHandlerMixin:
    """Mixin providing email and social media content handlers."""

    async def _handle_email_native(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        router: Any,
        message_text: str,
        user_id: int,
    ) -> None:
        """Compose and send email natively — no Claude CLI subprocess.

        1. Uses openrouter (streaming) to compose email JSON from natural language
        2. Parses {to, subject, body} from the response
        3. Calls send_email() directly in Python
        """
        import json as _json
        import re as _re_e

        progress_msg = await update.message.reply_text(
            "📧 <b>Redactando email...</b> · ⠋",
            parse_mode="HTML",
        )
        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        try:
            compose_prompt = (
                f"Extrae y compone el email solicitado. Responde SOLO con JSON válido, sin markdown:\n"
                f'{{"to": "email@destinatario.com", "subject": "Asunto", "body": "Cuerpo del email"}}\n\n'
                f"Contexto del dueño: royaluniondesign@gmail.com es el email de Ricardo (yo mismo).\n"
                f"Si dice 'envíate', 'mándame', 'a mí', etc → to: royaluniondesign@gmail.com\n\n"
                f"Petición: {message_text}\n\n"
                f"Responde SOLO el JSON."
            )

            brain = router.get_brain("openrouter") if router else None
            if brain is None:
                brain = router.get_brain("haiku") if router else None

            composed: dict = {}  # type: ignore[type-arg]
            if brain and getattr(brain, "supports_streaming", False):
                # Stream compose
                accumulated = ""
                async for chunk in brain.execute_stream(
                    prompt=compose_prompt,
                    working_directory=str(self.settings.approved_directory),
                    timeout_seconds=30,
                ):
                    if chunk.startswith("\x00ERROR:"):
                        break
                    accumulated += chunk
                raw = accumulated.strip()
            else:
                resp = await brain.execute(prompt=compose_prompt) if brain else None
                raw = (resp.content if resp else "").strip()

            # Parse JSON from response (handles ```json ... ``` wrapping too)
            json_match = _re_e.search(r'\{[^{}]+\}', raw, _re_e.DOTALL)
            if json_match:
                try:
                    composed = _json.loads(json_match.group())
                except Exception:
                    pass

            if not composed.get("to") or not composed.get("subject"):
                await progress_msg.edit_text(
                    "❌ No pude extraer destinatario/asunto del mensaje. "
                    "Usa: <code>/email correo@x.com | Asunto | Cuerpo</code>",
                    parse_mode="HTML",
                )
                return

            await progress_msg.edit_text(
                f"📧 Enviando a <code>{escape_html(composed['to'])}</code>...",
                parse_mode="HTML",
            )

            from src.actions import call_tool
            result = await call_tool(
                "send_email",
                to=composed["to"],
                subject=composed["subject"],
                body=composed.get("body", ""),
            )

            await progress_msg.edit_text(
                f"📧 {escape_html(result)}",
                parse_mode="HTML",
            )
            logger.info(
                "email_native_sent",
                to=composed["to"],
                subject=composed["subject"],
            )

        except Exception as e:
            logger.error("email_native_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error enviando email: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

    async def _handle_social_post(
        self: "MessageOrchestrator",
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> None:
        """Run social media content pipeline: generate brand image → show in Telegram → post.

        Flow:
          1. Parse request (topic, platform, format)
          2. Generate structured content via Gemini CMO prompt
          3. Render brand image (Anthropic fonts/colors)
          4. Send image as Telegram photo + caption
          5. Post via N8N in background
        """
        import io
        import re as _re_social

        progress_msg = await update.message.reply_text(
            "📱 <b>Generando imagen...</b>",
            parse_mode="HTML",
        )
        _typing_task = start_typing_heartbeat(update.effective_chat, interval=3.0)

        async def _notify(text: str) -> None:
            try:
                await progress_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                pass

        try:
            from src.social.image_gen import PostSpec, generate_post_image
            from src.workflows.social_post import generate_post_content, parse_social_request

            # Detect format from message
            lower = message_text.lower()
            if _re_social.search(r"\b(reel|reels|story|stories|vertical|9.16)\b", lower):
                fmt = "9:16"
            elif _re_social.search(r"\b(landscape|horizontal|wide|4.3|16.9)\b", lower):
                fmt = "4:3"
            else:
                fmt = "1:1"

            parsed = parse_social_request(message_text)
            topic = parsed["topic"] or message_text.strip()
            platform = parsed["platform"]

            await _notify(f"✍️ Generando contenido para <b>{platform}</b>...")

            content = await generate_post_content(topic, platform)

            await _notify("🎨 Renderizando imagen con tipografía Anthropic...")

            spec = PostSpec(
                headline=content["headline"],
                subheadline=content["subheadline"],
                caption=content["caption"],
                tag=content["tag"],
                format=fmt,
            )
            png_bytes = generate_post_image(spec)

            # Delete progress message — about to send photo
            try:
                await progress_msg.delete()
            except Exception:
                pass

            caption_text = content["caption"]
            # Truncate caption if too long for Telegram (max 1024 chars)
            if len(caption_text) > 1020:
                caption_text = caption_text[:1017] + "..."

            await update.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=caption_text,
                filename=f"aura_{platform}_{fmt.replace(':', '')}.png",
            )

            logger.info(
                "social_image_sent",
                platform=platform,
                format=fmt,
                topic=topic[:60],
                size_kb=len(png_bytes) // 1024,
            )

            # Log to publication database (SQLite + Drive/Sheets in background)
            async def _log_publication() -> None:
                try:
                    from src.integrations.publication_db import get_publication_db
                    db = get_publication_db()
                    await db.log_publication(
                        platform=platform,
                        format=fmt,
                        topic=topic,
                        headline=content["headline"],
                        subheadline=content["subheadline"],
                        caption=content["caption"],
                        tag=content["tag"],
                        image_bytes=png_bytes,
                        status="generated",
                    )
                except Exception as exc:
                    logger.warning("publication_log_error", error=str(exc))

            asyncio.create_task(_log_publication())

            # Detect "publicar" / "post now" intent — post directly to Instagram
            import re as _re2
            should_post = bool(_re2.search(
                r"\b(publica|publish|post(?:ea)?|sube?|upload|publicar)\b",
                message_text.lower(),
            ))

            if should_post and platform == "instagram":
                async def _ig_post_background() -> None:
                    try:
                        from src.workflows.instagram_direct import post_image as _ig_post
                        result = await _ig_post(png_bytes, content["caption"])
                        if result["ok"]:
                            await update.message.reply_text(
                                f"✅ Publicado en Instagram!\n🔗 {result['url']}",
                                disable_web_page_preview=True,
                            )
                        else:
                            await update.message.reply_text(
                                f"⚠️ No se pudo publicar: {result['error'][:200]}"
                            )
                    except Exception as exc:
                        logger.warning("ig_post_background_error", error=str(exc))

                asyncio.create_task(_ig_post_background())

        except Exception as e:
            logger.error("social_pipeline_error", error=str(e))
            try:
                await progress_msg.edit_text(
                    f"❌ Error: {escape_html(str(e)[:300])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            if _typing_task and not _typing_task.done():
                _typing_task.cancel()

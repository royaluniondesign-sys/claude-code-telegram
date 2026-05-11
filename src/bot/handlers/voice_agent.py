"""Voice agent Telegram handlers — /voice command to control GeminiLiveAgent daemon."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_HELP = (
    "🎤 *AURA Voice Agent* — Gemini 2\\.5 Flash Native Audio \\(gratis\\)\n\n"
    "`/voice start` — inicia el agente de voz en el Mac\n"
    "`/voice stop` — detiene el agente\n"
    "`/voice status` — estado actual\n"
    "`/voice send <texto>` — envía texto al agente \\(responde por voz en el Mac\\)\n"
    "`/voice transcript` — últimas conversaciones\n\n"
    "El agente escucha el micrófono del Mac en tiempo real y tiene acceso a todas "
    "las tools de AURA \\+ Hermes \\+ control del ordenador\\."
)


async def voice_command(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Handle /voice command."""
    args = context.args or []
    sub = args[0].lower() if args else "help"

    if sub == "help" or not args:
        await update.message.reply_text(_HELP, parse_mode="MarkdownV2")
        return

    if sub == "status":
        await _voice_status(update)
        return

    if sub == "start":
        await _voice_start(update)
        return

    if sub == "stop":
        await _voice_stop(update)
        return

    if sub in ("send", "say") and len(args) > 1:
        text = " ".join(args[1:])
        await _voice_send(update, text)
        return

    if sub == "transcript":
        await _voice_transcript(update)
        return

    await update.message.reply_text(f"Subcomando desconocido: `{sub}`\nUsa `/voice help`", parse_mode="Markdown")


async def _voice_status(update: "Update") -> None:
    from src.voice.voice_daemon import get_daemon_status
    status = await get_daemon_status()
    if status:
        state = status.get("status", "unknown")
        uptime = status.get("uptime_s", 0)
        turns = status.get("transcript_count", 0)
        icon = "🟢" if state == "running" else "🟡"
        await update.message.reply_text(
            f"{icon} Voice agent: *{state}*\n"
            f"Uptime: {uptime}s | Turnos: {turns}\n"
            f"Modelo: {status.get('model', 'gemini-2.5-flash-native-audio')}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("🔴 Voice agent: *detenido*\nUsa `/voice start` para iniciar.", parse_mode="Markdown")


async def _voice_start(update: "Update") -> None:
    import aiohttp
    from src.voice.voice_daemon import _PORT
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{_PORT}/start",
                json={},
                timeout=aiohttp.ClientTimeout(total=35),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    await update.message.reply_text(
                        "🎤 *Voice agent iniciado*\nGemini 2\\.5 Flash Native Audio activo\\.\n"
                        "Habla directamente al micrófono del Mac\\.",
                        parse_mode="MarkdownV2",
                    )
                else:
                    await update.message.reply_text(f"❌ {data.get('error', 'Unknown error')}")
    except Exception as e:
        await update.message.reply_text(
            f"❌ No se pudo conectar al voice daemon\\.\n"
            f"¿Está corriendo? `launchctl list com\\.aura\\.voice\\-agent`\n"
            f"Error: `{str(e)}`",
            parse_mode="MarkdownV2",
        )


async def _voice_stop(update: "Update") -> None:
    import aiohttp
    from src.voice.voice_daemon import _PORT
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{_PORT}/stop",
                json={},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                await update.message.reply_text("⏹ Voice agent detenido.")
    except Exception:
        await update.message.reply_text("⚠️ Voice agent no estaba corriendo.")


async def _voice_send(update: "Update", text: str) -> None:
    from src.voice.voice_daemon import send_text_to_voice
    ok = await send_text_to_voice(text)
    if ok:
        await update.message.reply_text(f"✉️ Enviado al voice agent: _{text}_", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Voice agent no disponible. Usa `/voice start`.")


async def _voice_transcript(update: "Update") -> None:
    import aiohttp
    from src.voice.voice_daemon import _PORT
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{_PORT}/transcript?limit=10",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                entries = data.get("transcript", [])
                if not entries:
                    await update.message.reply_text("Sin transcripciones todavía.")
                    return
                lines = []
                for e in entries[-10:]:
                    icon = "✨" if e["speaker"] == "aura" else "🎤"
                    lines.append(f"{icon} *{e['speaker'].upper()}*: {e['text'][:150]}")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo obtener transcript: {e}")

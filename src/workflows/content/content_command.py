"""Telegram command handler: /content — Content Agent control panel.

Commands:
  /content plan   — Run brain now, generate today's plans
  /content run    — Execute today's plans (generate + post)
  /content status — Show recent content log
  /content next   — Show what's scheduled next
  /content feeds  — Show feed health (how many items fetched)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Awaitable

log = logging.getLogger("content.command")

PLANS_DIR = Path.home() / ".aura" / "content_plans"

FORMAT_EMOJI = {
    "post_4_5": "🖼",
    "carousel": "📑",
    "reel": "🎬",
    "story": "⭕",
    "text_post": "📝",
}

PLATFORM_EMOJI = {
    "instagram": "📸",
    "tiktok": "🎵",
    "youtube_shorts": "▶️",
    "linkedin": "💼",
}


async def handle_content_command(
    args: str,
    send: Callable[[str], Awaitable[None]],
) -> None:
    """Main dispatcher for /content subcommands."""
    sub = args.strip().lower().split()[0] if args.strip() else "status"

    if sub == "plan":
        await cmd_plan(send)
    elif sub == "run":
        await cmd_run(send)
    elif sub == "status":
        await cmd_status(send)
    elif sub == "next":
        await cmd_next(send)
    elif sub == "feeds":
        await cmd_feeds(send)
    else:
        await send(
            "**Content Agent** — comandos:\n"
            "`/content plan` — generar plan del día\n"
            "`/content run` — ejecutar plan (generar + publicar)\n"
            "`/content status` — historial reciente\n"
            "`/content next` — próximos posts programados\n"
            "`/content feeds` — estado de fuentes"
        )


async def cmd_plan(send: Callable) -> None:
    """Run brain: fetch feeds → select topics → generate plans."""
    await send("🧠 Content Brain arrancando… analizo fuentes y genero plan del día.")
    try:
        from .content_brain import run_daily_brain
        result = await run_daily_brain()

        if not result.get("ok"):
            await send(f"❌ Error: {result.get('error', 'desconocido')}")
            return

        plans = result.get("plans", [])
        lines = [
            f"✅ **Plan generado** — {result['date']}",
            f"📡 {result['feed_items']} artículos analizados · {len(plans)} piezas planeadas\n",
        ]
        for i, p in enumerate(plans, 1):
            fmt = FORMAT_EMOJI.get(p.get("format", ""), "📄")
            plats = " ".join(PLATFORM_EMOJI.get(pl, "?") for pl in p.get("platforms", []))
            lines.append(
                f"{i}. {fmt} **{p.get('headline', '?')}**\n"
                f"   {plats} · {p.get('format')} · {p.get('pillar', '')}\n"
                f"   _¿Por qué ahora?_ {p.get('why_now', '')}"
            )
        lines.append("\nCorre `/content run` para generar y publicar.")
        await send("\n".join(lines))

    except Exception as e:
        log.error("cmd_plan_error: %s", e)
        await send(f"❌ Error en brain: {e}")


async def cmd_run(send: Callable) -> None:
    """Execute today's plans."""
    await send("🚀 Ejecutando plan de hoy… generando contenido y publicando.")
    try:
        from .content_executor import execute_todays_plans
        results = await execute_todays_plans(notify_fn=send)

        if not results:
            await send("ℹ️ Sin planes pendientes. Corre `/content plan` primero.")
            return

        ok = sum(1 for r in results if r.get("ok"))
        failed = len(results) - ok
        await send(f"✅ {ok} publicados · ❌ {failed} fallidos — todo listo.")

    except Exception as e:
        log.error("cmd_run_error: %s", e)
        await send(f"❌ Error en ejecución: {e}")


async def cmd_status(send: Callable) -> None:
    """Show recent content history."""
    try:
        from .content_memory import recent_topics
        topics = recent_topics(15)
        if not topics:
            await send("📋 Sin historial aún. Corre `/content plan` para empezar.")
            return

        lines = ["**Historial de contenido** (últimas 15 piezas)\n"]
        for t in topics:
            status_icon = {"published": "✅", "planned": "🕐", "failed": "❌"}.get(t["status"], "?")
            fmt = FORMAT_EMOJI.get(t.get("format", ""), "📄")
            date = t["created"][:10]
            lines.append(f"{status_icon} {fmt} {t['title'][:45]} · {date}")

        await send("\n".join(lines))
    except Exception as e:
        await send(f"❌ Error: {e}")


async def cmd_next(send: Callable) -> None:
    """Show today's and upcoming scheduled content."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_file = PLANS_DIR / f"{today}.json"

    if not plan_file.exists():
        await send("📭 Sin plan para hoy. Corre `/content plan`.")
        return

    try:
        data = json.loads(plan_file.read_text())
        plans = data.get("plans", [])
        lines = [f"**Plan del día** — {today}\n"]
        for p in plans:
            fmt = FORMAT_EMOJI.get(p.get("format", ""), "📄")
            plats = " ".join(PLATFORM_EMOJI.get(pl, "?") for pl in p.get("platforms", []))
            status = {"published": "✅ publicado", "planned": "🕐 pendiente", "failed": "❌ falló"}.get(
                p.get("status", "planned"), "?"
            )
            scheduled = p.get("scheduled_at", "")[:16].replace("T", " ") if p.get("scheduled_at") else ""
            lines.append(
                f"{fmt} **{p.get('headline', '?')}**\n"
                f"   {plats} · {status}" + (f" · {scheduled}" if scheduled else "")
            )
        await send("\n".join(lines))
    except Exception as e:
        await send(f"❌ Error: {e}")


async def cmd_feeds(send: Callable) -> None:
    """Show feed health."""
    await send("📡 Comprobando fuentes…")
    try:
        from .feed_aggregator import fetch_all, FEEDS
        items = await fetch_all(max_age_hours=72)

        # Group by source
        by_source: dict[str, int] = {}
        for item in items:
            by_source[item.source] = by_source.get(item.source, 0) + 1

        lines = [f"**Fuentes RSS** — {len(items)} artículos (72h)\n"]
        for feed in FEEDS:
            src = feed["source"]
            count = by_source.get(src, 0)
            icon = "🟢" if count > 0 else "🔴"
            lines.append(f"{icon} {src}: {count} artículos")

        await send("\n".join(lines))
    except Exception as e:
        await send(f"❌ Error: {e}")

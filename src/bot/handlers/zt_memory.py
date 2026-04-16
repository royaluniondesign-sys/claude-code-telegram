"""Zero-token memory and cost commands — memory, costs."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenMemoryMixin:
    """Mixin: memory management and economy zero-token commands."""

    async def _zt_memory(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ View/update AURA's memory and learned facts.

        /memory          — show all learned facts
        /memory add <f>  — manually add a fact
        /memory client <email> [nombre] [empresa]  — register a client
        /memory task <desc>  — record a completed task
        /memory clear    — clear learned facts (keeps identity)
        /memory identity — show AURA's identity profile
        """
        from ...context.aura_context import (
            format_for_display, update_memory, add_client, add_task,
            get_identity, _MEMORY_FILE, _BRAIN_DIR,
        )

        text = (update.message.text or "").strip()
        parts = text.split(None, 2)  # /memory [subcommand] [rest]
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "add" and len(parts) > 2:
            fact = parts[2].strip()
            update_memory(fact)
            await update.message.reply_text(
                f"✅ Guardado en memoria:\n<i>{self._escape_html(fact)}</i>",
                parse_mode="HTML",
            )

        elif sub == "client" and len(parts) > 2:
            cparts = parts[2].split(None, 2)
            email = cparts[0]
            name = cparts[1] if len(cparts) > 1 else ""
            company = cparts[2] if len(cparts) > 2 else ""
            add_client(email=email, name=name, company=company)
            await update.message.reply_text(
                f"✅ Cliente registrado: <code>{self._escape_html(email)}</code>"
                + (f" — {self._escape_html(name)}" if name else ""),
                parse_mode="HTML",
            )

        elif sub == "task" and len(parts) > 2:
            add_task(parts[2].strip())
            await update.message.reply_text(
                f"✅ Tarea guardada en memoria.", parse_mode="HTML"
            )

        elif sub == "clear":
            # Reset memory.md to empty structure (keeps identity.md intact)
            _BRAIN_DIR.mkdir(parents=True, exist_ok=True)
            _MEMORY_FILE.write_text(
                "# AURA Memory — Hechos aprendidos\n\n"
                "## Clientes de RUD\n\n"
                "## Proyectos activos\n\n"
                "## Tareas recientes\n\n"
                "## Notas\n",
                encoding="utf-8",
            )
            await update.message.reply_text("🗑️ Memoria borrada (identidad intacta).")

        elif sub == "identity":
            identity = get_identity()
            if identity:
                # Truncate for Telegram
                display = identity[:3500]
                await update.message.reply_text(
                    f"<b>🧠 AURA Identity</b>\n\n<pre>{self._escape_html(display)}</pre>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("❌ Identity file not found at ~/.aura/brain/identity.md")

        elif sub == "palace":
            # Show MemPalace semantic memory stats
            try:
                from ...context.mempalace_memory import get_all_memories, palace_count
                total = await palace_count()
                recent = await get_all_memories(limit=5)
                lines = [f"<b>🧠 Palace — {total} memorias semánticas</b>"]
                for mem in recent:
                    short = mem[:120].replace("<", "&lt;").replace(">", "&gt;")
                    lines.append(f"• <i>{short}</i>")
                await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Palace error: {e}")

        else:
            # Default: show memory
            text_out = format_for_display()
            await update.message.reply_text(text_out[:4000], parse_mode="HTML")

    async def _zt_costs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Token economy stats — cache hits, routing, savings."""
        from ...economy.cache import ResponseCache

        monitor = context.bot_data.get("rate_monitor")
        cache = ResponseCache()
        stats = cache.stats()

        lines = ["<b>💰 Token Economy</b>\n"]
        lines.append(
            f"📦 Cache: {stats['fresh_entries']} fresh / {stats['total_entries']} total"
        )
        lines.append(f"   Hits: {stats['total_hits']} (tokens saved)")
        lines.append(f"   DB: {stats.get('db_size_kb', 0)}KB")

        if monitor:
            lines.append("\n<b>Usage this window:</b>")
            for usage in monitor.get_all_usage():
                lines.append(
                    f"  {usage.brain_name}: {usage.requests_in_window} req"
                    f" · {usage.errors_in_window} err"
                )

        lines.append(
            "\n💡 Zero-token: !, $, /ls, /git, /sh bypass LLMs entirely"
        )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

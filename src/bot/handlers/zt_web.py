"""Zero-token web/search/queue commands — web, search, queue."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.html_format import escape_html

logger = structlog.get_logger()


class ZeroTokenWebMixin:
    """Mixin: web, search, and task queue zero-token commands."""

    async def _zt_web(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Fetch and analyze a URL via Gemini (has web access).

        /web https://example.com
        /web https://example.com analiza el SEO
        """
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text(
                "Uso: <code>/web &lt;url&gt; [instrucción opcional]</code>\n"
                "Ejemplo: <code>/web https://oxyzen.es analiza el SEO</code>",
                parse_mode="HTML",
            )
            return

        url_and_rest = parts[1].strip()
        # Route to gemini (web-aware brain)
        router = context.bot_data.get("brain_router")
        if not router:
            await update.message.reply_text("Router no disponible.")
            return

        # Build prompt with URL explicit
        prompt = f"Analiza esta URL: {url_and_rest}"

        from ...bot.orchestrator import MessageOrchestrator
        if hasattr(self, "_handle_alt_brain"):
            await self._handle_alt_brain(
                update, context, router, prompt,
                update.effective_user.id, brain_name="gemini",
            )

    async def _zt_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Force web search via Gemini CLI.

        /search últimas noticias sobre IA
        /search precio MacBook Pro M4
        """
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text(
                "Uso: <code>/search &lt;query&gt;</code>\n"
                "Ejemplo: <code>/search precio Claude Pro 2026</code>",
                parse_mode="HTML",
            )
            return

        query = parts[1].strip()
        router = context.bot_data.get("brain_router")
        if not router:
            await update.message.reply_text("Router no disponible.")
            return

        if hasattr(self, "_handle_alt_brain"):
            await self._handle_alt_brain(
                update, context, router, query,
                update.effective_user.id, brain_name="gemini",
            )

    async def _zt_queue(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Queue a background task with auto brain routing.

        /queue <description>           → meta-router picks brain
        /queue urgent <description>    → urgent flag, runs first, Haiku speed
        /queue fix <description>       → mark as fix category

        Examples:
          /queue refactoriza el módulo de auth y añade tests
          /queue urgent revisa si hay errores en los últimos logs
          /queue fix el daemon de Termora no reinicia automáticamente
        """
        import asyncio as _asyncio
        from ...infra.task_store import create_task as _create_task
        from ...claude.meta_router import route_request as _route

        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text(
                "<b>⚡ /queue</b> — encola una tarea en background con auto-routing de brain\n\n"
                "<b>Uso:</b> <code>/queue [urgent|fix] &lt;descripción&gt;</code>\n\n"
                "<b>Ejemplos:</b>\n"
                "<code>/queue refactoriza el módulo de auth y añade tests</code>\n"
                "<code>/queue urgent revisa si hay errores en los últimos logs</code>\n"
                "<code>/queue fix el daemon de Termora no reinicia automáticamente</code>\n\n"
                "El meta-router detecta complejidad y elige el brain óptimo.",
                parse_mode="HTML",
            )
            return

        description = parts[1].strip()
        urgent = False
        category = "user"

        # Parse flags
        words = description.split(None, 1)
        if words[0].lower() == "urgent":
            urgent = True
            description = words[1].strip() if len(words) > 1 else ""
        elif words[0].lower() in ("fix", "arregla", "arreglar"):
            category = "fix"
            description = words[1].strip() if len(words) > 1 else description

        if not description:
            await update.message.reply_text("Descripción vacía.")
            return

        # Meta-router decides the brain
        decision = _route(
            text=description,
            urgent=urgent,
            category=category,
        )
        brain = decision.tier.value  # haiku / sonnet / opus

        task = _create_task(
            title=description[:120],
            description=description,
            priority="critical" if urgent else "medium",
            category=category,
            created_by="user",
            auto_fix=False,
            urgent=urgent,
            brain=brain,
            tags=["user_queued"],
        )

        tier_icons = {"haiku": "🟠", "sonnet": "🟡", "opus": "🔴"}
        icon = tier_icons.get(brain, "🤖")
        urgent_tag = " ⚡ <b>URGENTE</b>" if urgent else ""

        await update.message.reply_text(
            f"✅ Tarea encolada{urgent_tag}\n\n"
            f"<b>ID:</b> <code>{task['id'][:8]}</code>\n"
            f"<b>Brain:</b> {icon} {brain} (meta-router score: {decision.score})\n"
            f"<b>Tarea:</b> {escape_html(description[:200])}\n\n"
            f"AURA la ejecutará en el próximo ciclo (cada 5 min).\n"
            f"<code>/tasks</code> para ver estado.",
            parse_mode="HTML",
        )

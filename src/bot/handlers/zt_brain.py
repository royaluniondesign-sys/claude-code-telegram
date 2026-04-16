"""Zero-token brain management commands — brain, task, brains."""

from pathlib import Path

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Brain display metadata
_BRAIN_EMOJIS = {
    "haiku": "🟡", "sonnet": "🟠", "opus": "🔴",
    "codex": "🟢", "opencode": "🔶", "cline": "🟣",
    "gemini": "🔵", "openrouter": "🌐",
}

_VALID_BRAINS = ["haiku", "sonnet", "opus", "codex", "opencode", "cline", "gemini", "openrouter"]


class ZeroTokenBrainMixin:
    """Mixin: brain management zero-token commands."""

    async def _zt_brain(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show all brains / switch active brain. Usage: /brain [name]"""
        import shutil
        import os

        # --- /brain <name> → switch active brain for this user ---
        args = (update.message.text or "").split()[1:]
        if args:
            name = args[0].lower().strip()
            router = context.bot_data.get("brain_router")
            user_id = update.effective_user.id
            if name == "auto":
                # Reset to smart routing
                if router:
                    router.reset_user_brain(user_id)
                await update.message.reply_text(
                    "🔄 Brain reset — routing automático activado", parse_mode="HTML"
                )
                return
            if name not in _VALID_BRAINS:
                await update.message.reply_text(
                    f"❌ Brain desconocido: <code>{name}</code>\n"
                    f"Válidos: {' · '.join(_VALID_BRAINS)} · auto",
                    parse_mode="HTML",
                )
                return
            if router:
                ok = router.set_active_brain(name, user_id)
                if ok:
                    emoji = _BRAIN_EMOJIS.get(name, "🧠")
                    await update.message.reply_text(
                        f"{emoji} <b>{name}</b> activado — todos tus mensajes van a {name}\n"
                        f"<i>/brain auto</i> para volver al routing inteligente",
                        parse_mode="HTML",
                    )
                    return
            await update.message.reply_text("Router no disponible.")
            return

        # --- /brain (sin args) → mostrar estado ---
        extra_path = "/opt/homebrew/bin:/usr/local/bin"
        full_path = f"{extra_path}:{os.environ.get('PATH', '')}"
        router = context.bot_data.get("brain_router")
        user_id = update.effective_user.id
        active = router.get_active_brain_name(user_id) if router else "?"

        # Brain definitions with CLI binary checks
        BRAINS = [
            ("🟡", "haiku",       "claude",    "Max plan · ~$0",    "análisis ligero"),
            ("🟠", "sonnet",      "claude",    "Max plan · ~$0",    "código complejo"),
            ("🔴", "opus",        "claude",    "Max plan · ~$0",    "arquitectura deep"),
            ("🔵", "gemini",      "gemini",    "Google free (CLI)", "chat · búsqueda · análisis"),
            ("🌐", "openrouter",  "curl",      "OpenRouter free",   "code · deep · cascade 7 modelos"),
            ("🔶", "opencode",    "opencode",  "OpenRouter free",   "código free tier (legacy)"),
            ("🟣", "cline",       "cline",     "Ollama local · $0", "código local offline"),
            ("🟢", "codex",       "codex",     "OpenAI sub",        "agente código OpenAI"),
        ]

        ROUTING = [
            ("⚡", "BASH/GIT/FILES",    "zero-token",        "sin LLM"),
            ("🔵", "SEARCH",            "gemini CLI",         "web tools Google"),
            ("🌐", "CHAT/CODE/DEEP",    "openrouter",         "HTTP cascade free"),
            ("🟡", "EMAIL/CALENDAR",    "haiku",              "Claude tools"),
            ("↗️", "fallback",           "gemini→openrouter→haiku→sonnet→opus", "cascade"),
        ]

        # Load real usage data from global rate monitor
        try:
            from ...infra.rate_monitor import get_global_monitor
            _rm = get_global_monitor()
        except Exception:
            _rm = None

        brain_lines = []
        for emoji, name, binary, cost, use in BRAINS:
            found = shutil.which(binary, path=full_path)
            st = "✅" if found else "❌"
            lock = " ◀ activo" if name == active else ""
            # Real usage count from rate monitor
            req_str = ""
            if _rm:
                try:
                    u = _rm.get_usage(name)
                    if u.requests_in_window > 0:
                        req_str = f" · {u.requests_in_window}req"
                except Exception:
                    pass
            brain_lines.append(f"  {emoji} <b>{name}</b>: {st} {cost}{req_str}{lock}")
            brain_lines.append(f"      └ {use}")

        route_lines = [f"  {e} {t} → <b>{tgt}</b> ({h})" for e, t, tgt, h in ROUTING]

        lines = [
            "<b>🧠 AURA — 8 Brains</b>\n",
            *brain_lines,
            "\n<b>Routing automático:</b>",
            *route_lines,
            "\n💡 <code>/brain openrouter</code> · <code>/brain gemini</code> · <code>/brain auto</code>",
            "💡 <i>/limits</i> = uso · <i>/task &lt;brain&gt; &lt;prompt&gt;</i> = one-shot",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _zt_task(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Run ONE task with a specific brain. /task <brain> <prompt>

        Unlike /brain which locks all messages, /task runs a single prompt
        through the chosen brain without changing the default routing.

        Examples:
          /task opencode crea un archivo /tmp/hello.py con hello world
          /task haiku explica qué hace este código: def fib(n): ...
          /task cline refactoriza el módulo de autenticación
        """
        text = update.message.text or ""
        parts = text.split(None, 2)  # /task <brain> <rest>
        valid = ["haiku", "sonnet", "opus", "codex", "opencode", "cline", "gemini"]

        if len(parts) < 3:
            examples = "\n".join([
                "<code>/task opencode crea un script bash que liste los 5 procesos más pesados</code>",
                "<code>/task haiku explica qué hace esta función: ...</code>",
                "<code>/task cline refactoriza ~/proyecto/main.py</code>",
            ])
            await update.message.reply_text(
                f"<b>⚡ /task</b> — ejecuta una tarea con un brain específico (sin bloquear el routing)\n\n"
                f"<b>Uso:</b> <code>/task &lt;brain&gt; &lt;prompt&gt;</code>\n\n"
                f"<b>Ejemplos:</b>\n{examples}\n\n"
                f"<b>Brains disponibles:</b> {' · '.join(valid)}",
                parse_mode="HTML",
            )
            return

        brain_name = parts[1].lower()
        prompt = parts[2].strip()

        if brain_name not in valid:
            await update.message.reply_text(
                f"❌ Brain desconocido: <code>{brain_name}</code>\n"
                f"Válidos: {' · '.join(valid)}",
                parse_mode="HTML",
            )
            return

        router = context.bot_data.get("brain_router")
        if not router:
            await update.message.reply_text("Router no disponible.")
            return

        brain = router.get_brain(brain_name)
        if not brain:
            await update.message.reply_text(f"Brain <code>{brain_name}</code> no inicializado.", parse_mode="HTML")
            return

        # Run the task — reuse _handle_alt_brain via the orchestrator parent
        from ...bot.orchestrator import MessageOrchestrator
        orchestrator = context.bot_data.get("orchestrator")
        if orchestrator and hasattr(orchestrator, "_handle_alt_brain"):
            await orchestrator._handle_alt_brain(
                update, context, router, prompt,
                update.effective_user.id, brain_name=brain_name,
            )
        else:
            # Fallback: direct execute
            current_dir = str(context.user_data.get("current_directory", str(Path.home())))
            progress = await update.message.reply_text(
                f"{brain.emoji} <b>{brain.display_name}</b> trabajando...", parse_mode="HTML"
            )
            try:
                resp = await brain.execute(prompt=prompt, working_directory=current_dir)
                content = (resp.content or "(sin respuesta)")[:3900]
                dur = f" · {resp.duration_ms/1000:.1f}s" if resp.duration_ms else ""
                await progress.edit_text(
                    f"{brain.emoji} <b>{brain.display_name}</b>{dur}\n\n{content}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await progress.edit_text(f"❌ Error: {e}", parse_mode="HTML")

    async def _zt_brains(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show all brains and their status."""
        router = context.bot_data.get("brain_router")
        if not router:
            await update.message.reply_text("Brain router not initialized.")
            return

        user_id = update.effective_user.id
        active_name = router.get_active_brain_name(user_id)
        infos = await router.get_all_info()

        status_icons = {
            "ready": "✅",
            "not_installed": "❌",
            "not_authenticated": "🔑",
            "error": "⚠️",
            "rate_limited": "⏱",
        }

        lines = ["<b>🧠 AURA Brains</b>\n"]
        for info in infos:
            s_icon = status_icons.get(info.get("status", "error"), "❓")
            active = " ◀️" if info["name"] == active_name else ""
            lines.append(
                f"{info['emoji']} <b>{info['display_name']}</b>{active}\n"
                f"   {s_icon} {info['status']}"
            )

        lines.append(f"\n/brain para ver estado detallado")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

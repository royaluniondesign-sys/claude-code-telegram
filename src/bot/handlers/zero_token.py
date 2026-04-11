"""Zero-token command handlers — execute without consuming AI tokens.

All handlers are mixin methods that get composed into MessageOrchestrator.
They use self.settings, self._bash_passthrough(), and self._escape_html().
"""

import asyncio
import json as _json
from pathlib import Path
from typing import Any, Dict

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.html_format import escape_html

logger = structlog.get_logger()


class ZeroTokenMixin:
    """Mixin: system, workspace, brain, dashboard, voice, and workflow commands."""

    # --- System commands ---

    async def _zt_ls(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ List files without Claude."""
        args = update.message.text.split()[1:] if update.message.text else []
        target = args[0] if args else context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        await self._bash_passthrough(update, f"ls -la {target}")

    async def _zt_pwd(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show current directory."""
        current = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        await update.message.reply_text(f"<code>{current}</code>", parse_mode="HTML")

    async def _zt_git(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Git status without Claude."""
        args = update.message.text.split()[1:] if update.message.text else []
        cmd = " ".join(args) if args else "status -sb"
        current = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        await self._bash_passthrough(update, f"cd {current} && git {cmd}")

    async def _zt_health(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Full health check with watchdog."""
        from ...infra.watchdog import Watchdog

        watchdog = Watchdog()
        report = await watchdog.check_and_heal()
        text = watchdog.format_report(report)
        await update.message.reply_text(text, parse_mode="HTML")

    async def _zt_terminal(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Get Termora terminal — auto-restarts if down, one-tap link."""
        import urllib.request
        import json as _json
        import asyncio as _asyncio
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        termora_port = 4030

        async def _fetch_info() -> dict | None:
            try:
                loop = _asyncio.get_event_loop()
                def _get():
                    with urllib.request.urlopen(
                        f"http://localhost:{termora_port}/api/info", timeout=4
                    ) as r:
                        return _json.loads(r.read())
                return await loop.run_in_executor(None, _get)
            except Exception:
                return None

        info = await _fetch_info()

        if not info:
            # Auto-restart — no asking
            await update.message.reply_text("🔄 Termora está caída, reiniciando…", parse_mode="HTML")
            proc = await _asyncio.create_subprocess_shell(
                "launchctl kickstart -k gui/$(id -u)/com.termora.agent 2>/dev/null || "
                "(cd /Users/oxyzen/Projects/termora && /opt/homebrew/bin/npm run dev &)",
            )
            await _asyncio.sleep(5)
            info = await _fetch_info()

        if not info:
            await update.message.reply_text(
                "❌ Termora no arranca. Revisa <code>~/Projects/termora/termora.err.log</code>",
                parse_mode="HTML",
            )
            return

        auth_url = info.get("authUrl") or info.get("tunnelUrl")
        tunnel_method = info.get("tunnelMethod", "local")
        machine = info.get("machineName", "?")

        if auth_url:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    text=f"⚡ Abrir Terminal ({tunnel_method} · {machine})",
                    url=auth_url,
                )
            ]])
            await update.message.reply_text(
                "🖥️ <b>Termora</b> listo",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(
                f"⚠️ Sin tunnel — local: <code>http://localhost:{termora_port}</code>",
                parse_mode="HTML",
            )

    async def _zt_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show last Claude Code session context."""
        ctx_file = str(Path.home() / ".aura" / "context" / "latest.json")
        try:
            with open(ctx_file, "r") as f:
                ctx = _json.load(f)
            ts = ctx.get("timestamp", "?")
            model = ctx.get("model", "?")
            cwd = ctx.get("working_directory", "?")
            summary = ctx.get("summary", "No summary")
            ctx_type = ctx.get("type", "?")

            text = (
                f"📋 <b>Last session</b> ({ctx_type})\n"
                f"🕐 {ts}\n"
                f"🧠 {model}\n"
                f"📂 <code>{cwd}</code>\n\n"
                f"{self._escape_html(summary)}"
            )
            await update.message.reply_text(text, parse_mode="HTML")
        except FileNotFoundError:
            await update.message.reply_text("No context saved yet.")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _zt_sh(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Run arbitrary shell command."""
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: <code>/sh command</code>", parse_mode="HTML"
            )
            return
        await self._bash_passthrough(update, args[1])

    async def _zt_email(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Send email via Resend. Usage: /email to@x.com | Subject | Body"""
        import os
        args = (update.message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text(
                "Uso: <code>/email to@x.com | Asunto | Cuerpo</code>\n"
                "Ejemplo: <code>/email yo@gmail.com | Hola | Mensaje aquí</code>",
                parse_mode="HTML",
            )
            return

        parts = args[1].split("|", 2)
        if len(parts) < 3:
            await update.message.reply_text(
                "Formato: <code>to@x.com | Asunto | Cuerpo</code>",
                parse_mode="HTML",
            )
            return

        to, subject, body = [p.strip() for p in parts]

        # Inject key from .env if not in environ
        env_file = Path.home() / "claude-code-telegram" / ".env"
        if not os.environ.get("RESEND_API_KEY") and env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("RESEND_API_KEY="):
                    os.environ["RESEND_API_KEY"] = line.split("=", 1)[1].strip()
                if line.startswith("RESEND_FROM="):
                    os.environ["RESEND_FROM"] = line.split("=", 1)[1].strip()

        from ...workflows.email_sender import send_email
        await update.message.reply_text(f"📧 Enviando a {to}...")
        result = await send_email(to=to, subject=subject, body=body)

        if result["ok"]:
            await update.message.reply_text(
                f"✅ Email enviado\nID: <code>{result['id']}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ Error: {self._escape_html(result['error'])}",
                parse_mode="HTML",
            )

    # --- Workspace commands ---

    async def _zt_inbox(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show recent unread emails (zero-token via google-workspace-mcp)."""
        import subprocess

        try:
            result = subprocess.run(
                ["npx", "google-workspace-mcp", "status"],
                capture_output=True, text=True, timeout=10,
                cwd=str(Path.home()),
            )
            if "NOT found" in result.stdout or "No accounts" in result.stdout:
                await update.message.reply_text(
                    "📧 <b>Gmail not configured yet</b>\n\n"
                    "Setup needed:\n"
                    "1. Go to <a href='https://console.cloud.google.com'>Google Cloud Console</a>\n"
                    "2. Create project → Enable Gmail API\n"
                    "3. Create OAuth credentials → Download JSON\n"
                    "4. <code>mv ~/Downloads/credentials.json ~/.google-mcp/</code>\n"
                    "5. <code>npx google-workspace-mcp accounts add YOUR_ACCOUNT</code>\n\n"
                    "Once configured, /inbox will show your emails.",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return

            await update.message.reply_text(
                "📧 Gmail MCP configured. Use natural language:\n"
                '<i>"show my emails from today"</i>\n'
                '<i>"unread emails"</i>',
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"📧 Gmail check: {e}")

    async def _zt_calendar(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show today's calendar events."""
        import subprocess

        try:
            result = subprocess.run(
                ["npx", "google-workspace-mcp", "status"],
                capture_output=True, text=True, timeout=10,
                cwd=str(Path.home()),
            )
            if "NOT found" in result.stdout or "No accounts" in result.stdout:
                await update.message.reply_text(
                    "📅 <b>Calendar not configured yet</b>\n\n"
                    "Same setup as /inbox — once Gmail MCP is configured,\n"
                    "calendar access comes with it.\n\n"
                    "Use natural language once ready:\n"
                    '<i>"what do I have today"</i>',
                    parse_mode="HTML",
                )
                return

            await update.message.reply_text(
                "📅 Calendar MCP configured. Use natural language:\n"
                '<i>"what do I have on the calendar today"</i>\n'
                '<i>"tomorrow\'s agenda"</i>',
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"📅 Calendar check: {e}")

    async def _zt_limits(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Show rate limits and usage for all brains."""
        from ...infra.rate_monitor import RateMonitor

        monitor = context.bot_data.get("rate_monitor")
        if not monitor:
            monitor = RateMonitor()
        text = monitor.format_status()
        await update.message.reply_text(text, parse_mode="HTML")

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

    # --- Brain management ---

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
            valid = ["haiku", "sonnet", "opus", "codex", "opencode", "cline", "gemini", "openrouter"]
            if name == "auto":
                # Reset to smart routing
                if router:
                    router.reset_user_brain(user_id)
                await update.message.reply_text(
                    "🔄 Brain reset — routing automático activado", parse_mode="HTML"
                )
                return
            if name not in valid:
                await update.message.reply_text(
                    f"❌ Brain desconocido: <code>{name}</code>\n"
                    f"Válidos: {' · '.join(valid)} · auto",
                    parse_mode="HTML",
                )
                return
            if router:
                ok = router.set_active_brain(name, user_id)
                if ok:
                    emojis = {"haiku": "🟡", "sonnet": "🟠", "opus": "🔴",
                              "codex": "🟢", "opencode": "🔶", "cline": "🟣",
                              "gemini": "🔵", "openrouter": "🌐"}
                    emoji = emojis.get(name, "🧠")
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

        brain_lines = []
        for emoji, name, binary, cost, use in BRAINS:
            found = shutil.which(binary, path=full_path)
            st = "✅" if found else "❌"
            lock = " ◀ activo" if name == active else ""
            brain_lines.append(f"  {emoji} <b>{name}</b>: {st} {cost}{lock}")
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
        # We delegate back to the orchestrator's handler
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

    # --- Dashboard ---

    async def _zt_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ AURA Dashboard — local URL + Termora tunnel."""
        lines = ["<b>📊 AURA Dashboard</b>\n"]
        lines.append("📍 Local: <code>http://localhost:8080</code>")

        # Try to get Termora tunnel URL for remote access
        try:
            import json as _json
            import urllib.request as _req

            with _req.urlopen("http://localhost:4030/api/info", timeout=3) as r:
                info = _json.loads(r.read())
            tunnel = info.get("tunnelUrl", "")
            if tunnel:
                auth_url = info.get("authUrl", tunnel)
                lines.append(f"🌐 Remote: {auth_url}")
            else:
                lines.append("🌐 Remote: Termora not running")
        except Exception:
            lines.append("🌐 Remote: start Termora para acceso externo")

        lines.append(
            "\n<b>Secciones:</b> Overview · Brains · Logs · Commands · Tools · Crons · MCP"
        )
        lines.append("⚙️ API: <code>localhost:8080/api/status</code>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # --- Voice ---

    async def _zt_speak(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎤 /speak <text> — convierte texto a voz (edge-tts, gratis)."""
        text = (update.message.text or "").replace("/speak", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Uso: /speak <texto>\nEjemplo: /speak Todo listo, jefe."
            )
            return
        try:
            from ..features.voice_tts import generate_voice, send_voice_response
            sent = await send_voice_response(update, context, text)
            if not sent:
                await update.message.reply_text("❌ TTS no disponible — instala edge-tts")
        except Exception as e:
            await update.message.reply_text(f"TTS error: {e}")

    async def _zt_voz(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎙 /voz [on|off] — toggle respuestas de voz automáticas."""
        user_id = update.effective_user.id
        arg = (update.message.text or "").split()[-1].lower()
        voice_users = context.bot_data.setdefault("voice_users", set())

        if arg == "on":
            voice_users.add(user_id)
            await update.message.reply_text(
                "🎙 Voz activada — responderé con audio además de texto.\n"
                "Usa /voz off para desactivar."
            )
        elif arg == "off":
            voice_users.discard(user_id)
            await update.message.reply_text("🔇 Voz desactivada.")
        else:
            estado = "🎙 ON" if user_id in voice_users else "🔇 OFF"
            await update.message.reply_text(
                f"Voz: {estado}\nUsa /voz on o /voz off"
            )

    # --- Workflow commands ---

    async def _zt_standup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Daily standup — git activity, pending, system health."""
        from ...workflows.daily_standup import generate_standup

        await update.message.reply_text("⏳ Generating standup...")
        try:
            report = await generate_standup()
            await update.message.reply_text(report, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _zt_report(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Weekly report — code, brains, cache, system."""
        from ...workflows.weekly_report import generate_weekly_report

        await update.message.reply_text("⏳ Generating weekly report...")
        try:
            report = await generate_weekly_report()
            await update.message.reply_text(report, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _zt_triage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Email triage — classify inbox by priority."""
        from ...workflows.email_triage import generate_triage

        try:
            report = await generate_triage()
            await update.message.reply_text(report, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _zt_followup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Client follow-up — unanswered emails > 48h."""
        from ...workflows.client_followup import generate_followup

        try:
            report = await generate_followup()
            await update.message.reply_text(report, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    # ── NEW COMMANDS ──────────────────────────────────────────────────────────

    async def _zt_diagnose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Run full system diagnostic via self-healer."""
        msg = await update.message.reply_text("🔍 Running diagnostics…")
        try:
            from datetime import datetime

            from ...infra.self_healer import run_diagnostics

            report = await run_diagnostics()
            ts = datetime.fromtimestamp(report.checked_at).strftime("%Y-%m-%d %H:%M")
            icon = "✅" if report.ok and not report.warnings else "⚠️" if not report.issues else "🔴"
            lines = [f"<b>🩺 AURA Diagnostics</b> — {ts}\n{icon} <b>{'OK' if report.ok else 'ISSUES'}</b>"]

            if report.issues:
                lines.append(f"\n<b>Problemas ({len(report.issues)}):</b>")
                for issue in report.issues:
                    lines.append(f"  • {issue}")

            if report.fixes_applied:
                lines.append(f"\n<b>Auto-fixed ({len(report.fixes_applied)}):</b>")
                for fix in report.fixes_applied:
                    lines.append(f"  ✔ {fix}")

            if report.warnings:
                lines.append(f"\n<b>Advertencias ({len(report.warnings)}):</b>")
                for w in report.warnings[:5]:
                    lines.append(f"  ⚠ {w}")

            if not report.issues and not report.warnings:
                lines.append("\n✅ Todos los sistemas nominales")

            await msg.edit_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"❌ Diagnose error: {e}")

    async def _zt_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Concise command reference."""
        text = (
            "<b>🤖 AURA — Comandos</b>\n\n"
            "<b>Core</b>\n"
            "  /new — nueva sesión\n"
            "  /status — estado completo\n"
            "  /health — salud del sistema\n"
            "  /diagnose — diagnóstico automático\n\n"
            "<b>Brains &amp; Memoria</b>\n"
            "  /brain [nombre|auto] — ver/cambiar brain\n"
            "  /limits — uso de rate limits\n"
            "  /memory — hechos aprendidos\n"
            "  /memory add &lt;fact&gt; — agregar hecho\n\n"
            "<b>Shell &amp; Dev</b>\n"
            "  /sh &lt;cmd&gt; — shell directo\n"
            "  /git [subcmd] — git operations\n"
            "  /repo [nombre] — cambiar proyecto\n"
            "  <code>!cmd</code> o <code>$cmd</code> — shell rápido\n\n"
            "<b>Web</b>\n"
            "  /web &lt;url&gt; — analizar URL\n"
            "  /search &lt;query&gt; — búsqueda web\n\n"
            "<b>Comunicación</b>\n"
            "  /email to | asunto | cuerpo\n"
            "  /standup — daily standup\n"
            "  /report — weekly report\n\n"
            "<b>Herramientas</b>\n"
            "  /terminal — abrir Termora\n"
            "  /dashboard — URL del dashboard\n"
            "  /restart — reiniciar bot\n\n"
            "💡 Escribe libremente — AURA entiende lenguaje natural"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _zt_status_full(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Compact dashboard — brain, routing, limits, memory, disk."""
        import shutil

        router = context.bot_data.get("brain_router")
        rate_monitor = context.bot_data.get("rate_monitor")
        user_id = update.effective_user.id

        # Brain
        brain_name = router.get_active_brain_name(user_id) if router else "?"
        brain_emojis = {
            "haiku": "🟡", "sonnet": "🟠", "opus": "🔴",
            "gemini": "🔵", "openrouter": "🌐", "cline": "🟣",
            "codex": "🟢", "opencode": "🔶",
        }
        brain_emoji = brain_emojis.get(brain_name, "🧠")
        brain_is_auto = user_id not in (router._user_brains if router else {})
        brain_line = f"{brain_emoji} Brain: <b>{brain_name}</b>" + (" (auto)" if brain_is_auto else " (locked)")

        # Rate limits summary
        limit_lines = []
        if rate_monitor:
            for u in rate_monitor.get_all_usage():
                if u.requests_in_window > 0 or u.is_rate_limited:
                    icon = "⏱️" if u.is_rate_limited else "·"
                    limit_lines.append(f"  {icon} {u.brain_name}: {u.requests_in_window} req")

        # Memory
        mem_path = Path.home() / ".aura" / "brain" / "memory.md"
        mem_lines = 0
        if mem_path.exists():
            content = mem_path.read_text()
            mem_lines = sum(1 for l in content.splitlines() if l.strip().startswith("-"))

        # Disk
        usage = shutil.disk_usage(Path.home())
        disk_free_gb = usage.free / (1024 ** 3)

        # Compose
        lines = [
            "<b>📊 AURA Status</b>\n",
            brain_line,
            f"📂 Dir: <code>{context.user_data.get('current_directory', str(Path.home()))}</code>",
        ]
        if limit_lines:
            lines.append("\n<b>Rate limits (activos):</b>")
            lines.extend(limit_lines)
        lines.append(f"\n🧠 Memoria: {mem_lines} hechos aprendidos")
        lines.append(f"💾 Disco libre: {disk_free_gb:.1f}GB")
        lines.append("\n/brain · /limits · /memory · /health")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

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
        import asyncio as _asyncio  # noqa: F811 (local import OK in handler)
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

    # --- Social media pipeline ---

    async def _zt_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Social media content pipeline — generates images + captions → N8N.

        Usage:
          /post instagram carrusel 5 sobre claude code
          /post twitter hilo sobre ia y automatización
          /post linkedin post sobre productividad
          /post instagram 3 sobre diseño minimalista
        """
        args_text = (update.message.text or "").split(maxsplit=1)
        if len(args_text) < 2:
            await update.message.reply_text(
                "📱 <b>/post — Social Media Pipeline</b>\n\n"
                "Uso:\n"
                "  <code>/post instagram carrusel 5 sobre claude code</code>\n"
                "  <code>/post twitter hilo sobre IA y automatización</code>\n"
                "  <code>/post linkedin post sobre productividad</code>\n\n"
                "Plataformas: instagram · twitter · linkedin\n"
                "Tipos: carrusel/carousel · hilo/thread · post\n\n"
                "💡 También puedes escribir directamente:\n"
                '<i>"publica un carrusel en instagram sobre X, 5 fotos"</i>',
                parse_mode="HTML",
            )
            return

        raw_prompt = args_text[1].strip()
        # Delegate to the orchestrator's social pipeline handler
        await self._handle_social_post(update, context, raw_prompt)

    # --- Video generation ---

    async def _zt_video(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎬 Video generation — cinematic AI or structured slides.

        Usage:
          /video cinematic <prompt>     — Luma/Kling/Runway cinematic AI video
          /video slides <N> <topic>     — json2video structured slide video
          /video help                   — show options and configured providers
        """
        import os as _os

        args = (update.message.text or "").split(maxsplit=2)
        subcommand = args[1].lower() if len(args) > 1 else "help"

        if subcommand == "help" or len(args) < 2:
            luma_ok = "✅" if _os.environ.get("LUMA_API_KEY", "").strip() else "❌"
            kling_ok = "✅" if _os.environ.get("KLING_API_KEY", "").strip() else "❌"
            runway_ok = "✅" if _os.environ.get("RUNWAY_API_KEY", "").strip() else "❌"
            j2v_ok = "✅" if _os.environ.get("JSON2VIDEO_API_KEY", "").strip() else "❌"

            await update.message.reply_text(
                "🎬 <b>/video — Video Generation</b>\n\n"
                "<b>Modos:</b>\n"
                "  <code>/video cinematic &lt;prompt&gt;</code>\n"
                "    Kling/Luma cinematic AI video\n\n"
                "  <code>/video slides &lt;N&gt; &lt;topic&gt;</code>\n"
                "    json2video structured slides (e.g. /video slides 5 claude code)\n\n"
                "  <code>/video help</code> — este mensaje\n\n"
                "<b>Proveedores configurados:</b>\n"
                f"  {luma_ok} LUMA_API_KEY (Dream Machine)\n"
                f"  {kling_ok} KLING_API_KEY (Kling AI)\n"
                f"  {runway_ok} RUNWAY_API_KEY (Runway ML)\n"
                f"  {j2v_ok} JSON2VIDEO_API_KEY (slides)\n\n"
                "💡 También puedes escribir directamente:\n"
                '  <i>"crea un video de 10s de un developer usando AI"</i>\n'
                '  <i>"haz un video de 5 slides sobre automatización"</i>',
                parse_mode="HTML",
            )
            return

        router = context.bot_data.get("brain_router")

        if subcommand == "slides":
            # /video slides <N> <topic>  OR  /video slides <topic>
            rest = args[2] if len(args) > 2 else ""
            if not rest:
                await update.message.reply_text(
                    "Uso: <code>/video slides &lt;N&gt; &lt;topic&gt;</code>\n"
                    "Ejemplo: <code>/video slides 5 claude code</code>",
                    parse_mode="HTML",
                )
                return
            # Inject "slides" keyword so video_compose picks the right route
            synthetic_prompt = f"video de slides {rest}"
            await self._handle_video_gen(update, context, router, synthetic_prompt, update.effective_user.id)

        elif subcommand == "cinematic":
            rest = args[2] if len(args) > 2 else ""
            if not rest:
                await update.message.reply_text(
                    "Uso: <code>/video cinematic &lt;prompt&gt;</code>\n"
                    "Ejemplo: <code>/video cinematic developer coding at night, neon lights</code>",
                    parse_mode="HTML",
                )
                return
            await self._handle_video_gen(update, context, router, rest, update.effective_user.id)

        else:
            # Treat the whole thing as a cinematic prompt
            raw = " ".join(args[1:]).strip()
            await self._handle_video_gen(update, context, router, raw, update.effective_user.id)

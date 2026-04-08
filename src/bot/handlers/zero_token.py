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
        """⚡ Get Termora terminal — one-tap inline button."""
        import urllib.request
        import json as _json
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        termora_port = 4030
        try:
            req = urllib.request.Request(
                f"http://localhost:{termora_port}/api/info"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = _json.loads(resp.read())

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
                    "⚠️ Termora online pero sin tunnel activo.\n"
                    f"Local: <code>http://localhost:{termora_port}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            await update.message.reply_text(
                "❌ Termora no responde.\n"
                f"Iníciala: <code>cd ~/Projects/termora && npm run dev</code>",
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
        """⚡ Show all brains and routing status."""
        import shutil
        import os

        extra_path = "/opt/homebrew/bin:/usr/local/bin"
        full_path = f"{extra_path}:{os.environ.get('PATH', '')}"

        # Brain definitions with CLI binary checks
        BRAINS = [
            ("🟡", "haiku",     "claude",    "Max plan · ~$0",        "CHAT→CODE ligero"),
            ("🟠", "sonnet",    "claude",    "Max plan · ~$0",        "CODE · análisis"),
            ("🔴", "opus",      "claude",    "Max plan · ~$0",        "arquitectura deep"),
            ("🔶", "opencode",  "opencode",  "OpenRouter free",       "código via free tier"),
            ("🟣", "cline",     "cline",     "Ollama local · $0",     "código local sin internet"),
            ("🟢", "codex",     "codex",     "OpenAI suscripción",    "agente código OpenAI"),
            ("🔵", "gemini",    "gemini",    "Google free",           "chat · búsqueda · URLs"),
        ]

        ROUTING = [
            ("⚡", "BASH/GIT/FILES", "zero-token", "sin LLM"),
            ("🔵", "CHAT/SEARCH",   "gemini",     "HTTP directo"),
            ("🟡", "ANÁLISIS/DEEP", "haiku",      "CLI subprocess"),
            ("🟠", "CÓDIGO",        "sonnet",     "CLI subprocess"),
            ("🔶", "usa opencode",  "opencode",   "forzado"),
            ("🟣", "usa cline",     "cline",      "forzado"),
            ("🟢", "usa codex",     "codex",      "forzado"),
        ]

        brain_lines = []
        for emoji, name, binary, cost, use in BRAINS:
            found = shutil.which(binary, path=full_path)
            st = "✅" if found else "❌"
            brain_lines.append(f"  {emoji} <b>{name}</b>: {st} {cost}")
            brain_lines.append(f"      └ {use}")

        route_lines = []
        for emoji, trigger, target, how in ROUTING:
            route_lines.append(f"  {emoji} {trigger} → <b>{target}</b> ({how})")

        lines = [
            "<b>🧠 AURA — 7 Brains</b>\n",
            *brain_lines,
            "\n<b>Routing automático:</b>",
            *route_lines,
            "\n💡 <i>usa opencode/cline/codex para X</i> = forzar CLI",
            "💡 <i>/brains</i> = estado detallado en vivo",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

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
        """⚡ Get AURA Dashboard URL (local + tunnel)."""
        lines = ["<b>🖥️ AURA Dashboard</b>\n"]
        lines.append("📍 Local: <code>http://localhost:3000</code>")

        try:
            proc = await asyncio.create_subprocess_shell(
                f"grep trycloudflare "
                f"{Path(__file__).resolve().parent.parent.parent / 'logs' / 'dashboard-tunnel.stderr.log'} "
                "| tail -1 | grep -oE 'https://[^ |]+'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            tunnel_url = stdout.decode().strip()
            if tunnel_url:
                lines.append(f"🌐 Tunnel: {tunnel_url}")
            else:
                lines.append("🌐 Tunnel: not active")
        except Exception:
            lines.append("🌐 Tunnel: error getting URL")

        lines.append("\n📊 APIs: /api/health, /api/brains, /api/limits")
        lines.append("📄 Docs: /api/docs")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # --- Voice ---

    async def _zt_speak(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """🎤 Convert text to voice message (edge-tts, free)."""
        text = (update.message.text or "").replace("/speak", "", 1).strip()
        if not text:
            await update.message.reply_text(
                "Usage: /speak <text>\nExample: /speak Hello, everything is ready."
            )
            return

        try:
            from ...voice.tts import text_to_speech

            audio_bytes = await text_to_speech(text)
            await update.message.reply_voice(voice=audio_bytes)
        except Exception as e:
            await update.message.reply_text(f"TTS error: {e}")

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

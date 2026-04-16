"""Zero-token system commands — ls, pwd, git, health, terminal, context, sh, email."""

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import structlog
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenSystemMixin:
    """Mixin: system-level zero-token commands."""

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

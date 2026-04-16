"""Zero-token workspace commands — inbox, calendar, limits."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes
from pathlib import Path

logger = structlog.get_logger()


class ZeroTokenWorkspaceMixin:
    """Mixin: workspace/productivity zero-token commands."""

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

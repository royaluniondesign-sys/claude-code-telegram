"""Zero-token workflow commands — standup, report, triage, followup."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenWorkflowMixin:
    """Mixin: workflow/reporting zero-token commands."""

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

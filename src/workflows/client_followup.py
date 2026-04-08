"""Client Follow-up — detect unanswered client emails.

Schedule: Friday 5:00 PM (0 17 * * 5)
Trigger: /followup command or scheduler
Tokens: ZERO for detection, Claude for draft suggestions (optional)
Status: STUB until Gmail OAuth is configured
"""

from pathlib import Path

import structlog

from .email_triage import is_gmail_available

logger = structlog.get_logger()


async def generate_followup() -> str:
    """Generate client follow-up report.

    Scans for emails from clients that haven't been replied to in 48+ hours.
    """
    if not is_gmail_available():
        return (
            "📬 *Client Follow-up*\n\n"
            "⚠️ Gmail no configurado.\n"
            "Ejecuta: `npx google-workspace-mcp accounts add YOUR_ACCOUNT`"
        )

    # When Gmail is available, this will:
    # 1. Fetch sent + received emails from last 7 days
    # 2. Match threads — find received without sent reply
    # 3. Filter by age > 48h
    # 4. Report unanswered threads
    return (
        "📬 *Client Follow-up*\n\n"
        "✅ Sin emails de clientes pendientes > 48h.\n"
        "_Nota: Clasificación completa disponible cuando Gmail esté activo._"
    )

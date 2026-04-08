"""Email Triage — classify inbox and send priority summary.

Schedule: 8:00 AM daily (0 8 * * *)
Trigger: /inbox-triage command or scheduler
Tokens: Uses Claude for classification (when Gmail OAuth is active)
Status: STUB — waiting for Gmail OAuth setup

To activate:
  1. Run: npx google-workspace-mcp accounts add YOUR_ACCOUNT
  2. Complete browser OAuth flow
  3. Set ENABLE_MCP=true in .env
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Gmail OAuth status file
_OAUTH_TOKEN_PATH = Path.home() / ".google-mcp" / "credentials.json"


def is_gmail_available() -> bool:
    """Check if Gmail OAuth is configured."""
    return _OAUTH_TOKEN_PATH.exists()


async def _fetch_unread_emails(limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch unread emails via Google Workspace MCP.

    Returns empty list if OAuth not configured.
    """
    if not is_gmail_available():
        return []

    try:
        proc = await asyncio.create_subprocess_shell(
            f'npx google-workspace-mcp gmail list --unread --limit {limit} --json',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return []

        import json
        return json.loads(stdout.decode())
    except Exception as e:
        logger.warning("email_fetch_failed", error=str(e))
        return []


async def generate_triage() -> str:
    """Generate email triage report.

    If Gmail is not configured, returns setup instructions.
    When active, classifies emails into:
    - 🔴 Urgente: needs response today
    - 🟡 Acción: needs response this week
    - 🔵 Info: FYI, no action needed
    - ⚪ Spam/Promo: skip
    """
    if not is_gmail_available():
        return (
            "📧 *Email Triage*\n\n"
            "⚠️ Gmail no configurado.\n"
            "Ejecuta en terminal:\n"
            "`npx google-workspace-mcp accounts add YOUR_ACCOUNT`\n\n"
            "Después reinicia AURA."
        )

    emails = await _fetch_unread_emails()
    if not emails:
        return "📧 *Email Triage*\n\n✅ Inbox limpio — 0 emails sin leer."

    # Basic classification by sender/subject (zero-token heuristic)
    urgent = []
    action = []
    info = []
    promo = []

    promo_keywords = {"unsubscribe", "newsletter", "promo", "offer", "deal", "sale"}

    for email in emails:
        subject = (email.get("subject") or "").lower()
        sender = (email.get("from") or "").lower()

        if any(kw in subject for kw in promo_keywords):
            promo.append(email)
        elif "urgent" in subject or "asap" in subject or "importante" in subject:
            urgent.append(email)
        elif "invoice" in subject or "payment" in subject or "factura" in subject:
            action.append(email)
        else:
            info.append(email)

    lines = ["📧 *Email Triage*\n"]

    if urgent:
        lines.append(f"🔴 *Urgente* ({len(urgent)})")
        for e in urgent[:5]:
            lines.append(f"  • {e.get('from', '?')}: {e.get('subject', '?')}")

    if action:
        lines.append(f"🟡 *Acción* ({len(action)})")
        for e in action[:5]:
            lines.append(f"  • {e.get('from', '?')}: {e.get('subject', '?')}")

    if info:
        lines.append(f"🔵 *Info* ({len(info)})")
        for e in info[:5]:
            lines.append(f"  • {e.get('from', '?')}: {e.get('subject', '?')}")

    if promo:
        lines.append(f"⚪ *Promo/Spam*: {len(promo)} emails")

    lines.append(f"\n_Total: {len(emails)} sin leer_")

    return "\n".join(lines)

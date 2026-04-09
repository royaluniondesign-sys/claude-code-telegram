"""Email tools — send and report via Resend API."""
from __future__ import annotations
import os
from src.actions.registry import aura_tool


@aura_tool(
    name="send_email",
    description="Send an email via Resend API. From: onboarding@resend.dev → royaluniondesign@gmail.com",
    category="email",
    parameters={
        "to":      {"type": "str", "description": "Recipient email address"},
        "subject": {"type": "str", "description": "Email subject line"},
        "body":    {"type": "str", "description": "Plain-text email body"},
        "html":    {"type": "str", "description": "HTML body (optional, overrides body)"},
    },
)
async def send_email(to: str, subject: str, body: str, html: str = "") -> str:
    from src.workflows.email_sender import send_email as _send
    result = await _send(to=to, subject=subject, body=body, html=html or None)
    if result.get("ok"):
        return f"✅ Email enviado a {to} — ID: {result.get('id', '?')}"
    return f"❌ Error enviando email: {result.get('error', 'unknown')}"

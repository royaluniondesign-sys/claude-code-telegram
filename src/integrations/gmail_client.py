"""Gmail client — read, search, send, reply via Google API.

Auth: uses ~/.aura/google_credentials.json (must include Gmail scopes).
Setup: run /gmail-auth in Telegram → click link → done.
"""

from __future__ import annotations

import base64
import email as email_lib
import json
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_CREDS_PATH = Path.home() / ".aura" / "google_credentials.json"
_TOKEN_PATH = Path.home() / ".aura" / "google_oauth_token.json"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

RUD_EMAIL = os.environ.get("RUD_EMAIL", "royaluniondesign@gmail.com")
RUD_NAME = os.environ.get("RUD_NAME", "RUD Studio")


def is_configured() -> bool:
    return _TOKEN_PATH.exists() or (
        _CREDS_PATH.exists()
        and json.loads(_CREDS_PATH.read_text()).get("type") == "authorized_user"
    )


def _get_service():
    """Build authenticated Gmail service. Raises if not configured."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None

    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), GMAIL_SCOPES)
    elif _CREDS_PATH.exists():
        data = json.loads(_CREDS_PATH.read_text())
        if data.get("type") == "authorized_user":
            creds = Credentials.from_authorized_user_info(data, GMAIL_SCOPES)

    if not creds:
        raise RuntimeError("Gmail no configurado. Envía /gmail-auth en Telegram.")

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _save_token(creds) -> None:
    """Persist refreshed credentials."""
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(creds.to_json())


def _parse_message(msg: dict) -> dict:
    """Extract clean fields from a Gmail message dict."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    snippet = msg.get("snippet", "")

    # Extract plain text body
    body = ""
    payload = msg.get("payload", {})

    def _extract_body(part: dict) -> str:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if "parts" in part:
            for subpart in part["parts"]:
                result = _extract_body(subpart)
                if result:
                    return result
        return ""

    body = _extract_body(payload) or snippet

    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", "(sin asunto)"),
        "date": headers.get("date", ""),
        "snippet": snippet,
        "body": body[:3000],  # cap at 3k chars
        "labels": msg.get("labelIds", []),
    }


async def list_unread(max_results: int = 10, query: str = "") -> list[dict]:
    """List unread emails. Optionally filter with Gmail query syntax."""
    import asyncio

    def _run():
        service = _get_service()
        q = "is:unread"
        if query:
            q = f"{q} {query}"
        result = service.users().messages().list(
            userId="me", q=q, maxResults=max_results
        ).execute()

        messages = []
        for m in result.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()
            messages.append(_parse_message(full))
        return messages

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def get_message(message_id: str) -> dict:
    """Get a specific message by ID."""
    import asyncio

    def _run():
        service = _get_service()
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        return _parse_message(msg)

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def search(query: str, max_results: int = 10) -> list[dict]:
    """Search Gmail with query. Supports Gmail search syntax."""
    import asyncio

    def _run():
        service = _get_service()
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        messages = []
        for m in result.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()
            messages.append(_parse_message(full))
        return messages

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def send(
    to: str,
    subject: str,
    body: str,
    html: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
    reply_to_thread_id: Optional[str] = None,
) -> dict:
    """Send email FROM royaluniondesign@gmail.com.

    Returns {"ok": True, "id": "..."} or {"ok": False, "error": "..."}.
    """
    import asyncio

    def _run():
        service = _get_service()

        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain"))
            msg.attach(MIMEText(html, "html"))
        else:
            msg = MIMEText(body, "plain")

        msg["From"] = f"{RUD_NAME} <{RUD_EMAIL}>"
        msg["To"] = to
        msg["Subject"] = subject

        # If replying, set thread headers
        if reply_to_message_id:
            msg["In-Reply-To"] = reply_to_message_id
            msg["References"] = reply_to_message_id

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_data: dict[str, Any] = {"raw": raw}
        if reply_to_thread_id:
            body_data["threadId"] = reply_to_thread_id

        result = service.users().messages().send(
            userId="me", body=body_data
        ).execute()
        return {"ok": True, "id": result.get("id", "?")}

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as e:
        logger.error("gmail_send_failed", error=str(e))
        return {"ok": False, "error": str(e)}


async def mark_read(message_id: str) -> bool:
    """Mark a message as read."""
    import asyncio

    def _run():
        service = _get_service()
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return True

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception:
        return False


async def start_oauth_flow() -> str:
    """Start Gmail OAuth flow. Returns URL for user to visit."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    import asyncio

    # Check for existing OAuth app credentials
    app_creds_path = Path.home() / ".aura" / "gmail_app_credentials.json"
    if not app_creds_path.exists():
        return (
            "⚙️ *Para activar Gmail necesito las credenciales de tu app de Google.*\n\n"
            "Pasos (5 min):\n"
            "1. Ve a https://console.cloud.google.com\n"
            "2. Crea un proyecto (o usa uno existente)\n"
            "3. APIs & Services → Enable → *Gmail API*\n"
            "4. Credentials → Create → *OAuth 2.0 Client ID* → Desktop App\n"
            "5. Download JSON → renómbralo `gmail_app_credentials.json`\n"
            "6. Envíame el contenido del JSON aquí y lo instalo yo"
        )

    def _run_flow():
        flow = InstalledAppFlow.from_client_secrets_file(
            str(app_creds_path), GMAIL_SCOPES
        )
        # Use console flow (no browser on server)
        flow.run_local_server(port=8902, open_browser=False)
        creds = flow.credentials
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(creds.to_json())
        return "✅ Gmail configurado correctamente."

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _run_flow)
    except Exception as e:
        return f"❌ Error en OAuth: {e}"

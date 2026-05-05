"""IONOS email client — IMAP read + SMTP send for hello@royaluniondesign.com.

No third-party services. Direct IONOS connection.
Credentials: IONOS_EMAIL_USER + IONOS_EMAIL_PASS in .env

IMAP: imap.ionos.es:993 (SSL)
SMTP: smtp.ionos.es:587 (STARTTLS)
"""

from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import os
import smtplib
import ssl
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import structlog

logger = structlog.get_logger()

IMAP_HOST = "imap.ionos.es"
IMAP_PORT = 993
SMTP_HOST = "smtp.ionos.es"
SMTP_PORT = 587

RUD_EMAIL = os.environ.get("RUD_EMAIL", "hello@royaluniondesign.com")
RUD_NAME = os.environ.get("RUD_NAME", "RUD Studio")
_EMAIL_USER = os.environ.get("IONOS_EMAIL_USER", RUD_EMAIL)
_EMAIL_PASS = os.environ.get("IONOS_EMAIL_PASS", "")


def is_configured() -> bool:
    return bool(_EMAIL_PASS or os.environ.get("IONOS_EMAIL_PASS"))


def _get_pass() -> str:
    return os.environ.get("IONOS_EMAIL_PASS", _EMAIL_PASS)


def _decode_header_val(val: str) -> str:
    parts = decode_header(val)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def _parse_imap_message(msg_bytes: bytes) -> dict:
    msg = email_lib.message_from_bytes(msg_bytes)
    body = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain" and not part.get("Content-Disposition"):
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload:
                body = payload.decode(charset, errors="replace")[:3000]
                break
    if not body:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    body = f"[HTML] {payload.decode(charset, errors='replace')[:2000]}"
                    break

    return {
        "from": _decode_header_val(msg.get("From", "")),
        "to": _decode_header_val(msg.get("To", "")),
        "subject": _decode_header_val(msg.get("Subject", "(sin asunto)")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "body": body,
        "snippet": body[:150].replace("\n", " "),
    }


async def list_unread(max_results: int = 10, folder: str = "INBOX") -> list[dict]:
    def _run():
        pw = _get_pass()
        if not pw:
            raise RuntimeError("IONOS_EMAIL_PASS no configurado en .env")
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(_EMAIL_USER, pw)
            imap.select(folder)
            _, ids = imap.search(None, "UNSEEN")
            msg_ids = ids[0].split()
            if not msg_ids:
                return []
            # Take last N
            selected = msg_ids[-max_results:]
            messages = []
            for mid in reversed(selected):
                _, data = imap.fetch(mid, "(RFC822)")
                if data and data[0]:
                    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                    parsed = _parse_imap_message(raw)
                    parsed["uid"] = mid.decode()
                    messages.append(parsed)
            return messages

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def get_message(uid: str, folder: str = "INBOX") -> dict:
    def _run():
        pw = _get_pass()
        if not pw:
            raise RuntimeError("IONOS_EMAIL_PASS no configurado en .env")
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(_EMAIL_USER, pw)
            imap.select(folder)
            _, data = imap.fetch(uid.encode(), "(RFC822)")
            if data and data[0]:
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                parsed = _parse_imap_message(raw)
                parsed["uid"] = uid
                return parsed
        raise RuntimeError(f"Mensaje {uid} no encontrado")

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def search(query: str, max_results: int = 10, folder: str = "INBOX") -> list[dict]:
    """Search emails. query supports: FROM, SUBJECT, BODY, SINCE, BEFORE."""
    def _run():
        pw = _get_pass()
        if not pw:
            raise RuntimeError("IONOS_EMAIL_PASS no configurado en .env")
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(_EMAIL_USER, pw)
            imap.select(folder)
            # Build IMAP search criteria from simple query
            criteria = "ALL"
            q = query.strip().upper()
            if q.startswith("FROM:"):
                criteria = f'FROM "{query[5:].strip()}"'
            elif q.startswith("SUBJECT:"):
                criteria = f'SUBJECT "{query[8:].strip()}"'
            elif q.startswith("BODY:"):
                criteria = f'BODY "{query[5:].strip()}"'
            else:
                criteria = f'TEXT "{query}"'

            _, ids = imap.search(None, criteria)
            msg_ids = ids[0].split()
            if not msg_ids:
                return []
            selected = msg_ids[-max_results:]
            messages = []
            for mid in reversed(selected):
                _, data = imap.fetch(mid, "(RFC822)")
                if data and data[0]:
                    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                    parsed = _parse_imap_message(raw)
                    parsed["uid"] = mid.decode()
                    messages.append(parsed)
            return messages

    return await asyncio.get_event_loop().run_in_executor(None, _run)


async def send(
    to: str,
    subject: str,
    body: str,
    html: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> dict:
    """Send email FROM hello@royaluniondesign.com via IONOS SMTP."""
    def _run():
        pw = _get_pass()
        if not pw:
            raise RuntimeError("IONOS_EMAIL_PASS no configurado en .env")

        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")

        msg["From"] = f"{RUD_NAME} <{_EMAIL_USER}>"
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to_message_id:
            msg["In-Reply-To"] = reply_to_message_id
            msg["References"] = reply_to_message_id

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.login(_EMAIL_USER, pw)
            smtp.sendmail(_EMAIL_USER, [to], msg.as_bytes())

        return {"ok": True}

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as e:
        logger.error("ionos_send_failed", error=str(e))
        return {"ok": False, "error": str(e)}


async def mark_read(uid: str, folder: str = "INBOX") -> bool:
    def _run():
        pw = _get_pass()
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(_EMAIL_USER, pw)
            imap.select(folder)
            imap.store(uid.encode(), "+FLAGS", "\\Seen")
        return True
    try:
        return await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception:
        return False

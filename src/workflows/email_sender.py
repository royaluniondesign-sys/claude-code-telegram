"""Email sender via Resend API.

Usage from bot:
    from src.workflows.email_sender import send_email
    result = await send_email(to="x@gmail.com", subject="...", body="...")
"""

import os
import urllib.request
import urllib.error
import json
from typing import Optional


RESEND_API_URL = "https://api.resend.com/emails"
_API_KEY = os.environ.get("RESEND_API_KEY", "")
_FROM = os.environ.get("RESEND_FROM", "AURA <onboarding@resend.dev>")

# Without a verified domain, Resend only allows sending to the account owner.
# Set RESEND_VERIFIED_DOMAIN=true once a domain is verified.
_DOMAIN_VERIFIED = os.environ.get("RESEND_VERIFIED_DOMAIN", "").lower() == "true"
_ACCOUNT_EMAIL = "royaluniondesign@gmail.com"


def _reload_key() -> str:
    """Re-read key from env (may be set after module import)."""
    return os.environ.get("RESEND_API_KEY", _API_KEY)


async def send_email(
    to: str,
    subject: str,
    body: str,
    html: Optional[str] = None,
) -> dict:
    """Send email via Resend.

    Returns {"ok": True, "id": "..."} or {"ok": False, "error": "..."}.

    Note: Without a verified domain, `to` must be the Resend account email
    (royaluniondesign@gmail.com). Verify a domain at resend.com/domains to
    send to any address.
    """
    import asyncio

    api_key = _reload_key()
    if not api_key:
        return {"ok": False, "error": "RESEND_API_KEY not set"}

    # Warn if recipient is not account owner and domain not verified
    if not _DOMAIN_VERIFIED and to.lower() != _ACCOUNT_EMAIL:
        return {
            "ok": False,
            "error": (
                f"Sin dominio verificado, Resend solo puede enviar a {_ACCOUNT_EMAIL}. "
                "Verifica un dominio en resend.com/domains para enviar a cualquier dirección."
            ),
        }

    payload = {
        "from": _FROM,
        "to": [to],
        "subject": subject,
        **({"html": html} if html else {"text": body}),
    }

    def _send() -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            RESEND_API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AURA/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                return {"ok": True, "id": result.get("id", "?")}
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            try:
                msg = json.loads(body_err).get("message", body_err)
            except Exception:
                msg = body_err
            return {"ok": False, "error": msg}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return await asyncio.get_event_loop().run_in_executor(None, _send)

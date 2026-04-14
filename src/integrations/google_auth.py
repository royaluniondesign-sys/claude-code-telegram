"""Google OAuth setup for Drive + Sheets access.

Two auth methods supported:
1. Service Account JSON — drop file at ~/.aura/google_credentials.json
2. OAuth User Flow   — run /drive-auth in Telegram → click link → done

OAuth flow uses a local HTTP server on port 8901 to capture the callback.
"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_CREDS_PATH = Path.home() / ".aura" / "google_credentials.json"
_TOKEN_PATH = Path.home() / ".aura" / "google_oauth_token.json"

# Scopes needed
_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Google OAuth endpoints
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Default OAuth client for installed apps (public client — safe to embed)
# These are the Gemini CLI's client credentials extracted from the npm package
_DEFAULT_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
_DEFAULT_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

_OAUTH_PORT = 8901


def is_configured() -> bool:
    """Return True if Google credentials are available."""
    return _CREDS_PATH.exists() or _TOKEN_PATH.exists()


def get_credentials_info() -> dict:
    """Return info about configured credentials."""
    if _CREDS_PATH.exists():
        try:
            data = json.loads(_CREDS_PATH.read_text())
            return {
                "type": data.get("type", "unknown"),
                "email": data.get("client_email", data.get("email", "?")),
                "configured": True,
            }
        except Exception:
            pass
    if _TOKEN_PATH.exists():
        return {"type": "oauth_user", "configured": True}
    return {"configured": False}


async def start_oauth_flow(
    client_id: str,
    client_secret: str,
    redirect_port: int = _OAUTH_PORT,
) -> Optional[str]:
    """Start OAuth flow. Returns authorization URL to show user.

    After user visits the URL and authorizes, the callback lands on
    http://localhost:{redirect_port}/callback and credentials are saved.
    """
    from urllib.parse import urlencode

    redirect_uri = f"http://localhost:{redirect_port}/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{_AUTH_URI}?{urlencode(params)}"

    # Start local server to catch callback
    asyncio.create_task(_run_oauth_callback_server(
        client_id, client_secret, redirect_uri, redirect_port
    ))

    return auth_url


async def _run_oauth_callback_server(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    port: int,
) -> None:
    """Run a minimal HTTP server to capture the OAuth callback code."""
    import aiohttp
    from aiohttp import web

    auth_code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def handle_callback(request: web.Request) -> web.Response:
        code = request.query.get("code", "")
        error = request.query.get("error", "")
        if error:
            if not auth_code_future.done():
                auth_code_future.set_exception(RuntimeError(f"OAuth error: {error}"))
            return web.Response(text="❌ Authorization failed. Close this window.")
        if code:
            if not auth_code_future.done():
                auth_code_future.set_result(code)
            return web.Response(
                text="✅ Autorización exitosa! AURA ya tiene acceso a Drive y Sheets. Cierra esta ventana.",
                content_type="text/html",
            )
        return web.Response(text="Waiting...")

    app = web.Application()
    app.router.add_get("/callback", handle_callback)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    logger.info("oauth_callback_server_started", port=port)

    try:
        code = await asyncio.wait_for(auth_code_future, timeout=300)  # 5 min timeout
        await _exchange_code_for_token(client_id, client_secret, redirect_uri, code)
        logger.info("oauth_flow_completed")
    except asyncio.TimeoutError:
        logger.warning("oauth_flow_timeout")
    except Exception as e:
        logger.error("oauth_flow_error", error=str(e))
    finally:
        await runner.cleanup()


async def _exchange_code_for_token(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> None:
    """Exchange auth code for access + refresh tokens. Saves to _CREDS_PATH."""
    import aiohttp

    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(_TOKEN_URI, data=data) as resp:
            token_data = await resp.json()

    if "error" in token_data:
        raise RuntimeError(f"Token exchange failed: {token_data}")

    # Save as google_credentials.json in OAuth format
    creds = {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token_data.get("refresh_token", ""),
        "token": token_data.get("access_token", ""),
        "token_uri": _TOKEN_URI,
        "scopes": _SCOPES,
    }
    _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CREDS_PATH.write_text(json.dumps(creds, indent=2))
    logger.info("google_credentials_saved", path=str(_CREDS_PATH))


def save_service_account_credentials(service_account_json: str) -> bool:
    """Save service account credentials from JSON string. Returns True on success."""
    try:
        data = json.loads(service_account_json)
        if data.get("type") != "service_account":
            raise ValueError("Not a service account JSON")
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CREDS_PATH.write_text(json.dumps(data, indent=2))
        logger.info("service_account_saved")
        return True
    except Exception as e:
        logger.error("service_account_save_failed", error=str(e))
        return False


def get_setup_instructions() -> str:
    """Return step-by-step setup instructions."""
    return """🔧 *Google Drive/Sheets Setup*

*Opción A — Service Account (recomendado):*
1. console.cloud.google.com → Nuevo proyecto: "AURA"
2. APIs → Enable: Google Drive API + Google Sheets API
3. IAM → Service Accounts → Create → Descargar JSON
4. Compárteme el JSON en Telegram y lo guardo yo

*Opción B — OAuth (tu cuenta personal):*
1. console.cloud.google.com → Nuevo proyecto: "AURA"
2. APIs → Enable: Google Drive API + Google Sheets API
3. Credentials → OAuth 2.0 → Desktop App → Descargar client_secrets.json
4. Envíame el `client_id` y `client_secret` del JSON
5. Yo te genero el link de autorización

*Una vez configurado:*
✅ Cada post guardado en Drive (carpetas por fecha)
✅ Base de datos en Google Sheets (actualizada automáticamente)
✅ Videos, imágenes, metadata organizados"""

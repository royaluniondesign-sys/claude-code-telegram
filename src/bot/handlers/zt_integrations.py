"""Zero-token integration commands — ig_auth, posts, drive."""

import json as _json
import os as _os
from pathlib import Path

import structlog
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenIntegrationsMixin:
    """Mixin: OAuth and third-party integration zero-token commands."""

    async def _zt_ig_auth(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/ig-auth <app_secret> — start Instagram OAuth via Instagram Login."""
        from pathlib import Path as _Path

        args = (update.message.text or "").split(maxsplit=2)
        app_secret = args[1].strip() if len(args) > 1 else ""

        if not app_secret:
            token_path = _Path.home() / ".aura" / "instagram_token.json"
            if token_path.exists():
                info = _json.loads(token_path.read_text())
                created = info.get("created_at", "")[:10]
                refreshed = info.get("refreshed_at", created)[:10]
                days = info.get("expires_in", 0) // 86400
                uid = info.get("user_id", "?")
                await update.message.reply_text(
                    f"✅ <b>Instagram conectado</b> (Instagram Login)\n"
                    f"User ID: <code>{uid}</code>\n"
                    f"Creado: {created} · Último refresh: {refreshed}\n"
                    f"Token válido ~{days} días desde creación\n\n"
                    f"Reconectar: <code>/ig-auth &lt;app_secret&gt;</code>",
                    parse_mode="HTML",
                )
            else:
                api_port = _os.environ.get("API_SERVER_PORT", "8080")
                await update.message.reply_text(
                    "📱 <b>Instagram OAuth Setup</b>\n\n"
                    "<b>1.</b> Meta App Dashboard → AURA app → Instagram\n"
                    "   → Instagram Login → OAuth Redirect URIs:\n"
                    f"   <code>http://localhost:{api_port}/auth/instagram/callback</code>\n\n"
                    "<b>2.</b> Copia el <b>App Secret</b> (Configuración básica)\n\n"
                    "<b>3.</b> Envía: <code>/ig-auth TU_APP_SECRET</code>",
                    parse_mode="HTML",
                )
            return

        # Store secret + start flow
        app_id = _os.environ.get("META_APP_ID", "1534047588298769")
        api_port = int(_os.environ.get("API_SERVER_PORT", "8080"))

        from src.api.server import _ig_oauth_state
        _ig_oauth_state["app_secret"] = app_secret
        _ig_oauth_state["app_id"] = app_id

        from urllib.parse import urlencode
        scope = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments,instagram_business_manage_insights"
        redirect_uri = f"http://localhost:{api_port}/auth/instagram/callback"
        params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "enable_fb_login": "0",
        }
        auth_url = f"https://www.instagram.com/oauth/authorize?{urlencode(params)}"

        await update.message.reply_text(
            "🔐 <b>Abre este link en tu navegador</b> (mismo Mac):\n\n"
            f'<a href="{auth_url}">Autorizar Instagram → AURA</a>\n\n'
            "• Inicia sesión con Instagram\n"
            "• Acepta los permisos\n"
            "• Recibirás confirmación aquí\n\n"
            "⏱ 5 minutos para completar.",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

    async def _zt_posts(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/posts — list recent publications from the database."""
        try:
            from src.integrations.publication_db import get_recent_publications
            pubs = get_recent_publications(10)

            if not pubs:
                await update.message.reply_text(
                    "📭 No hay publicaciones registradas todavía.\n"
                    "Usa <code>/post instagram sobre [tema]</code> para crear una.",
                    parse_mode="HTML",
                )
                return

            lines = ["📊 <b>Últimas publicaciones</b>\n"]
            for p in pubs:
                ts = (p.get("created_at", "") or "")[:10]
                status_emoji = {
                    "generated": "🖼",
                    "published": "✅",
                    "scheduled": "⏰",
                    "failed": "❌",
                }.get(p.get("status", ""), "📝")
                headline = (p.get("headline", "") or "")[:40]
                platform = (p.get("platform", "") or "").capitalize()
                fmt = p.get("format", "")
                pub_id = p.get("id", "")
                drive_url = p.get("drive_folder_url", "")
                drive_link = f' <a href="{drive_url}">📁</a>' if drive_url else ""
                post_url = p.get("post_url", "")
                post_link = f' <a href="{post_url}">🔗</a>' if post_url else ""
                lines.append(
                    f"{status_emoji} <b>{headline}</b>\n"
                    f"   {platform} {fmt} · {ts} · <code>{pub_id}</code>{drive_link}{post_link}"
                )

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

    async def _zt_drive(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/drive [setup|status|auth <client_id> <client_secret>] — Google Drive integration."""
        from src.integrations.google_auth import (
            get_credentials_info,
            get_setup_instructions,
            is_configured,
            save_service_account_credentials,
            start_oauth_flow,
        )

        args = (update.message.text or "").split(maxsplit=3)
        sub = args[1].lower() if len(args) > 1 else "status"

        if sub == "status":
            info = get_credentials_info()
            if info["configured"]:
                from src.integrations.publication_db import get_recent_publications, _SHEET_ID_PATH, _DRIVE_ROOT_ID_PATH
                sheet_id = _SHEET_ID_PATH.read_text().strip() if _SHEET_ID_PATH.exists() else None
                root_id = _DRIVE_ROOT_ID_PATH.read_text().strip() if _DRIVE_ROOT_ID_PATH.exists() else None
                pubs = get_recent_publications(1)
                total = len(get_recent_publications(1000))

                sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else "—"
                drive_url = f"https://drive.google.com/drive/folders/{root_id}" if root_id else "—"

                await update.message.reply_text(
                    f"✅ <b>Google Drive conectado</b>\n"
                    f"Tipo: {info.get('type','?')}\n"
                    f"Email: {info.get('email','?')}\n\n"
                    f"📊 <a href=\"{sheet_url}\">Abrir Google Sheet</a>\n"
                    f"📁 <a href=\"{drive_url}\">Abrir Drive AURA Social</a>\n\n"
                    f"Publicaciones registradas: {total}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    "❌ <b>Google Drive no configurado</b>\n\n"
                    "Usa <code>/drive setup</code> para ver instrucciones.",
                    parse_mode="HTML",
                )

        elif sub == "setup":
            await update.message.reply_text(
                get_setup_instructions(),
                parse_mode="Markdown",
            )

        elif sub == "auth" and len(args) >= 4:
            # /drive auth <client_id> <client_secret>
            client_id = args[2].strip()
            client_secret = args[3].strip()
            await update.message.reply_text("🔐 Iniciando OAuth flow...")
            auth_url = await start_oauth_flow(client_id, client_secret)
            if auth_url:
                await update.message.reply_text(
                    f"🔗 <b>Abre este link para autorizar:</b>\n\n"
                    f"<a href=\"{auth_url}\">{auth_url[:80]}...</a>\n\n"
                    "Después de autorizar, AURA guardará el token automáticamente.\n"
                    "Tiene 5 minutos.",
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )

        elif sub == "service" and len(args) >= 3:
            # /drive service <json_content>
            json_content = " ".join(args[2:])
            if save_service_account_credentials(json_content):
                await update.message.reply_text(
                    "✅ Service account guardado. Drive + Sheets activos desde ahora."
                )
            else:
                await update.message.reply_text("❌ JSON inválido. Verifica que sea un service account.")

        else:
            await update.message.reply_text(
                "📁 <b>/drive</b> — Google Drive Integration\n\n"
                "<code>/drive status</code>     — estado actual\n"
                "<code>/drive setup</code>      — instrucciones de configuración\n"
                "<code>/drive auth &lt;id&gt; &lt;secret&gt;</code> — OAuth con tus credenciales\n"
                "<code>/drive service &lt;json&gt;</code>        — service account JSON",
                parse_mode="HTML",
            )

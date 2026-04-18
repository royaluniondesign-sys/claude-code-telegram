"""Zero-token status commands — dashboard, status_full, help, diagnose."""

import json as _json
from pathlib import Path

import structlog
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

logger = structlog.get_logger()


class ZeroTokenStatusMixin:
    """Mixin: status, dashboard, help, and diagnostics zero-token commands."""

    async def _zt_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Dashboard URL via ngrok — pre-embedded token, one tap and you're in."""
        import os as _os
        from ...infra.tunnel import get_dashboard_url

        _token = _os.environ.get("DASHBOARD_TOKEN", "")

        # Termora: interactive terminal (port 4030)
        import urllib.request as _req
        termora_url = ""
        termora_online = False
        try:
            with _req.urlopen("http://localhost:4030/api/info", timeout=3) as r:
                info = _json.loads(r.read())
            termora_url = info.get("authUrl") or info.get("tunnelUrl", "")
            termora_online = bool(termora_url) and not termora_url.startswith("http://localhost")
        except Exception:
            pass

        # ngrok URL (updated every 60s by tunnel.py polling task)
        dashboard_public = get_dashboard_url() or ""

        # Embed token — instant login, no form
        if dashboard_public and _token:
            dashboard_auth_url = f"{dashboard_public.rstrip('/')}/?token={_token}"
        elif dashboard_public:
            dashboard_auth_url = dashboard_public
        else:
            dashboard_auth_url = ""

        buttons = []
        msg_lines = ["<b>AURA Acceso remoto</b>\n"]

        # Dashboard section
        msg_lines.append("<b>📊 Dashboard</b>")
        if dashboard_auth_url:
            # Show clean URL + auth URL for copy-paste from other networks
            msg_lines.append(f"🔗 <a href='{dashboard_auth_url}'>Abrir directo</a> (token incluido)")
            msg_lines.append(f"\n<code>{dashboard_auth_url}</code>")
            buttons.append([InlineKeyboardButton("📊 Dashboard →", url=dashboard_auth_url)])
        else:
            msg_lines.append("   Túnel offline — Dashboard solo en LAN: <code>http://localhost:8080</code>")
            if _token:
                msg_lines.append(f"\n🔑 Token: <code>{_token}</code>")

        msg_lines.append("")

        # Terminal section
        msg_lines.append("<b>💻 Terminal remota</b>")
        if termora_online:
            msg_lines.append(f"🔗 <a href='{termora_url}'>Abrir Terminal</a>")
            buttons.append([InlineKeyboardButton("💻 Terminal →", url=termora_url)])
        else:
            msg_lines.append("   Offline — inicia con:")
            msg_lines.append("   <code>cd ~/Projects/termora && npm run dev</code>")

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(
            "\n".join(msg_lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    async def _zt_diagnose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Run full system diagnostic via self-healer."""
        msg = await update.message.reply_text("🔍 Running diagnostics…")
        try:
            from datetime import datetime

            from ...infra.self_healer import run_diagnostics

            report = await run_diagnostics()
            ts = datetime.fromtimestamp(report.checked_at).strftime("%Y-%m-%d %H:%M")
            icon = "✅" if report.ok and not report.warnings else "⚠️" if not report.issues else "🔴"
            lines = [f"<b>🩺 AURA Diagnostics</b> — {ts}\n{icon} <b>{'OK' if report.ok else 'ISSUES'}</b>"]

            if report.issues:
                lines.append(f"\n<b>Problemas ({len(report.issues)}):</b>")
                for issue in report.issues:
                    lines.append(f"  • {issue}")

            if report.fixes_applied:
                lines.append(f"\n<b>Auto-fixed ({len(report.fixes_applied)}):</b>")
                for fix in report.fixes_applied:
                    lines.append(f"  ✔ {fix}")

            if report.warnings:
                lines.append(f"\n<b>Advertencias ({len(report.warnings)}):</b>")
                for w in report.warnings[:5]:
                    lines.append(f"  ⚠ {w}")

            if not report.issues and not report.warnings:
                lines.append("\n✅ Todos los sistemas nominales")

            await msg.edit_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await msg.edit_text(f"❌ Diagnose error: {e}")

    async def _zt_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Concise help — only commands that actually work."""
        text = (
            "<b>AURA — Comandos</b>\n\n"
            "Escribe en lenguaje natural para la mayoría de cosas.\n"
            "Los slash commands son shortcuts para acciones frecuentes.\n\n"
            "<b>Conversación</b>\n"
            "  /new — resetear contexto\n"
            "  /status — brains, rate limits, sistema\n"
            "  /stop — matar tarea colgada\n\n"
            "<b>Voz</b>\n"
            "  /voz on|off — respuestas de audio\n"
            "  Envía audio → AURA transcribe y responde\n\n"
            "<b>Dev</b>\n"
            "  /git — git status/log/diff\n"
            "  /sh &lt;cmd&gt; — shell directo\n"
            "  <code>!cmd</code> o <code>$cmd</code> — shell rápido\n\n"
            "<b>Comunicación</b>\n"
            "  /email destinatario | asunto | cuerpo\n"
            "  /post instagram|twitter &lt;tema&gt;\n\n"
            "<b>Acceso remoto</b>\n"
            "  /terminal — Termora (terminal web)\n"
            "  /dashboard — dashboard en el navegador\n\n"
            "<b>Avanzado</b>\n"
            "  /c &lt;tarea&gt; — conductor 3 capas\n"
            "  /memory — hechos aprendidos\n"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _zt_status_full(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Clean status: active brain + real rate limits + disk."""
        import shutil

        router      = context.bot_data.get("brain_router")
        rate_monitor = context.bot_data.get("rate_monitor")
        user_id     = update.effective_user.id

        # ── Active brain ──────────────────────────────────────────────────────
        brain_name = router.get_active_brain_name(user_id) if router else "?"
        _EMOJIS = {
            "haiku": "🟡", "sonnet": "🟠", "opus": "🔴",
            "gemini": "🔵", "codex": "🟢", "cline": "🟣",
        }
        brain_emoji = _EMOJIS.get(brain_name, "🧠")
        is_auto = user_id not in (getattr(router, "_user_brains", {}) or {})
        mode = "auto" if is_auto else "fijo"
        brain_line = f"{brain_emoji} <b>{brain_name}</b> ({mode})"

        # ── Rate limits ───────────────────────────────────────────────────────
        if rate_monitor:
            limits_block = rate_monitor.format_status()
        else:
            limits_block = "⚠️ Rate monitor no disponible"

        # ── Disk ─────────────────────────────────────────────────────────────
        usage = shutil.disk_usage(Path.home())
        disk_gb = usage.free / (1024 ** 3)

        # ── Working directory ─────────────────────────────────────────────────
        current_dir = context.user_data.get("current_directory") if hasattr(context, "user_data") else None
        if not current_dir:
            approved = getattr(getattr(self, "settings", None), "approved_directory", None)
            current_dir = str(approved) if approved else "~"
        dir_short = str(current_dir).replace(str(Path.home()), "~")

        # ── Compose ──────────────────────────────────────────────────────────
        text = (
            f"<b>Brain activo:</b> {brain_line}\n\n"
            f"📂 <code>{dir_short}</code>\n\n"
            f"{limits_block}\n\n"
            f"💾 Disco libre: {disk_gb:.1f} GB"
        )
        await update.message.reply_text(text, parse_mode="HTML")

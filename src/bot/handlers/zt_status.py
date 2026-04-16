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
        """⚡ AURA Dashboard — botones directos al dashboard y terminal remoto."""
        import urllib.request as _req

        dashboard_url: str = ""
        termora_url: str = ""
        termora_status: str = "offline"

        # Get Termora tunnel (dashboard + terminal live there)
        try:
            with _req.urlopen("http://localhost:4030/api/info", timeout=3) as r:
                info = _json.loads(r.read())
            termora_url = info.get("authUrl", info.get("tunnelUrl", ""))
            if termora_url:
                termora_status = "online"
                dashboard_url = termora_url  # dashboard rides on same tunnel
        except Exception:
            pass

        # Fallback: try AURA API server ngrok or local
        if not dashboard_url:
            try:
                with _req.urlopen("http://localhost:4040/api/tunnels", timeout=2) as r:
                    tunnels = _json.loads(r.read()).get("tunnels", [])
                for t in tunnels:
                    if "8080" in t.get("config", {}).get("addr", ""):
                        dashboard_url = t.get("public_url", "")
                        break
            except Exception:
                pass

        msg = "<b>📊 AURA Dashboard</b>"
        if termora_status == "online":
            msg += "\n✅ Termora online"
        else:
            msg += "\n⚠️ Termora offline — inicia con <code>cd ~/Projects/termora && npm run dev</code>"

        buttons: list[list[InlineKeyboardButton]] = []

        if dashboard_url:
            buttons.append([
                InlineKeyboardButton("🖥️ Abrir Dashboard", url=dashboard_url),
            ])
            buttons.append([
                InlineKeyboardButton("💻 Terminal remota", url=termora_url or dashboard_url),
            ])
        else:
            msg += "\n\n📍 Local: <code>http://localhost:8080</code>\n(sin túnel activo — solo LAN)"

        # Only add API status button if we have a real public URL
        if dashboard_url:
            api_status_url = dashboard_url.rstrip("/") + "/api/status"
            buttons.append([
                InlineKeyboardButton("📡 API status", url=api_status_url),
            ])

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            reply_markup=reply_markup,
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
        """⚡ Concise command reference."""
        text = (
            "<b>🤖 AURA — Comandos</b>\n\n"
            "<b>Core</b>\n"
            "  /new — nueva sesión\n"
            "  /status — estado completo\n"
            "  /health — salud del sistema\n"
            "  /diagnose — diagnóstico automático\n\n"
            "<b>Brains &amp; Memoria</b>\n"
            "  /brain [nombre|auto] — ver/cambiar brain\n"
            "  /limits — uso de rate limits\n"
            "  /memory — hechos aprendidos\n"
            "  /memory add &lt;fact&gt; — agregar hecho\n\n"
            "<b>Shell &amp; Dev</b>\n"
            "  /sh &lt;cmd&gt; — shell directo\n"
            "  /git [subcmd] — git operations\n"
            "  /repo [nombre] — cambiar proyecto\n"
            "  <code>!cmd</code> o <code>$cmd</code> — shell rápido\n\n"
            "<b>Web</b>\n"
            "  /web &lt;url&gt; — analizar URL\n"
            "  /search &lt;query&gt; — búsqueda web\n\n"
            "<b>Comunicación</b>\n"
            "  /email to | asunto | cuerpo\n"
            "  /standup — daily standup\n"
            "  /report — weekly report\n\n"
            "<b>Herramientas</b>\n"
            "  /terminal — abrir Termora\n"
            "  /dashboard — URL del dashboard\n"
            "  /restart — reiniciar bot\n\n"
            "💡 Escribe libremente — AURA entiende lenguaje natural"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _zt_status_full(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """⚡ Compact dashboard — brain, routing, limits, memory, disk."""
        import shutil

        router = context.bot_data.get("brain_router")
        rate_monitor = context.bot_data.get("rate_monitor")
        user_id = update.effective_user.id

        # Brain
        brain_name = router.get_active_brain_name(user_id) if router else "?"
        brain_emojis = {
            "haiku": "🟡", "sonnet": "🟠", "opus": "🔴",
            "gemini": "🔵", "openrouter": "🌐", "cline": "🟣",
            "codex": "🟢", "opencode": "🔶",
        }
        brain_emoji = brain_emojis.get(brain_name, "🧠")
        brain_is_auto = user_id not in (router._user_brains if router else {})
        brain_line = f"{brain_emoji} Brain: <b>{brain_name}</b>" + (" (auto)" if brain_is_auto else " (locked)")

        # Rate limits summary
        limit_lines = []
        if rate_monitor:
            for u in rate_monitor.get_all_usage():
                if u.requests_in_window > 0 or u.is_rate_limited:
                    icon = "⏱️" if u.is_rate_limited else "·"
                    limit_lines.append(f"  {icon} {u.brain_name}: {u.requests_in_window} req")

        # Memory
        mem_path = Path.home() / ".aura" / "brain" / "memory.md"
        mem_lines = 0
        if mem_path.exists():
            content = mem_path.read_text()
            mem_lines = sum(1 for l in content.splitlines() if l.strip().startswith("-"))

        # Disk
        usage = shutil.disk_usage(Path.home())
        disk_free_gb = usage.free / (1024 ** 3)

        # Compose
        lines = [
            "<b>📊 AURA Status</b>\n",
            brain_line,
            f"📂 Dir: <code>{context.user_data.get('current_directory', str(Path.home()))}</code>",
        ]
        if limit_lines:
            lines.append("\n<b>Rate limits (activos):</b>")
            lines.extend(limit_lines)
        lines.append(f"\n🧠 Memoria: {mem_lines} hechos aprendidos")
        lines.append(f"💾 Disco libre: {disk_free_gb:.1f}GB")
        lines.append("\n/brain · /limits · /memory · /health")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

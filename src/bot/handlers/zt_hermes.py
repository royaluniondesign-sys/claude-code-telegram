"""Hermes bridge — delegate tasks to Hermes (OpenClaw) from AURA's Telegram bot.

Commands:
  /hermes <task>  — send a task to Hermes and get response back
  /mesh           — show both agents' health (AURA + Hermes)
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import structlog

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

_OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
_MESH_LOG = Path.home() / ".aura" / "memory" / "mesh-log.md"


def _append_mesh_log(entry: str) -> None:
    try:
        _MESH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_MESH_LOG, "a") as f:
            f.write(f"\n{entry}\n")
    except Exception:
        pass


def _extract_text(data: Any) -> Optional[str]:
    """Extract reply text from openclaw agent --json response."""
    if isinstance(data, dict):
        # Try direct text field
        for key in ("text", "reply", "content", "message"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Walk payloads
        payloads = data.get("result", {}).get("payloads", [])
        texts = [p.get("text", "") for p in payloads if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(texts).strip()
        # Recurse into result
        if "result" in data:
            return _extract_text(data["result"])
    if isinstance(data, list):
        parts = [_extract_text(item) for item in data]
        parts = [p for p in parts if p]
        return "\n".join(parts) if parts else None
    return None


def _safe_parse(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON or Python-style dict output from openclaw."""
    import ast
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        result = ast.literal_eval(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return None


async def _hermes_agent(task: str, timeout: int = 90) -> Dict[str, Any]:
    """Run a task through Hermes via `openclaw agent --json`."""
    proc = await asyncio.create_subprocess_exec(
        _OPENCLAW_BIN, "agent", "--agent", "main",
        "--message", task,
        "--json",
        "--timeout", str(timeout),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 10)
        raw = stdout.decode("utf-8", errors="replace").strip()
        # Try full output first (handles pretty-printed multi-line JSON)
        data = _safe_parse(raw) if raw else None
        if data is None:
            # Fallback: find the last top-level JSON block (lines starting with '{')
            for candidate in reversed(raw.splitlines()):
                if candidate.startswith("{"):
                    data = _safe_parse(candidate)
                    if data is not None:
                        break
        if data is not None:
            text = _extract_text(data)
            status = data.get("status", "?")
            return {"ok": status == "ok", "result": text or "(sin texto)", "status": status}
        # No parseable JSON — treat plain text as the result
        if raw:
            return {"ok": True, "result": raw[:3000]}
        err = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": err[:200] if err else "no response"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timeout ({timeout}s)"}


async def _hermes_health() -> Dict[str, Any]:
    """Check Hermes gateway health via `openclaw health`."""
    proc = await asyncio.create_subprocess_exec(
        _OPENCLAW_BIN, "health",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        text = stdout.decode("utf-8", errors="replace").strip()
        ok = "ok" in text.lower() or "telegram" in text.lower()
        return {"ok": ok, "text": text[:300]}
    except asyncio.TimeoutError:
        return {"ok": False, "text": "timeout"}
    except Exception as e:
        return {"ok": False, "text": str(e)}


class ZeroTokenHermesMixin:
    """Mixin that adds /hermes and /mesh commands to the orchestrator."""

    async def _zt_hermes(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """/hermes <task> — delegate task to Hermes and return result."""
        args = context.args or []
        task = " ".join(args).strip()

        if not task:
            await update.message.reply_text(
                "Uso: `/hermes <tarea>`\nEjemplo: `/hermes busca los últimos commits del repo`",
                parse_mode="Markdown",
            )
            return

        msg = await update.message.reply_text("⚡ Delegando a Hermes…")
        start = time.time()

        result = await _hermes_agent(task)
        elapsed = int((time.time() - start) * 1000)

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        _append_mesh_log(
            f"[{ts}] AURA→HERMES: {task[:80]} → ok={result.get('ok')} ({elapsed}ms)"
        )

        if result.get("ok"):
            content = result.get("result") or "(sin respuesta)"
            if len(content) > 3500:
                content = content[:3500] + "\n…(truncado)"
            # Show exchange inline
            elapsed_s = elapsed / 1000
            reply_text = (
                f"🕸 <b>AURA → Hermes:</b>\n<i>{task[:200]}</i>\n\n"
                f"⚡ <b>Hermes ({int(elapsed_s)}s):</b>\n{content}"
            )
            try:
                await msg.edit_text(reply_text, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(reply_text, parse_mode="HTML")
            # Also broadcast to mesh broadcaster (no-op if same chat, useful for programmatic calls)
            try:
                from src.infra.mesh_broadcaster import broadcast_exchange
                await broadcast_exchange(
                    sender="aura", receiver="hermes",
                    message=task, reply=content, elapsed_s=elapsed_s,
                )
            except Exception:
                pass
        else:
            err = result.get("error", "error desconocido")
            reply = f"❌ Hermes error: {err}"
            try:
                await msg.edit_text(reply, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(reply, parse_mode="Markdown")

    async def _zt_mesh_chat(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """/mesh chat <msg> — Ricardo sends to the group: AURA forwards to Hermes and shows reply."""
        args = (update.message.text or "").split(maxsplit=2)
        msg_text = args[2].strip() if len(args) > 2 else ""
        if not msg_text:
            await update.message.reply_text(
                "Uso: <code>/mesh chat ¿qué hacéis con el tema del blog?</code>",
                parse_mode="HTML",
            )
            return

        progress = await update.message.reply_text(
            f"🕸 <b>Grupo AURA + Hermes</b>\n<i>Ricardo dice:</i> {msg_text[:120]}",
            parse_mode="HTML",
        )

        # Forward to Hermes with context
        full_msg = (
            f"Mensaje de Ricardo para los dos:\n\n\"{msg_text}\"\n\n"
            f"Respóndele directamente."
        )
        start = time.time()
        result = await _hermes_agent(full_msg, timeout=90)
        elapsed = time.time() - start

        if result.get("ok"):
            content = result.get("result") or "(sin respuesta)"
            reply_text = (
                f"🕸 <b>Grupo — Ricardo dice:</b>\n<i>{msg_text[:200]}</i>\n\n"
                f"⚡ <b>Hermes ({int(elapsed)}s):</b>\n{content[:1000]}"
            )
            try:
                await progress.edit_text(reply_text, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(reply_text, parse_mode="HTML")
        else:
            err = result.get("error", "?")
            await progress.edit_text(f"❌ Hermes no responde: {err[:200]}", parse_mode="HTML")

    async def _zt_mesh(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """/mesh [chat <msg>] — agent mesh status or group message."""
        args = (update.message.text or "").split(maxsplit=2)
        subcommand = args[1].lower() if len(args) > 1 else "status"

        if subcommand == "chat":
            await self._zt_mesh_chat(update, context)
            return

        hermes_data = await _hermes_health()
        hermes_ok = hermes_data.get("ok", False)
        hermes_text = hermes_data.get("text", "?")

        # AURA self-check
        aura_brains = "running"
        try:
            br = getattr(self, "brain_router", None)  # type: ignore[attr-defined]
            if br:
                active = getattr(br, "active_brain_name", "?")
                avail = getattr(br, "available_brains", [])
                aura_brains = f"brain={active}, {len(avail)} disponibles"
        except Exception:
            pass

        # Mesh loop status
        loop_info = ""
        try:
            from src.infra.mesh_loop import get_mesh_loop_status
            st = get_mesh_loop_status()
            last_del = st.get("last_delegated") or "ninguna"
            total = st.get("total_delegations", 0)
            last_run = (st.get("last_run_at") or "nunca")[:16].replace("T", " ")
            loop_info = f"\n🔄 <b>Loop autónomo</b>: {total} delegaciones · última: {last_run} UTC\n   Última tarea: <i>{last_del[:60]}</i>"
        except Exception:
            pass

        # Recent mesh log (last 5 entries)
        mesh_recent = ""
        try:
            if _MESH_LOG.exists():
                lines = [l for l in _MESH_LOG.read_text().splitlines() if l.strip()]
                last = lines[-5:] if len(lines) >= 5 else lines
                mesh_recent = "\n".join(f"  {l}" for l in last)
        except Exception:
            pass

        hermes_icon = "✅" if hermes_ok else "❌"
        report = (
            f"🕸 <b>Agent Mesh</b>\n\n"
            f"✨ <b>AURA</b> ✅ | {aura_brains}\n"
            f"⚡ <b>Hermes</b> {hermes_icon} | {(hermes_text.splitlines()[0] if hermes_text else '?')[:80]}"
            f"{loop_info}"
        )
        if mesh_recent:
            report += f"\n\n📋 <b>Últimas conversaciones:</b>\n<code>{mesh_recent}</code>"

        report += (
            "\n\n💬 <code>/mesh chat ¿qué tal vais los dos?</code> — hablar con los dos\n"
            "🔧 <code>/hermes &lt;tarea&gt;</code> — delegar directamente a Hermes"
        )

        await update.message.reply_text(report, parse_mode="HTML")

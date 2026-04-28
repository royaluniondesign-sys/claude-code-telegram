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
        # Filter diagnostic lines (start with '[')
        json_lines = [l for l in raw.splitlines() if l.startswith("{")]
        if json_lines:
            data = json.loads(json_lines[-1])
            text = _extract_text(data)
            status = data.get("status", "?")
            return {"ok": status == "ok", "result": text or "(sin texto)", "status": status}
        # No JSON — try plain text from stderr
        err = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": err[:200] if err else "no response"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timeout ({timeout}s)"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse error: {e}"}


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
            reply = f"⚡ **Hermes** ({elapsed}ms):\n\n{content}"
        else:
            err = result.get("error", "error desconocido")
            reply = f"❌ Hermes error: {err}"

        try:
            await msg.edit_text(reply, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(reply, parse_mode="Markdown")

    async def _zt_mesh(
        self,
        update: "Update",
        context: "ContextTypes.DEFAULT_TYPE",
    ) -> None:
        """/mesh — show health of both agents (AURA + Hermes)."""
        hermes_data = await _hermes_health()
        hermes_ok = hermes_data.get("ok", False)
        hermes_text = hermes_data.get("text", "?")

        # AURA self-check — grab brain info if accessible
        aura_brains = "running"
        try:
            br = getattr(self, "brain_router", None)  # type: ignore[attr-defined]
            if br:
                active = getattr(br, "active_brain_name", "?")
                avail = getattr(br, "available_brains", [])
                aura_brains = f"brain={active}, avail={len(avail)}"
        except Exception:
            pass

        # Recent mesh log (last 3 entries)
        mesh_recent = ""
        try:
            if _MESH_LOG.exists():
                lines = [l for l in _MESH_LOG.read_text().splitlines() if l.strip()]
                last = lines[-3:] if len(lines) >= 3 else lines
                mesh_recent = "\n".join(f"  {l}" for l in last)
        except Exception:
            pass

        hermes_icon = "✅" if hermes_ok else "❌"
        report = (
            f"🕸 **Agent Mesh**\n\n"
            f"🟣 **AURA** ✅ | {aura_brains}\n"
            f"⚡ **Hermes** {hermes_icon} | {hermes_text.splitlines()[0] if hermes_text else '?'}\n"
        )
        if mesh_recent:
            report += f"\n📋 Últimas delegaciones:\n```\n{mesh_recent}\n```"

        await update.message.reply_text(report, parse_mode="Markdown")

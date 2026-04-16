"""Squad router: /api/squad/*, /api/team."""

import asyncio
import os
from typing import Any, Dict

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request

logger = structlog.get_logger()

router = APIRouter()


async def _after_squad_complete(original_task: str, result: str) -> None:
    """After squad completes: propose next tasks + notify Telegram."""
    # 1. Generate next task suggestions via LLM (haiku — cheap)
    suggestions_created: list[str] = []
    try:
        from src.brains.router import BrainRouter as _BR
        from src.infra.task_store import create_task as _ct
        _router = _BR()
        suggestion_prompt = (
            f"Tarea completada: {original_task}\n\n"
            f"Resultado (resumen): {result[:600]}\n\n"
            "Sugiere exactamente 3 tareas de seguimiento concretas y accionables. "
            "Una por línea, formato estricto: TAREA: [título corto] | DESC: [descripción breve]\n"
            "Sé específico. Sin explicaciones extra."
        )
        suggestions_raw = await _router.call("haiku", suggestion_prompt, max_tokens=400)
        lines = [l.strip() for l in suggestions_raw.split("\n") if "TAREA:" in l]
        for line in lines[:3]:
            try:
                title = line.split("TAREA:")[1].split("|")[0].strip()[:120]
                desc = line.split("DESC:")[1].strip()[:300] if "DESC:" in line else ""
                _ct(
                    title=title,
                    description=desc,
                    category="content",
                    priority="medium",
                    created_by="squad_ai",
                    tags=["auto-suggested", "siguiente"],
                )
                suggestions_created.append(title)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Notify Telegram with result + next steps
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = os.environ.get("ALLOWED_USERS", "")
    if token and allowed_raw:
        chat_ids = [u.strip() for u in allowed_raw.split(",") if u.strip().isdigit()]
        summary = result[:700] + ("…" if len(result) > 700 else "")
        next_block = ""
        if suggestions_created:
            next_block = "\n\n📋 <b>Próximos pasos sugeridos:</b>\n" + "\n".join(
                f"  • {t}" for t in suggestions_created
            )
        msg = (
            f"✅ <b>Squad completó:</b> <i>{original_task[:80]}</i>\n\n"
            f"{summary}"
            f"{next_block}"
        )
        async with httpx.AsyncClient(timeout=10) as _client:
            for cid in chat_ids[:3]:
                try:
                    await _client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": int(cid), "text": msg, "parse_mode": "HTML"},
                    )
                except Exception:
                    pass


async def _run_squad_with_hooks(squad: Any, task: str) -> None:
    """Run squad and fire post-completion hooks."""
    try:
        result = await squad.run(task, notify_fn=None)
    except Exception as exc:
        result = f"[error] {exc}"
    await _after_squad_complete(task, result or "")


@router.get("/api/team")
async def get_team_activity() -> Dict[str, Any]:
    """Real-time squad activity snapshot."""
    try:
        from src.agents.activity import get_tracker
        return get_tracker().snapshot()
    except Exception as e:
        return {"run_active": False, "agents": {}, "messages": [], "error": str(e)}


@router.post("/api/squad/stop")
async def stop_squad(_: Request) -> Dict[str, Any]:
    """Request the running squad to stop."""
    try:
        from src.agents.activity import get_tracker
        tracker = get_tracker()
        if not tracker._run_active:
            return {"ok": False, "msg": "Sin tarea activa"}
        tracker.request_stop()
        return {"ok": True, "msg": "Stop solicitado — el squad finalizará tras la tarea actual"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/squad/run")
async def run_squad_task(request: Request) -> Dict[str, Any]:
    """Trigger a squad run from the dashboard. Body: {task: str}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    task = (body.get("task") or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task required")
    try:
        from src.agents.squad import get_squad, AgentSquad
        from src.brains.router import BrainRouter
        squad = get_squad()
        if squad is None:
            # Bootstrap a fresh squad with default router
            brain_router = BrainRouter()
            squad = AgentSquad(brain_router)
        asyncio.create_task(_run_squad_with_hooks(squad, task))
        return {"ok": True, "task": task, "msg": "Squad lanzado"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

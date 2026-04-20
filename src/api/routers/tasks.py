"""Tasks router: /api/tasks/*."""

import asyncio
import os
from datetime import UTC, datetime as _dt
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# Task store imports (module-level so they're available across handlers)
from ...infra.task_store import (
    create_task as _ts_create,
    list_tasks as _ts_list,
    get_task as _ts_get,
    update_task as _ts_update,
    delete_task as _ts_delete,
    stats as _ts_stats,
)


@router.get("/api/tasks")
async def get_tasks(
    status: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Return all tasks from the persistent task store."""
    tasks_list = _ts_list(status=status, category=category)
    return {"tasks": tasks_list, "stats": _ts_stats()}


@router.post("/api/tasks")
async def create_task_endpoint(request: Request) -> Dict[str, Any]:
    """Create a new task."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    task = _ts_create(
        title=title,
        description=body.get("description", ""),
        priority=body.get("priority", "medium"),
        category=body.get("category", "fix"),
        created_by="dashboard",
        auto_fix=bool(body.get("auto_fix", False)),
        fix_command=body.get("fix_command", ""),
        tags=body.get("tags", []),
    )
    return {"ok": True, "task": task}


@router.patch("/api/tasks/{task_id}")
async def update_task_endpoint(task_id: str, request: Request) -> Dict[str, Any]:
    """Update fields on an existing task."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    task = _ts_update(task_id, **body)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True, "task": task}


@router.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(task_id: str) -> Dict[str, Any]:
    """Delete a task."""
    ok = _ts_delete(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@router.post("/api/tasks/evaluate")
async def trigger_evaluation() -> Dict[str, Any]:
    """Force AURA to run self-evaluation now (create tasks from system state)."""
    from ...infra import auto_executor
    auto_executor._LAST_EVAL = 0.0  # reset timer so self_evaluate runs
    asyncio.ensure_future(auto_executor.self_evaluate())
    return {"ok": True, "message": "Self-evaluation triggered"}


@router.post("/api/tasks/{task_id}/publish")
async def publish_task(task_id: str, request: Request) -> Dict[str, Any]:
    """Mark task as published and record channels/URL."""
    task = _ts_get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    channels = body.get("channels", [])  # e.g. ["instagram","linkedin"]
    url = body.get("url", "")
    note = body.get("note", "")
    ts = _dt.now(UTC).isoformat()
    published_info = {
        "channels": channels,
        "url": url,
        "note": note,
        "published_at": ts,
    }
    _ts_update(
        task_id,
        status="completed",
        result=(task.get("result") or "") + f"\n\n[Publicado: {', '.join(channels)} — {ts}]",
        published_channels=channels,
        published_at=ts,
        published_url=url,
    )
    # Notify Telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_raw = os.environ.get("ALLOWED_USERS", "")
    if token and allowed_raw:
        chat_ids = [u.strip() for u in allowed_raw.split(",") if u.strip().isdigit()]
        ch_str = ", ".join(channels) if channels else "sin canal"
        msg = f"📣 <b>Publicado:</b> {task.get('title','')[:60]}\n📍 Canales: {ch_str}"
        if url:
            msg += f"\n🔗 {url}"
        async with httpx.AsyncClient(timeout=8) as _client:
            for cid in chat_ids[:3]:
                try:
                    await _client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": int(cid), "text": msg, "parse_mode": "HTML"},
                    )
                except Exception:
                    pass
    return {"ok": True, "published": published_info}


@router.post("/api/tasks/{task_id}/run")
async def run_task_now_v2(task_id: str) -> Dict[str, Any]:
    """Execute a task immediately via fix_command or conductor fallback."""
    task = _ts_get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("status") == "in_progress":
        return {"ok": False, "error": "already_running", "message": "Task is already running."}

    cmd = (task.get("fix_command") or "").strip()

    if not cmd:
        # No fix_command — delegate to conductor
        title = (task.get("title") or "").strip()
        description = (task.get("description") or "").strip()
        prompt = title
        if description:
            prompt = f"{title}\n\n{description}"

        _ts_update(task_id, status="in_progress", attempts=(task.get("attempts", 0) + 1))

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    "http://localhost:8080/api/conductor/run",
                    json={"task": prompt, "async": True},
                )
        except Exception:
            pass  # Conductor dispatch is fire-and-forget; failures are non-fatal

        return {"ok": True, "via": "conductor", "message": "Ejecutando via conductor..."}

    async def _execute() -> None:
        from ...infra.auto_executor import _run_bash
        _ts_update(task_id, status="in_progress", attempts=(task.get("attempts", 0) + 1))
        ok, output = await _run_bash(cmd, timeout=120)
        ts = _dt.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if ok:
            _ts_update(task_id, status="completed", result=f"[{ts}] ✅\n{output}")
        else:
            _ts_update(task_id, status="failed", result=f"[{ts}] ❌\n{output}")

    asyncio.ensure_future(_execute())
    return {"ok": True, "message": "Execution started — poll /api/tasks for status."}

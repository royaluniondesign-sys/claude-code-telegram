"""Routines router: /api/routines/* and /api/routines/jobs/*."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/api/routines")
async def get_routines() -> Dict[str, Any]:
    """List all routines."""
    try:
        from src.scheduler.routines_store import list_routines
        routines = await list_routines()
        return {"routines": [r.as_dict() for r in routines]}
    except Exception as e:
        return {"routines": [], "error": str(e)}


@router.post("/api/routines")
async def create_routine_endpoint(request: Request) -> Dict[str, Any]:
    """Create a new routine."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        from src.scheduler.routines_store import Routine, create_routine, routine_exists
        from src.scheduler.routine_runner import schedule_routine
        name = (body.get("name") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        if not name or not prompt:
            raise HTTPException(status_code=400, detail="name and prompt required")
        if await routine_exists(name):
            raise HTTPException(status_code=409, detail=f"Routine '{name}' already exists")
        r = Routine(
            name=name,
            prompt=prompt,
            description=body.get("description") or "",
            brain=body.get("brain") or "codex",
            frequency=body.get("frequency") or "daily",
            schedule_time=body.get("schedule_time") or "09:00",
            working_dir=body.get("working_dir") or "/Users/oxyzen/claude-code-telegram",
            is_local=bool(body.get("is_local", True)),
            auto_created=bool(body.get("auto_created", False)),
        )
        await create_routine(r)
        schedule_routine(r)
        return {"ok": True, "routine": r.as_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/routines/{routine_id}")
async def update_routine_endpoint(
    routine_id: str, request: Request
) -> Dict[str, Any]:
    """Update routine fields (name, prompt, enabled, frequency, etc.)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        from src.scheduler.routines_store import update_routine
        from src.scheduler.routine_runner import schedule_routine, unschedule_routine
        updated = await update_routine(routine_id, **body)
        if not updated:
            raise HTTPException(status_code=404, detail="Routine not found")
        # Re-schedule if enabled state changed
        if updated.enabled:
            schedule_routine(updated)
        else:
            unschedule_routine(routine_id)
        return {"ok": True, "routine": updated.as_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/routines/{routine_id}")
async def delete_routine_endpoint(routine_id: str) -> Dict[str, Any]:
    """Delete a routine and remove from scheduler."""
    try:
        from src.scheduler.routines_store import delete_routine
        from src.scheduler.routine_runner import unschedule_routine
        unschedule_routine(routine_id)
        deleted = await delete_routine(routine_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Routine not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/routines/{routine_id}/run")
async def trigger_routine(routine_id: str) -> Dict[str, Any]:
    """Trigger a routine in the background. Returns job_id immediately."""
    try:
        from src.scheduler.routine_runner import run_routine_background
        job_id = await run_routine_background(routine_id)
        return {"ok": True, "job_id": job_id, "status": "running"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/routines/jobs/{job_id}")
async def get_routine_job(job_id: str) -> Dict[str, Any]:
    """Poll the status of a background routine run."""
    from src.scheduler.routine_runner import get_job_status
    job = get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/api/routines/{routine_id}/logs")
async def get_routine_logs(routine_id: str, limit: int = 20) -> Dict[str, Any]:
    """Get execution history for a routine."""
    try:
        from src.scheduler.routines_store import get_logs
        logs = await get_logs(routine_id, limit=limit)
        return {"logs": logs}
    except Exception as e:
        return {"logs": [], "error": str(e)}

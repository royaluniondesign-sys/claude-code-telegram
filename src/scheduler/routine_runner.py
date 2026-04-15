"""Routine runner — executes a routine's prompt via a brain and saves the result.

Integrates with APScheduler: each enabled routine gets a cron job.
Also exposes `run_now()` for on-demand triggers from the API/dashboard.

Auto-creation API: `propose_routine()` lets the conductor suggest a new
routine by name + prompt. If no routine with that name exists, it's created
and scheduled automatically.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .routines_store import (
    Routine,
    append_log,
    create_routine,
    get_routine,
    list_routines,
    routine_exists,
    update_routine,
)

logger = structlog.get_logger()

# In-memory job status store: job_id → {status, output, started_at, routine_id}
_jobs: Dict[str, Dict[str, Any]] = {}

# Global scheduler reference (set during bot startup)
_scheduler: Optional[AsyncIOScheduler] = None
_brain_router: Any = None
_notify_fn: Optional[Callable[[str], Any]] = None


def init_routine_runner(
    scheduler: AsyncIOScheduler,
    brain_router: Any,
    notify_fn: Optional[Callable[[str], Any]] = None,
) -> None:
    """Wire up dependencies. Call once at bot startup."""
    global _scheduler, _brain_router, _notify_fn
    _scheduler = scheduler
    _brain_router = brain_router
    _notify_fn = notify_fn
    logger.info("routine_runner_initialized")


def set_notify_fn(fn: Callable[[str], Any]) -> None:
    """Update the notify function after it becomes available (e.g. after bot startup)."""
    global _notify_fn
    _notify_fn = fn
    logger.debug("routine_runner_notify_fn_updated")


async def run_routine(routine_id: str) -> dict:
    """Execute a routine immediately. Returns result dict."""
    r = await get_routine(routine_id)
    if not r:
        return {"ok": False, "error": "routine not found"}

    if not _brain_router:
        return {"ok": False, "error": "brain_router not initialized"}

    start = time.time()
    brain_name = r.brain or "codex"

    try:
        brain_obj = _brain_router.get_brain(brain_name) or _brain_router.get_default_brain()
        response = await brain_obj.execute(
            r.prompt,
            working_directory=r.working_dir or "",
            timeout_seconds=300,
        )
        elapsed = int((time.time() - start) * 1000)
        output = response.content if hasattr(response, "content") else str(response)
        status = "ok" if not getattr(response, "is_error", False) else "error"
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        output = f"ERROR: {e}"
        status = "error"
    brain = brain_name

    now = datetime.now(UTC).isoformat()
    await update_routine(
        routine_id,
        last_run_at=now,
        last_result=output[:500],
        last_status=status,
        run_count=(r.run_count or 0) + 1,
    )
    await append_log(routine_id, status, output, elapsed, brain)

    logger.info("routine_executed", id=routine_id, name=r.name,
                status=status, ms=elapsed)

    # Notify Ricardo if configured
    if _notify_fn and status == "ok" and r.auto_created:
        try:
            await _notify_fn(
                f"🔄 Rutina **{r.name}** completada\n"
                f"{output[:200]}{'...' if len(output) > 200 else ''}"
            )
        except Exception:
            pass

    return {"ok": status == "ok", "output": output[:1000],
            "brain": brain, "duration_ms": elapsed}


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Return current status of a background run job, or None if not found."""
    return _jobs.get(job_id)


def list_active_jobs() -> Dict[str, Dict[str, Any]]:
    """Return all in-memory jobs (running + recent completed)."""
    return dict(_jobs)


def get_running_job_for_routine(routine_id: str) -> Optional[str]:
    """Return job_id if this routine is already running, else None."""
    for job_id, job in _jobs.items():
        if job["routine_id"] == routine_id and job["status"] == "running":
            return job_id
    return None


async def run_routine_background(routine_id: str) -> str:
    """Start a routine execution in the background. Returns job_id immediately.

    If the routine is already running, returns the existing job_id (dedup protection).
    """
    existing = get_running_job_for_routine(routine_id)
    if existing:
        logger.info("routine_bg_already_running", job_id=existing, routine_id=routine_id)
        return existing

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "job_id": job_id,
        "routine_id": routine_id,
        "status": "running",
        "output": "",
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "brain": None,
        "duration_ms": None,
    }

    async def _run() -> None:
        try:
            result = await run_routine(routine_id)
            _jobs[job_id].update({
                "status": "ok" if result.get("ok") else "error",
                "output": result.get("output", ""),
                "brain": result.get("brain"),
                "duration_ms": result.get("duration_ms"),
                "finished_at": datetime.now(UTC).isoformat(),
            })
        except Exception as exc:
            _jobs[job_id].update({
                "status": "error",
                "output": f"ERROR: {exc}",
                "finished_at": datetime.now(UTC).isoformat(),
            })
        # Evict old jobs after 10 minutes to prevent memory leak
        asyncio.get_event_loop().call_later(600, lambda: _jobs.pop(job_id, None))

    asyncio.create_task(_run(), name=f"routine_bg_{job_id}")
    logger.info("routine_bg_started", job_id=job_id, routine_id=routine_id)
    return job_id


def _make_job_id(routine_id: str) -> str:
    return f"routine_{routine_id}"


def schedule_routine(r: Routine) -> None:
    """Add (or replace) a routine's APScheduler cron job."""
    if not _scheduler or not r.enabled:
        return
    job_id = _make_job_id(r.id)
    cron_str = r.to_cron()

    # Parse cron string (min hour dom mon dow)
    parts = cron_str.split()
    if len(parts) != 5:
        logger.warning("routine_bad_cron", id=r.id, cron=cron_str)
        return

    minute, hour, dom, month, dow = parts
    trigger = CronTrigger(
        minute=minute, hour=hour,
        day=dom, month=month, day_of_week=dow,
    )

    # Remove old job if exists
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)

    async def _job() -> None:
        await run_routine(r.id)

    _scheduler.add_job(_job, trigger=trigger, id=job_id,
                       name=f"routine:{r.name}", replace_existing=True)
    logger.info("routine_scheduled", id=r.id, name=r.name, cron=cron_str)


def unschedule_routine(routine_id: str) -> None:
    if _scheduler:
        job_id = _make_job_id(routine_id)
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)


async def load_all_routines() -> int:
    """Load all enabled routines from DB and schedule them. Returns count."""
    routines = await list_routines()
    count = 0
    for r in routines:
        if r.enabled:
            schedule_routine(r)
            count += 1
    logger.info("routines_loaded", count=count)
    return count


async def propose_routine(
    name: str,
    prompt: str,
    description: str = "",
    brain: str = "codex",
    frequency: str = "daily",
    schedule_time: str = "09:00",
    working_dir: str = "/Users/oxyzen/claude-code-telegram",
) -> Optional[Routine]:
    """Auto-create a routine if one with this name doesn't exist.

    Called by the conductor/proactive_loop when it identifies a recurring
    improvement pattern. Returns the routine if created, None if already exists.
    """
    if await routine_exists(name):
        logger.debug("routine_already_exists", name=name)
        return None

    r = Routine(
        name=name,
        prompt=prompt,
        description=description,
        brain=brain,
        frequency=frequency,
        schedule_time=schedule_time,
        working_dir=working_dir,
        auto_created=True,
    )
    await create_routine(r)
    schedule_routine(r)

    if _notify_fn:
        try:
            await _notify_fn(
                f"🤖 AURA creó rutina automática: **{name}**\n"
                f"_{description or prompt[:100]}_"
            )
        except Exception:
            pass

    logger.info("routine_auto_created", name=name, frequency=frequency)
    return r

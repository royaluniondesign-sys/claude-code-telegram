"""Routine runner — executes a routine's prompt via a brain and saves the result.

Integrates with APScheduler: each enabled routine gets a cron job.
Also exposes `run_now()` for on-demand triggers from the API/dashboard.

Auto-creation API: `propose_routine()` lets the conductor suggest a new
routine by name + prompt. If no routine with that name exists, it's created
and scheduled automatically.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Callable, Optional

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
    brain = r.brain or "codex"

    try:
        result = await _brain_router.call(
            brain,
            r.prompt,
            working_directory=r.working_dir,
            timeout_seconds=300,
        )
        elapsed = int((time.time() - start) * 1000)
        output = result if isinstance(result, str) else str(result)
        status = "ok"
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        output = f"ERROR: {e}"
        status = "error"

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

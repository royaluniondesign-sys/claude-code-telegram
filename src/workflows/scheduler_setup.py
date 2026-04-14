"""Auto-register business workflows with the job scheduler.

Called once during bot startup to ensure all workflows are scheduled.
Uses direct Telegram sends (not Claude) — zero tokens.
"""

import asyncio
from typing import Any, Callable, List, Optional

import structlog
from telegram import Bot

logger = structlog.get_logger()

# Workflow definitions: (name, cron, generator_function, description)
_WORKFLOW_DEFS = [
    {
        "name": "self_heal",
        "cron": "0 */6 * * *",  # Every 6 hours (silent when OK)
        "module": "src.infra.self_healer",
        "func": "run_diagnostics_report",
        "description": "AURA auto-diagnostic — check all systems, auto-fix",
    },
    {
        "name": "daily_standup",
        "cron": "0 8 * * 1-5",  # 8AM weekdays
        "module": "src.workflows.daily_standup",
        "func": "generate_standup",
        "description": "Morning standup — git, pending, health",
    },
    {
        "name": "email_triage",
        "cron": "0 8 * * *",  # 8AM daily
        "module": "src.workflows.email_triage",
        "func": "generate_triage",
        "description": "Email classification by priority",
    },
    {
        "name": "client_followup",
        "cron": "0 17 * * 5",  # Friday 5PM
        "module": "src.workflows.client_followup",
        "func": "generate_followup",
        "description": "Unanswered client emails > 48h",
    },
    {
        "name": "weekly_report",
        "cron": "0 20 * * 0",  # Sunday 8PM
        "module": "src.workflows.weekly_report",
        "func": "generate_weekly_report",
        "description": "Weekly summary — code, brains, system",
    },
]


def _import_generator(module_path: str, func_name: str) -> Callable:
    """Dynamically import a workflow generator function."""
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)


async def _run_workflow_and_send(
    bot: Bot,
    chat_id: int,
    module_path: str,
    func_name: str,
) -> None:
    """Run a workflow generator and send the result to Telegram.

    Skips sending if the generator returns an empty string (silent OK).
    """
    try:
        generator = _import_generator(module_path, func_name)
        report = await generator()
        if not report:
            # Empty = all OK, no noise
            logger.debug("workflow_silent_ok", func=func_name)
            return
        await bot.send_message(
            chat_id=chat_id,
            text=report,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(
            "workflow_send_failed",
            module=module_path,
            func=func_name,
            error=str(e),
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Workflow error ({func_name}): {e}",
            )
        except Exception:
            pass


def register_workflows(
    scheduler: Any,
    bot: Bot,
    owner_chat_id: int,
) -> List[str]:
    """Register all business workflows with APScheduler.

    Returns list of registered workflow names.
    Uses direct APScheduler (not the event bus) for zero-token execution.
    """
    from apscheduler.triggers.cron import CronTrigger

    registered = []

    for wf in _WORKFLOW_DEFS:
        job_id = f"workflow_{wf['name']}"

        # Remove existing job if re-registering
        try:
            scheduler._scheduler.remove_job(job_id)
        except Exception:
            pass

        trigger = CronTrigger.from_crontab(wf["cron"])

        scheduler._scheduler.add_job(
            _run_workflow_and_send,
            trigger=trigger,
            kwargs={
                "bot": bot,
                "chat_id": owner_chat_id,
                "module_path": wf["module"],
                "func_name": wf["func"],
            },
            id=job_id,
            name=wf["description"],
            replace_existing=True,
        )

        registered.append(wf["name"])
        logger.info(
            "workflow_registered",
            name=wf["name"],
            cron=wf["cron"],
            description=wf["description"],
        )

    return registered

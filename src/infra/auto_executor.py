"""AURA Auto-Executor — background loop that picks up auto_fix tasks and runs them.

Runs every 5 minutes. For each pending auto_fix task:
1. Marks it in_progress
2. Runs the fix_command via bash (or built-in resolver)
3. Stores the result and marks completed/failed
4. Creates a memory entry so AURA learns from the fix

Also runs a self-evaluation every 30 minutes:
- Scans logs for new recurring errors
- Checks brain health
- Creates new tasks automatically if issues found
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from typing import Callable, Coroutine, Optional, Any

import structlog

from .task_store import (
    complete_task,
    create_task,
    fail_task,
    list_tasks,
    pending_auto_fix_tasks,
    stats,
    update_task,
)

logger = structlog.get_logger()

_LAST_EVAL: float = 0.0
_EVAL_INTERVAL = 1800  # 30 min
_EXEC_INTERVAL = 300   # 5 min


async def _run_bash(cmd: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a bash command, return (success, output)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode(errors="replace").strip()
        return proc.returncode == 0, text[:2000]
    except asyncio.TimeoutError:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, str(e)


async def _store_memory(fact: str, category: str = "fix") -> None:
    """Persist a learned fact to Mem0."""
    try:
        from ..context.mem0_memory import Mem0Memory
        mem = Mem0Memory()
        await mem.add(fact, metadata={"category": category, "source": "auto_executor"})
    except Exception as e:
        logger.debug("auto_executor_mem_fail", error=str(e))


_NotifyFn = Optional[Callable[[str], Coroutine[Any, Any, None]]]


async def run_pending_tasks(notify: _NotifyFn = None) -> int:
    """Execute all pending auto_fix tasks. Returns count of tasks processed."""
    tasks = pending_auto_fix_tasks()
    processed = 0

    for task in tasks:
        task_id = task["id"]
        title = task["title"]
        cmd = task.get("fix_command", "").strip()

        logger.info("auto_executor_start", task_id=task_id[:8], title=title)
        update_task(task_id, status="in_progress", attempts=task.get("attempts", 0) + 1)

        if not cmd:
            fail_task(task_id, "No fix_command defined — needs manual resolution")
            logger.warning("auto_executor_no_cmd", task_id=task_id[:8])
            continue

        ok, output = await _run_bash(cmd, timeout=60)
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

        if ok:
            result = f"[{ts}] ✅ Auto-fixed:\n{output}"
            complete_task(task_id, result)
            learned = f"Auto-fixed: {title}. Command: {cmd}. Output: {output[:300]}"
            await _store_memory(learned, category="auto_fix")
            logger.info("auto_executor_fixed", task_id=task_id[:8], title=title)
            if notify:
                msg = f"✅ *Auto-fixed:* {title}\n\n`{output[:300]}`"
                try:
                    await notify(msg)
                except Exception:
                    pass
        else:
            attempts = task.get("attempts", 0) + 1
            if attempts >= 3:
                fail_task(task_id, f"[{ts}] ❌ Failed after {attempts} attempts:\n{output}")
                logger.error("auto_executor_gave_up", task_id=task_id[:8], attempts=attempts)
                if notify:
                    msg = f"❌ *Auto-fix failed* ({attempts}× tried): {title}\n\n`{output[:200]}`"
                    try:
                        await notify(msg)
                    except Exception:
                        pass
            else:
                # Back to pending for retry
                update_task(task_id, status="pending", result=f"Attempt {attempts} failed: {output[:200]}")
                logger.warning("auto_executor_retry", task_id=task_id[:8], attempt=attempts)

        processed += 1
        await asyncio.sleep(1)  # brief pause between tasks

    return processed


async def self_evaluate(notify: _NotifyFn = None) -> None:
    """Scan system state and auto-create tasks for issues found."""
    global _LAST_EVAL
    now = time.time()
    if now - _LAST_EVAL < _EVAL_INTERVAL:
        return
    _LAST_EVAL = now

    logger.info("auto_executor_eval_start")
    tasks_created = 0
    new_task_titles: list[str] = []

    def _make_task(title: str, **kw: Any) -> None:
        nonlocal tasks_created
        create_task(title, **kw)
        new_task_titles.append(title)
        tasks_created += 1

    # 1. Check for OPENROUTER_API_KEY missing
    import os
    if not os.environ.get("OPENROUTER_API_KEY"):
        existing = list_tasks(status="pending")
        titles = [t["title"] for t in existing]
        if "Set OPENROUTER_API_KEY in .env" not in titles:
            _make_task(
                "Set OPENROUTER_API_KEY in .env",
                description=(
                    "Self-healer detected OPENROUTER_API_KEY is missing. "
                    "Add to ~/claude-code-telegram/.env to enable OpenRouter brain."
                ),
                priority="high",
                category="fix",
                created_by="auto_executor",
                auto_fix=False,
                tags=["env", "openrouter"],
            )

    # 2. Check for recurring log errors
    try:
        from pathlib import Path
        log = Path.home() / "claude-code-telegram/logs/bot.stdout.log"
        if log.exists():
            lines = log.read_text(errors="replace").splitlines()[-500:]
            error_lines = [l for l in lines if "error" in l.lower() and "warn" not in l.lower()]
            # Find patterns
            patterns: dict[str, int] = {}
            for line in error_lines:
                # Extract key event from structlog lines
                if "event" in line:
                    import re
                    m = re.search(r'"event"\s*[=:]\s*"([^"]{4,60})"', line)
                    if m:
                        key = m.group(1)
                        patterns[key] = patterns.get(key, 0) + 1

            for pattern, count in patterns.items():
                if count >= 5:  # recurring error
                    existing = list_tasks(status="pending", category="fix")
                    if not any(pattern in t["title"] for t in existing):
                        _make_task(
                            f"Fix recurring error: {pattern}",
                            description=f"Found {count} occurrences in recent logs. Pattern: {pattern}",
                            priority="high" if count >= 10 else "medium",
                            category="fix",
                            created_by="auto_executor",
                            auto_fix=False,
                            tags=["log", "recurring"],
                        )
    except Exception as e:
        logger.debug("auto_executor_log_check_fail", error=str(e))

    # 3. Check disk pressure
    try:
        import shutil
        du = shutil.disk_usage("/")
        free_gb = du.free / 1e9
        if free_gb < 10:
            existing = list_tasks(status="pending")
            if not any("disk" in t["title"].lower() for t in existing):
                _make_task(
                    f"Free disk space — only {free_gb:.1f}GB left",
                    description="Disk usage critical. Clean logs, caches, Docker images.",
                    priority="critical" if free_gb < 5 else "high",
                    category="maintenance",
                    created_by="auto_executor",
                    auto_fix=True,
                    fix_command=(
                        "docker system prune -f 2>/dev/null; "
                        "rm -f ~/claude-code-telegram/logs/*.log.bak 2>/dev/null; "
                        "df -h / | tail -1"
                    ),
                    tags=["disk", "maintenance"],
                )
    except Exception as e:
        logger.debug("auto_executor_disk_check_fail", error=str(e))

    # 4. Check RAM pressure
    try:
        import os
        page_sz = os.sysconf("SC_PAGE_SIZE")
        total_b = page_sz * os.sysconf("SC_PHYS_PAGES")
        result = subprocess.run(
            ["bash", "-c", "vm_stat | grep 'Pages free' | awk '{print $3}' | tr -d '.'"],
            capture_output=True, text=True, timeout=3,
        )
        free_pages = int(result.stdout.strip() or "0")
        free_gb = free_pages * page_sz / 1e9
        ram_pct = (1 - (free_pages * page_sz) / total_b) * 100
        if ram_pct > 97:
            existing = list_tasks(status="pending")
            if not any("ram" in t["title"].lower() for t in existing):
                _make_task(
                    f"RAM pressure — {ram_pct:.0f}% used ({free_gb:.1f}GB free)",
                    description="System RAM critically low. Consider closing Chrome tabs or Docker containers.",
                    priority="high",
                    category="maintenance",
                    created_by="auto_executor",
                    auto_fix=False,
                    tags=["ram", "performance"],
                )
    except Exception as e:
        logger.debug("auto_executor_ram_check_fail", error=str(e))

    # 5. Check Resend domain verification
    try:
        import os
        if not os.environ.get("RESEND_VERIFIED_DOMAIN", "").lower() == "true":
            existing = list_tasks()
            if not any("resend" in t["title"].lower() for t in existing):
                _make_task(
                    "Verify Resend domain royaluniondesign.com",
                    description=(
                        "RESEND_VERIFIED_DOMAIN is not set. "
                        "Until domain is verified, AURA can only send email to royaluniondesign@gmail.com. "
                        "Go to resend.com/domains and verify royaluniondesign.com."
                    ),
                    priority="medium",
                    category="fix",
                    created_by="auto_executor",
                    auto_fix=False,
                    tags=["email", "resend"],
                )
    except Exception:
        pass

    if tasks_created:
        logger.info("auto_executor_eval_done", tasks_created=tasks_created)
        if notify and new_task_titles:
            lines = "\n".join(f"• {t}" for t in new_task_titles[:5])
            try:
                await notify(f"🔍 *Auto-detecté {tasks_created} tarea(s) nueva(s):*\n{lines}")
            except Exception:
                pass
    else:
        logger.debug("auto_executor_eval_clean", msg="no new tasks")


async def auto_executor_loop(notify: _NotifyFn = None) -> None:
    """Main background loop — runs forever, executes tasks + evaluates."""
    logger.info("auto_executor_started")
    await asyncio.sleep(15)  # brief startup delay

    while True:
        try:
            # Self-evaluate: create tasks from system state
            await self_evaluate(notify=notify)

            # Execute pending auto_fix tasks
            processed = await run_pending_tasks(notify=notify)
            if processed:
                logger.info("auto_executor_cycle_done", processed=processed)

        except Exception as e:
            logger.error("auto_executor_loop_error", error=str(e))

        await asyncio.sleep(_EXEC_INTERVAL)


import subprocess  # noqa: E402 — needed for RAM check above

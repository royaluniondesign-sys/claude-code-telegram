"""AURA Auto-Executor — background loop that picks up auto_fix tasks and runs them.

Runs every 5 minutes. For each pending auto_fix task:
1. Marks it in_progress
2. Runs the fix_command via bash (or built-in resolver)
3. Stores the result and marks completed/failed
4. Creates a memory entry so AURA learns from the fix
5. Writes a task journal entry with learnings for future reference

Also runs a self-evaluation every 30 minutes:
- Scans logs for new recurring errors
- Checks brain health
- Creates new tasks automatically if issues found

Improvements:
- Parallel execution: independent tasks run concurrently (asyncio.gather)
- Urgency routing: urgent tasks skip queue, use Haiku for speed
- Task journal: learnings persist in ~/.aura/task_journal/{id}.md
- Meta-router: complexity detection → escalate to Sonnet/Opus
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
from .task_journal import (
    start_task as journal_start,
    log_attempt as journal_attempt,
    log_learning as journal_learn,
    complete_task_journal,
    search_similar,
)

logger = structlog.get_logger()

_LAST_EVAL: float = 0.0
_EVAL_INTERVAL = 1800  # 30 min
_EXEC_INTERVAL = 300   # 5 min

# Hours (local time) when RAM pressure notifications are sent to Telegram.
# Auto-fix still runs silently at all times — only the notification is gated.
_RAM_NOTIFY_HOURS: frozenset[int] = frozenset({11, 16, 23})
_LAST_RAM_NOTIFY_HOUR: int = -1

def _ram_notify_allowed() -> bool:
    """Return True only during the three daily notification windows, once per window."""
    global _LAST_RAM_NOTIFY_HOUR
    current_hour = datetime.now().hour
    if current_hour in _RAM_NOTIFY_HOURS and current_hour != _LAST_RAM_NOTIFY_HOUR:
        _LAST_RAM_NOTIFY_HOUR = current_hour
        return True
    return False


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
    """Persist a learned fact to MemPalace."""
    try:
        from ..context.mempalace_memory import store_interaction
        await store_interaction(f"[auto-executor {category}]", fact)
    except Exception as e:
        logger.debug("auto_executor_mem_fail", error=str(e))


_NotifyFn = Optional[Callable[[str], Coroutine[Any, Any, None]]]

# Max tasks to run in parallel — avoid overwhelming system
_MAX_PARALLEL = 3


def _format_fix_message(title: str, output: str) -> str:
    """Format auto-fix output into a clean Telegram message."""
    # RAM pressure: parse structured output
    if "ram" in title.lower() and "RAM_BEFORE=" in output:
        lines = {
            k: v for k, v in (
                line.split("=", 1) for line in output.splitlines() if "=" in line
            )
        }
        before = lines.get("RAM_BEFORE", "?")
        after  = lines.get("RAM_AFTER",  "?")
        freed  = lines.get("RAM_FREED",  "?")
        return f"💾 *RAM limpiada*\n{before} → {after}\n_{freed} liberados_"

    # Generic: trim raw output, keep it short
    clean = output.strip()[:200]
    return f"✅ *Auto-fixed:* {title}\n`{clean}`" if clean else f"✅ *Auto-fixed:* {title}"


async def _execute_single_task(
    task: dict[str, Any],
    notify: _NotifyFn,
) -> bool:
    """Execute one auto_fix task. Returns True if processed."""
    task_id = task["id"]
    title = task["title"]
    cmd = task.get("fix_command", "").strip()
    attempts = task.get("attempts", 0) + 1
    urgent = task.get("urgent", False)
    # Tasks tagged "silent" run the fix but suppress Telegram notification
    silent = "silent" in (task.get("tags") or [])

    logger.info("auto_executor_start", task_id=task_id[:8], title=title, urgent=urgent)
    update_task(task_id, status="in_progress", attempts=attempts)

    # Start task journal
    try:
        journal_start(
            task_id,
            title=title,
            brain="auto_executor",
            context=task.get("description", ""),
        )
        # Look up similar past fixes for context
        similar = search_similar(title, max_results=2)
        if similar:
            logger.debug("auto_executor_similar_found", count=len(similar))
    except Exception:
        pass  # Journal is non-critical

    if not cmd:
        fail_task(task_id, "No fix_command defined — needs manual resolution")
        try:
            complete_task_journal(task_id, "❌ No fix_command defined")
        except Exception:
            pass
        logger.warning("auto_executor_no_cmd", task_id=task_id[:8])
        return True

    # Urgency: shorter timeout for urgent tasks (run fast, fail fast)
    timeout = 20 if urgent else 60
    ok, output = await _run_bash(cmd, timeout=timeout)
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Journal the attempt
    try:
        journal_attempt(task_id, attempt=attempts, command=cmd, output=output, success=ok)
    except Exception:
        pass

    if ok:
        result = f"[{ts}] ✅ Auto-fixed:\n{output}"
        complete_task(task_id, result)
        learned = f"Auto-fixed: {title}. Command: {cmd}. Output: {output[:300]}"
        await _store_memory(learned, category="auto_fix")
        try:
            journal_learn(task_id, f"Fixed with: `{cmd[:120]}`")
            complete_task_journal(task_id, f"✅ Fixed on attempt {attempts}")
        except Exception:
            pass
        logger.info("auto_executor_fixed", task_id=task_id[:8], title=title)
        if notify and not silent:
            msg = _format_fix_message(title, output)
            try:
                await notify(msg)
            except Exception:
                pass
    else:
        if attempts >= 3:
            fail_task(task_id, f"[{ts}] ❌ Failed after {attempts} attempts:\n{output}")
            try:
                journal_learn(task_id, f"Failed {attempts}× — needs manual review")
                complete_task_journal(task_id, f"❌ Gave up after {attempts} attempts")
            except Exception:
                pass
            logger.error("auto_executor_gave_up", task_id=task_id[:8], attempts=attempts)
            if notify and not silent:
                msg = f"❌ *Auto-fix failed* ({attempts}× tried): {title}\n\n`{output[:200]}`"
                try:
                    await notify(msg)
                except Exception:
                    pass
        else:
            # Back to pending for retry
            update_task(
                task_id,
                status="pending",
                result=f"Attempt {attempts} failed: {output[:200]}",
            )
            logger.warning("auto_executor_retry", task_id=task_id[:8], attempt=attempts)

    return True


async def run_pending_tasks(notify: _NotifyFn = None) -> int:
    """Execute pending auto_fix tasks in parallel batches. Returns count processed."""
    tasks = pending_auto_fix_tasks()
    if not tasks:
        return 0

    # Skip phase tasks — those are conductor tasks, not auto_executor tasks
    tasks = [
        t for t in tasks
        if not any(str(tag).startswith("phase:") for tag in (t.get("tags") or []))
    ]
    # Skip content/dashboard tasks without fix_command — they go to squad, not bash
    tasks = [
        t for t in tasks
        if (t.get("fix_command") or "").strip()
        or (
            t.get("category") not in ("content", "user")
            and t.get("created_by") != "dashboard"
        )
    ]
    if not tasks:
        return 0

    processed = 0

    # Split: urgent tasks first (single batch), then regular in parallel batches
    urgent_tasks = [t for t in tasks if t.get("urgent")]
    regular_tasks = [t for t in tasks if not t.get("urgent")]

    all_batches: list[list[dict[str, Any]]] = []
    if urgent_tasks:
        all_batches.append(urgent_tasks)
    # Regular tasks in batches of _MAX_PARALLEL
    for i in range(0, len(regular_tasks), _MAX_PARALLEL):
        all_batches.append(regular_tasks[i : i + _MAX_PARALLEL])

    for batch in all_batches:
        results = await asyncio.gather(
            *[_execute_single_task(task, notify) for task in batch],
            return_exceptions=True,
        )
        processed += sum(1 for r in results if r is True)
        await asyncio.sleep(0.5)  # brief pause between batches

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

    # 1. OPENROUTER_API_KEY check — DISABLED (AURA uses Claude Max subscription, not OpenRouter)
    import os

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

    # 4. Check RAM pressure — notify only, no auto-task (purge requires sudo interactivo)
    try:
        page_sz = os.sysconf("SC_PAGE_SIZE")
        total_b = page_sz * os.sysconf("SC_PHYS_PAGES")
        _vm_result = subprocess.run(
            ["bash", "-c", "vm_stat | grep 'Pages free' | awk '{print $3}' | tr -d '.'"],
            capture_output=True, text=True, timeout=3,
        )
        free_pages = int(_vm_result.stdout.strip() or "0")
        free_gb = free_pages * page_sz / 1e9
        ram_pct = (1 - (free_pages * page_sz) / total_b) * 100
        # Only notify via Telegram (no task creation — purge requires sudo password)
        if ram_pct > 98 and _ram_notify_allowed() and notify:
            try:
                await notify(
                    f"⚠️ RAM alta: {ram_pct:.0f}% usada ({free_gb:.1f}GB libre)\n"
                    f"Ejecuta <code>sudo purge</code> en Terminal si hay lentitud."
                )
            except Exception:
                pass
    except Exception as e:
        logger.debug("auto_executor_ram_check_fail", error=str(e))

    # 5. Resend domain check — DISABLED (not blocking any active workflow)
    pass

    if tasks_created:
        logger.info("auto_executor_eval_done", tasks_created=tasks_created)
        if notify and new_task_titles:
            # Skip batch notification if the only new tasks are silent RAM fixes
            visible_titles = [t for t in new_task_titles if "RAM pressure" not in t or _ram_notify_allowed()]
            if visible_titles:
                lines = "\n".join(f"• {t}" for t in visible_titles[:5])
                try:
                    await notify(f"🔍 *Auto-detecté {len(visible_titles)} tarea(s) nueva(s):*\n{lines}")
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

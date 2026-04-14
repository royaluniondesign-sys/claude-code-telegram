"""AURA Proactive Loop — conductor-driven autonomous development engine.

AURA uses its own 3-layer conductor to continuously analyze and improve itself.
This is NOT a single task — it's the main autonomous loop that runs AURA as a
self-directed agent.

Every 15 minutes:
  1. Gather full AURA state: errors, pending tasks, git diff, test results
  2. Feed context to the Conductor (Claude as director)
  3. Claude creates a plan: diagnose → implement → verify/commit
  4. Brains execute the plan — ClaudeBrain has full tool access (Read/Write/Bash)
  5. Any code changes get committed automatically
  6. Telegram notification if something notable happened

The conductor orchestrates AURA's own development:
  Layer 1 (Analysis): analyze errors, read code, understand root cause
  Layer 2 (Implementation): write fix, new feature, or refactor
  Layer 3 (Verification): syntax check, test, git commit

This is what makes AURA self-improving — not a feature, the engine.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

_LOOP_INTERVAL = 900   # 15 minutes

# ── Proactive loop status (in-memory, for dashboard) ─────────────────────────

_proactive_status: dict = {
    "running": False,
    "last_run_at": None,
    "next_run_at": None,
    "last_result": None,
    "last_steps_ok": 0,
    "last_steps_failed": 0,
    "total_runs": 0,
    "total_steps_ok": 0,
    "total_steps_failed": 0,
    "started_at": None,
}


def get_proactive_status() -> dict:
    """Return current proactive loop status snapshot."""
    return {**_proactive_status}
_AURA_ROOT = Path.home() / "claude-code-telegram"
_LOG_PATH  = _AURA_ROOT / "logs" / "bot.stdout.log"

# ── AURA self-improvement planner prompt ──────────────────────────────────────

_AURA_ROOT_STR = str(_AURA_ROOT)


def _build_task_plan(task: dict) -> "Any":
    """Build a concrete 3-step ConductorPlan for a specific auto_fix task.

    Bypasses the LLM planner entirely — creates deterministic steps that
    force ClaudeBrain to actually READ code, WRITE the fix, and COMMIT.
    """
    from ..brains.conductor import ConductorPlan, ConductorStep

    title = task.get("title", "task")
    desc = task.get("description", "")
    task_id = task.get("id", "")
    # Escalar brain según intentos: haiku (1-2), sonnet (3+)
    attempts = task.get("attempts", 0)
    if attempts >= 3:
        brain = "sonnet"
    else:
        brain = task.get("brain") or "haiku"

    step1_prompt = f"""You are implementing a feature for AURA (the Telegram bot at {_AURA_ROOT_STR}).

TASK ID: {task_id}
TASK: {title}
DESCRIPTION: {desc}

Step 1 — DIAGNOSE: Use Glob and Read tools to find and read the relevant source files.
- Use Bash: ls {_AURA_ROOT_STR}/src/infra/ {_AURA_ROOT_STR}/src/brains/ {_AURA_ROOT_STR}/src/api/
- Use Glob to find files related to this task
- Read the files that need to change
- Return: exact file paths that need modification and the current relevant code"""

    step2_prompt = f"""You are implementing a feature for AURA (the Telegram bot at {_AURA_ROOT_STR}).

TASK ID: {task_id}
TASK: {title}
DESCRIPTION: {desc}

PREVIOUS ANALYSIS:
{{step_1_output}}

Step 2 — IMPLEMENT: Use Write or Edit tools to implement the actual code change.
- Make the minimal correct change to implement what the task describes
- Use Edit tool for small changes, Write tool only if creating new file
- After writing, run: python3 -c "import ast; ast.parse(open('THE_CHANGED_FILE').read())"
- Return: what you changed and confirmation syntax check passed"""

    step3_prompt = f"""You are implementing a feature for AURA (the Telegram bot at {_AURA_ROOT_STR}).

TASK: {title}
IMPLEMENTATION DONE:
{{step_2_output}}

Step 3 — VERIFY + COMMIT:
1. Run syntax check on ALL changed .py files:
   python3 -c "import ast, pathlib; [ast.parse(f.read_text()) for f in pathlib.Path('{_AURA_ROOT_STR}/src').rglob('*.py')]"
2. If ALL syntax OK, commit:
   git -C {_AURA_ROOT_STR} add -A && git -C {_AURA_ROOT_STR} commit -m "auto: {title[:60]}"
3. Return exactly: "COMMITTED: <commit hash>" if committed, or "SYNTAX_ERROR: <details>" if not"""

    steps = [
        ConductorStep(step=1, layer=1, brain=brain, role="diagnoser",
                      prompt=step1_prompt, depends_on=[]),
        ConductorStep(step=2, layer=2, brain=brain, role="implementer",
                      prompt=step2_prompt, depends_on=[1]),
        ConductorStep(step=3, layer=3, brain=brain, role="verifier",
                      prompt=step3_prompt, depends_on=[2]),
    ]

    return ConductorPlan(
        task_summary=title[:120],
        strategy=f"Auto-implement via 3-layer plan. Brain={brain}. ID={task_id[:8]}",
        steps=steps,
    )


# ── Context gathering ─────────────────────────────────────────────────────────

def _recent_errors(n: int = 20) -> list[str]:
    """Extract unique error messages from the last 300 log lines."""
    if not _LOG_PATH.exists():
        return []
    try:
        lines = _LOG_PATH.read_text(errors="replace").splitlines()[-300:]
        errors = []
        seen: set[str] = set()
        for line in lines:
            if '"level": "error"' in line or '"level":"error"' in line:
                # extract event field
                import re as _re
                m = _re.search(r'"event":\s*"([^"]+)"', line)
                ev = m.group(1) if m else line[:80]
                if ev not in seen:
                    seen.add(ev)
                    errors.append(ev)
                if len(errors) >= n:
                    break
        return errors
    except Exception:
        return []


def _pending_tasks(n: int = 5) -> list[dict]:
    """Get top pending tasks from task_store."""
    try:
        from .task_store import list_tasks
        tasks = list_tasks(status="pending")
        # Sort by priority: critical > high > medium > low
        _prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tasks.sort(key=lambda t: _prio.get(t.get("priority", "medium"), 2))
        return [
            {
                "title": t.get("title", ""),
                "priority": t.get("priority", "medium"),
                "category": t.get("category", ""),
                "auto_fix": t.get("auto_fix", False),
                "id": t.get("id", ""),
            }
            for t in tasks[:n]
        ]
    except Exception:
        return []


def _git_status() -> str:
    """Short git status of the AURA repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "status", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()[:300] or "clean"
    except Exception:
        return "unknown"


def _git_recent_commits(n: int = 3) -> str:
    """Last N commit messages."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "log", f"-{n}", "--oneline"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()[:300]
    except Exception:
        return ""


def _test_result() -> str:
    """Run a quick syntax check on modified Python files."""
    try:
        r = subprocess.run(
            ["python3", "-c",
             "import ast, pathlib; "
             "files = list(pathlib.Path('/Users/oxyzen/claude-code-telegram/src').rglob('*.py')); "
             "errs = []; "
             "[errs.append(f.name) for f in files if not (lambda: (ast.parse(f.read_text(errors='replace')), True))()[1] or False]; "
             "print('syntax_ok: ' + str(len(files) - len(errs)) + '/' + str(len(files)))"],
            capture_output=True, text=True, timeout=15,
        )
        return (r.stdout or r.stderr or "?").strip()[:100]
    except Exception:
        return "check failed"


def _build_context() -> str:
    """Assemble current AURA state into a concise context string."""
    errors = _recent_errors()
    tasks = _pending_tasks()
    git = _git_status()
    commits = _git_recent_commits()
    syntax = _test_result()

    lines = []

    if errors:
        lines.append(f"RECENT ERRORS ({len(errors)}):")
        for e in errors[:8]:
            lines.append(f"  - {e}")
    else:
        lines.append("ERRORS: none in recent logs")

    if tasks:
        lines.append(f"\nPENDING TASKS ({len(tasks)}):")
        for t in tasks:
            lines.append(f"  [{t['priority']}] {t['title']}")
    else:
        lines.append("\nPENDING TASKS: none")

    lines.append(f"\nGIT STATUS: {git}")
    if commits:
        lines.append(f"RECENT COMMITS:\n{commits}")
    lines.append(f"\nSYNTAX CHECK: {syntax}")

    return "\n".join(lines)


# ── Self-improvement conductor run ────────────────────────────────────────────

def _pick_next_task() -> Optional[dict]:
    """Pick the highest-priority conductor task.

    Picks any auto_fix=True task that has no fix_command (those require the
    conductor, not a bare bash executor), plus the legacy phase:* tagged tasks.
    """
    try:
        from .task_store import list_tasks
        tasks = list_tasks(status="pending")
        # Conductor tasks: phase-tagged OR auto_fix without a fix_command
        conductor_tasks = [
            t for t in tasks
            if t.get("auto_fix")
            and t.get("attempts", 0) < 3
            and (
                any(str(tag).startswith("phase:") for tag in (t.get("tags") or []))
                or not (t.get("fix_command") or "").strip()
            )
        ]
        if not conductor_tasks:
            return None
        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        conductor_tasks.sort(key=lambda t: prio.get(t.get("priority", "medium"), 2))
        return conductor_tasks[0]
    except Exception:
        return None


def _make_minimal_brain_router() -> Any:
    """Create a minimal brain router with just ClaudeBrain (haiku) when the full
    router is not available (e.g. called from the scheduler without context)."""
    from ..brains.claude_brain import ClaudeBrain

    haiku = ClaudeBrain(model="haiku", timeout=180)
    sonnet = ClaudeBrain(model="sonnet", timeout=300)

    class _MinimalRouter:
        def get_brain(self, name: str):  # type: ignore[return]
            if name in ("sonnet", "opus"):
                return sonnet
            return haiku  # haiku is the default for everything else

        def __bool__(self) -> bool:
            return True

    return _MinimalRouter()


async def run_self_improvement(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
    source: str = "proactive",
) -> Optional[str]:
    """Run one self-improvement cycle.

    If there is a pending auto_fix task: build a deterministic 3-step plan
    and execute it directly (no LLM planner). The brain uses Read/Write/Edit/Bash
    tools to actually implement the task and commit the result.

    Args:
        brain_router: Optional brain router (creates minimal if not provided)
        notify_fn: Optional notification callback
        source: Origin of the run — "proactive" or "scheduler"

    Returns: summary string of what was done, or None if nothing to do.
    """
    from ..brains.conductor import get_conductor, Conductor, set_conductor  # type: ignore
    from .task_store import update_task, complete_task, fail_task

    # If no router provided (e.g. called from scheduler), build a minimal one
    if brain_router is None:
        brain_router = _make_minimal_brain_router()

    conductor = get_conductor(brain_router, notify_fn=notify_fn)
    if conductor is None:
        conductor = Conductor(brain_router, notify_fn=notify_fn)
        set_conductor(conductor)

    # Pick the next auto_fix task
    next_task = _pick_next_task()

    if next_task is None:
        # No auto_fix tasks — check for errors to fix
        errors = _recent_errors()
        if not errors:
            logger.info("proactive_loop_skip", reason="nothing_to_do")
            return None
        # Fall through with generic error-fix task
        logger.info("proactive_loop_start_errors", errors=len(errors))
    else:
        logger.info("proactive_loop_start_task",
                    task_id=next_task["id"][:8], title=next_task["title"][:60])

    _proactive_status["running"] = True
    _proactive_status["started_at"] = datetime.now(UTC).isoformat()

    run_id = f"self-{int(time.time()) % 10000}"

    try:
        if next_task:
            # Mark as in_progress before executing
            new_attempts = next_task.get("attempts", 0) + 1
            update_task(next_task["id"], status="in_progress",
                        attempts=new_attempts)
            next_task["attempts"] = new_attempts  # Actualizar en memoria para _build_task_plan
            plan = _build_task_plan(next_task)
            result = await asyncio.wait_for(
                conductor.run_plan(plan, task=next_task["title"], run_id=run_id, source=source),
                timeout=300,
            )
        else:
            # Generic error-fix: let the LLM planner handle it
            errors = _recent_errors()
            error_task = (
                f"Fix the most critical error in AURA.\n"
                f"PROJECT: {_AURA_ROOT_STR}\n"
                f"Recent errors: {chr(10).join(errors[:5])}\n\n"
                f"Read the relevant source files, fix the error, verify syntax, commit."
            )
            result = await asyncio.wait_for(
                conductor.run(error_task, run_id=run_id, source=source),
                timeout=300,
            )

        _proactive_status["running"] = False
        _proactive_status["last_run_at"] = datetime.now(UTC).isoformat()
        _proactive_status["total_runs"] = _proactive_status["total_runs"] + 1
        _proactive_status["last_steps_ok"] = result.steps_completed
        _proactive_status["last_steps_failed"] = result.steps_failed
        _proactive_status["total_steps_ok"] = (
            _proactive_status["total_steps_ok"] + result.steps_completed
        )
        _proactive_status["total_steps_failed"] = (
            _proactive_status["total_steps_failed"] + result.steps_failed
        )

        output = result.final_output.strip() if result.final_output else ""

        # Mark task done or failed in task_store
        if next_task:
            if result.is_error or result.steps_completed == 0:
                attempts = next_task.get("attempts", 0)
                if attempts < 3:
                    # Reintentar: determinar brain para siguiente intento
                    next_brain = "sonnet" if (attempts + 1) >= 3 else "haiku"
                    update_task(next_task["id"], status="pending", brain=next_brain)
                    _proactive_status["last_result"] = "task_retrying"
                    logger.warning("proactive_loop_retry",
                                   task_id=next_task["id"][:8],
                                   attempt=attempts,
                                   next_brain=next_brain)
                else:
                    # 3 intentos agotados → fallar
                    fail_task(next_task["id"], error=f"Conductor: {result.steps_failed} steps failed (3 attempts)")
                    _proactive_status["last_result"] = "task_failed"
                    logger.warning("proactive_loop_task_failed",
                                   task_id=next_task["id"][:8], title=next_task["title"][:40])
            else:
                committed = "COMMITTED" in output.upper()
                complete_task(next_task["id"],
                              result=output[:300] if output else "conductor ran all steps")
                logger.info("proactive_loop_task_done",
                            task_id=next_task["id"][:8],
                            title=next_task["title"][:40],
                            committed=committed)

        if result.is_error or not output:
            _proactive_status["last_result"] = "no_output"
            logger.warning("proactive_loop_no_output", run_id=result.run_id)
            return None

        _proactive_status["last_result"] = output[:200]

        # Auto-commit any leftover changes the brain didn't commit itself
        task_id = next_task["id"] if next_task else "error-fix"
        task_title = next_task["title"] if next_task else "Error fix"
        await _maybe_commit(output, task_id, task_title)

        duration_s = round(result.total_duration_ms / 1000, 1)
        summary = (
            f"🔄 <b>Proactive loop</b> completó ({duration_s}s)\n"
            f"Steps: {result.steps_completed} ok / {result.steps_failed} failed\n\n"
            f"{output[:600]}"
        )
        logger.info(
            "proactive_loop_done",
            run_id=result.run_id,
            duration_s=duration_s,
            steps_ok=result.steps_completed,
        )
        return summary

    except asyncio.TimeoutError:
        _proactive_status["running"] = False
        _proactive_status["last_result"] = "timeout"
        if next_task:
            fail_task(next_task["id"], error="timeout after 300s")
        logger.warning("proactive_loop_timeout")
        return None
    except Exception as exc:
        _proactive_status["running"] = False
        _proactive_status["last_result"] = f"error: {exc}"
        if next_task:
            fail_task(next_task["id"], error=str(exc)[:200])
        logger.error("proactive_loop_error", error=str(exc))
        return None


async def _maybe_commit(output: str, task_id: str, task_title: str) -> None:
    """If conductor made code changes, commit them with task reference."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "status", "--short"],
            capture_output=True, text=True, timeout=5,
        )
        changed = r.stdout.strip()
        if not changed:
            return  # nothing to commit

        # Check if any .py files changed (conductor might have written code)
        py_changed = any(line.strip().endswith(".py") for line in changed.splitlines())
        if not py_changed:
            return

        # Syntax-check changed files before committing
        for line in changed.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].endswith(".py"):
                fpath = _AURA_ROOT / parts[1]
                if fpath.exists():
                    try:
                        import ast
                        ast.parse(fpath.read_text(errors="replace"))
                    except SyntaxError as e:
                        logger.warning("proactive_loop_syntax_error",
                                       file=parts[1], error=str(e))
                        return  # don't commit broken code

        # Commit with task reference
        msg = f"auto: {task_title} [{task_id[:8]}]\n\n{output[:200]}"
        subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "add", "-A"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "commit", "-m", msg],
            capture_output=True, timeout=15,
        )
        logger.info("proactive_loop_committed", changed_files=changed[:200])
    except Exception as exc:
        logger.warning("proactive_loop_commit_failed", error=str(exc))


# ── Scheduler entry point ─────────────────────────────────────────────────────

async def run_proactive_cycle(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> str:
    """Scheduler-callable wrapper. Returns summary or empty string (silent OK)."""
    summary = await run_self_improvement(brain_router, notify_fn=notify_fn, source="scheduler")
    return summary or ""


# ── Standalone loop (runs in background task) ─────────────────────────────────

async def start_proactive_loop(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> None:
    """Start the autonomous background loop. Call once at bot startup.

    Runs every _LOOP_INTERVAL seconds. Each cycle:
    - Gathers AURA state
    - Runs conductor self-improvement
    - Auto-commits any code changes
    - Notifies Telegram if something notable happened
    """
    logger.info("proactive_loop_started", interval_min=_LOOP_INTERVAL // 60)

    # Small initial delay — let the bot fully start first
    await asyncio.sleep(120)

    while True:
        try:
            summary = await run_self_improvement(brain_router, notify_fn=notify_fn, source="proactive")
            if summary and notify_fn:
                try:
                    result = notify_fn(summary)
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
        except Exception as exc:
            logger.error("proactive_loop_exception", error=str(exc))

        # Track next scheduled run time
        _proactive_status["next_run_at"] = datetime.now(UTC).replace(
            second=0, microsecond=0
        ).isoformat()  # will be corrected below
        import time as _t
        _next = datetime.fromtimestamp(_t.time() + _LOOP_INTERVAL, tz=UTC).isoformat()
        _proactive_status["next_run_at"] = _next

        await asyncio.sleep(_LOOP_INTERVAL)

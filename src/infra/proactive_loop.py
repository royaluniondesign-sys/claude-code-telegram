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
_AURA_ROOT = Path.home() / "claude-code-telegram"
_LOG_PATH  = _AURA_ROOT / "logs" / "bot.stdout.log"

# ── AURA self-improvement planner prompt ──────────────────────────────────────

_AURA_PLANNER = """\
You are AURA directing your own development and maintenance.

PROJECT: /Users/oxyzen/claude-code-telegram/
STACK: Python 3.10+, python-telegram-bot 20+, FastAPI, aiosqlite, APScheduler

ClaudeBrain (haiku/sonnet) has FULL TOOL ACCESS via the `claude` CLI:
  Read, Write, Edit, Bash, Glob, Grep — it can actually modify the codebase.

When you assign a step to haiku/sonnet, write a PRECISE prompt that tells Claude:
  - Exact file path(s) to work on
  - Exactly what to check/fix/build
  - How to verify the fix (python3 -c "import ast; ast.parse(open('file').read())")
  - Whether to commit: "git -C /Users/oxyzen/claude-code-telegram add -A && git commit -m '...'"

Layer philosophy for AURA self-improvement:
  Layer 1 — DIAGNOSE: read logs, read code, understand the problem
  Layer 2 — IMPLEMENT: write the fix or new feature code
  Layer 3 — VERIFY+COMMIT: syntax check, quick test, git commit

Rules:
1. Pick the SINGLE highest-priority issue from the context below
2. If nothing is broken: pick the most useful incomplete feature
3. Each step prompt must be self-contained and specific — no vague instructions
4. Layer 3 MUST include a syntax/import check before committing
5. If context shows no issues and no pending work: skip (return empty steps)

Return ONLY valid JSON. No markdown. No explanation.

CURRENT AURA STATE:
"""


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

async def run_self_improvement(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> Optional[str]:
    """Run one self-improvement cycle using the conductor.

    Returns: summary string of what was done, or None if nothing to do.
    """
    from .conductor import get_conductor, Conductor, set_conductor  # type: ignore

    conductor = get_conductor(brain_router, notify_fn=notify_fn)
    if conductor is None:
        conductor = Conductor(brain_router, notify_fn=notify_fn)
        set_conductor(conductor)

    context = _build_context()

    # Skip if everything is clean and no tasks
    if (
        "ERRORS: none" in context
        and "PENDING TASKS: none" in context
        and "clean" in context
    ):
        logger.info("proactive_loop_skip", reason="all_clean")
        return None

    # Build the self-improvement task description
    task = f"{_AURA_PLANNER}\n{context}"

    logger.info("proactive_loop_start", errors=len(_recent_errors()), tasks=len(_pending_tasks()))

    try:
        result = await asyncio.wait_for(
            conductor.run(task, run_id=f"self-{int(time.time()) % 10000}"),
            timeout=300,  # 5 min hard cap per cycle
        )

        if result.is_error or not result.final_output:
            logger.warning("proactive_loop_no_output", run_id=result.run_id)
            return None

        output = result.final_output.strip()

        # Auto-commit if conductor made changes
        await _maybe_commit(output)

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
        logger.warning("proactive_loop_timeout")
        return None
    except Exception as exc:
        logger.error("proactive_loop_error", error=str(exc))
        return None


async def _maybe_commit(output: str) -> None:
    """If conductor made code changes, commit them."""
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

        # Commit
        msg = f"auto: proactive loop self-improvement\n\n{output[:200]}"
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
    summary = await run_self_improvement(brain_router, notify_fn=notify_fn)
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
            summary = await run_self_improvement(brain_router, notify_fn=notify_fn)
            if summary and notify_fn:
                try:
                    result = notify_fn(summary)
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
        except Exception as exc:
            logger.error("proactive_loop_exception", error=str(exc))

        await asyncio.sleep(_LOOP_INTERVAL)

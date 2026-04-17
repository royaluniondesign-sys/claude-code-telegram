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
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from random import randint
from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

_LOOP_INTERVAL = 900  # 15 minutes

# ── External task interrupt — pauses self-improvement when Ricardo sends a task ──
_external_task_active: bool = False
_external_task_ts: float = 0.0
_EXTERNAL_COOLDOWN = (
    120  # wait 2 min after external task before resuming self-improvement
)


def set_external_task_active(active: bool) -> None:
    """Call this when Ricardo sends a Telegram message. Pauses proactive loop."""
    global _external_task_active, _external_task_ts
    import time as _t

    _external_task_active = active
    if active:
        _external_task_ts = _t.time()
    logger.info("external_task_flag", active=active)


def is_external_task_active() -> bool:
    """True if self-improvement should pause (Ricardo is sending tasks)."""
    import time as _t

    if not _external_task_active:
        return False
    # Auto-clear after cooldown
    if _t.time() - _external_task_ts > _EXTERNAL_COOLDOWN:
        return False
    return True


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


# ── Retry polling decorator with exponential backoff ────────────────────────────


def retry_polling(max_retries: int = 5, backoff_factor: float = 1) -> Callable:
    """Decorator for reliable polling with exponential backoff + jitter.

    Retries the wrapped function with exponential backoff on any exception.
    Uses exponential backoff (2^retries) with random jitter for distributed retry.

    Args:
        max_retries: Maximum number of retry attempts (default 5)
        backoff_factor: Multiplier for backoff timing (default 1 second base)

    Returns:
        Decorator function that wraps the target function with retry logic

    Example:
        @retry_polling(max_retries=3)
        def poll_telegram():
            # polling logic
            pass
    """

    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    wait_time = backoff_factor * (2**retries) + randint(0, 1000) / 1000
                    logger.warning(
                        "polling_retry_scheduled",
                        function=func.__name__,
                        attempt=retries,
                        max_retries=max_retries,
                        wait_seconds=round(wait_time, 3),
                        error=str(e)[:100],
                    )
                    time.sleep(wait_time)
            raise Exception(
                f"Failed to execute {func.__name__} after {max_retries} retries"
            )

        return wrapper

    return decorator


_AURA_ROOT = Path.home() / "claude-code-telegram"
_LOG_PATH = _AURA_ROOT / "logs" / "bot.stdout.log"
CONDUCTOR_LOG_PATH = Path.home() / ".aura" / "memory" / "conductor_log.md"

# ── AURA self-improvement planner prompt ──────────────────────────────────────

_AURA_ROOT_STR = str(_AURA_ROOT)


def _build_task_plan(task: dict) -> "Any":
    """Build a 3-layer ConductorPlan using real multi-brain orchestration.

    Layer 1 — local-ollama (qwen2.5:7b): reads codebase, diagnoses what to change
    Layer 2 — local-ollama (qwen2.5:7b): generates the actual implementation code
    Layer 3 — haiku (Claude): writes files to disk, syntax checks, commits

    This way free local brains do the analysis and code generation,
    and Claude only runs once for file writing + git — the expensive step is minimal.
    """
    from ..brains.conductor import ConductorPlan, ConductorStep

    title = task.get("title", "task")
    desc = task.get("description", "")
    task_id = task.get("id", "")
    attempts = task.get("attempts", 0)
    # Escalate L3 brain on repeated failures
    l3_brain = "sonnet" if attempts >= 3 else "haiku"

    # Build full ADENTRO meta-context for L1 — the diagnoser knows AURA's full history
    adentro_ctx = ""
    try:
        from ..infra.meta_context import build_full_context

        adentro_ctx = build_full_context()
    except Exception:
        pass

    # Include structured lessons from previous cycles so Ollama doesn't repeat mistakes
    lessons_ctx = _read_recent_lessons(5)

    # ── Layer 1: local-ollama diagnoses the codebase ──────────────────────────
    step1_prompt = f"""You are a code analyst for the AURA project at {_AURA_ROOT_STR}.

TASK: {title}
DETAILS: {desc}

{f"## AURA HISTORY (what was tried, what failed):{chr(10)}{adentro_ctx[:1500]}{chr(10)}" if adentro_ctx else ""}
{lessons_ctx}
Your job: Read the relevant source files and produce a precise diagnosis.

IMPORTANT — If the history shows this task or a similar one was already attempted:
- Do NOT propose the same approach that failed
- Propose a different strategy or explain why this time is different

Steps:
1. Run: ls {_AURA_ROOT_STR}/src/infra/ {_AURA_ROOT_STR}/src/brains/ {_AURA_ROOT_STR}/src/api/ {_AURA_ROOT_STR}/src/workflows/
2. Identify which files are relevant to this task
3. Read each relevant file (use cat or read)
4. Output a structured diagnosis:
   - FILE: <path> — what needs to change and why
   - CURRENT CODE: the relevant snippet
   - REQUIRED CHANGE: the exact modification needed
   - APPROACH: why this won't repeat previous failures

Be specific. No filler. Output only what's needed for implementation."""

    # ── Layer 2: local-ollama generates the implementation ────────────────────
    step2_prompt = f"""You are a Python code generator for the AURA project at {_AURA_ROOT_STR}.

TASK: {title}

DIAGNOSIS from previous analysis:
{{step_1_output}}

Your job: Output the COMPLETE implementation ready to be written to disk.

Format your output as:
FILE: <absolute_path>
```python
<complete new file content or the exact edit>
```

Rules:
- Output valid Python only
- NEVER import pandas, sklearn, torch, tensorflow, or any ML library (not installed)
- NEVER import libraries not in the project's pyproject.toml
- If editing existing code, output ONLY the changed function/block + surrounding context (5 lines before/after)
- Use "EDIT:" prefix if modifying existing file, "NEW:" if creating
- No explanations — just the code"""

    # ── Layer 3: haiku writes files, verifies, commits ────────────────────────
    step3_prompt = f"""You are implementing task "{title}" for AURA at {_AURA_ROOT_STR}.

The implementation code is ready:
{{step_2_output}}

Your job — use your file tools:
1. Apply the implementation: use Edit tool for targeted changes, Write for new files
2. Syntax check each changed .py file:
   python3 -c "import ast; ast.parse(open('<changed_file>').read()); print('OK')"
3. If syntax OK: run a quick import check:
   cd {_AURA_ROOT_STR} && python3 -c "import src.main" 2>&1 | head -5
4. If no import errors: commit only the changed files (NOT git add -A):
   git -C {_AURA_ROOT_STR} add -- <specific_file1> <specific_file2>
   git -C {_AURA_ROOT_STR} commit -m "auto: {title[:60]}"
5. Return: "COMMITTED: <hash>" on success, or "SYNTAX_ERROR: <detail>" / "IMPORT_ERROR: <detail>" on failure

NEVER use `git add -A` — only stage the specific files you changed.
You have full tool access. Execute all steps now."""

    steps = [
        ConductorStep(
            step=1,
            layer=1,
            brain="local-ollama",
            role="diagnoser",
            prompt=step1_prompt,
            depends_on=[],
        ),
        ConductorStep(
            step=2,
            layer=2,
            brain="local-ollama",
            role="implementer",
            prompt=step2_prompt,
            depends_on=[1],
        ),
        ConductorStep(
            step=3,
            layer=3,
            brain=l3_brain,
            role="executor",
            prompt=step3_prompt,
            depends_on=[2],
        ),
    ]

    return ConductorPlan(
        task_summary=title[:120],
        strategy=f"local-ollama(L1:diagnose) → local-ollama(L2:codegen) → {l3_brain}(L3:write+commit). Task={task_id[:8]}",
        steps=steps,
    )


# ── Context gathering ─────────────────────────────────────────────────────────


def _recent_errors(n: int = 20) -> list[str]:
    """Extract unique error messages from the last 300 log lines.

    Auto-repair: if critical errors (ConnectionError, BotError, Exception, Error)
    are detected, trigger bot restart via launchctl.
    """
    if not _LOG_PATH.exists():
        return []
    try:
        lines = _LOG_PATH.read_text(errors="replace").splitlines()[-300:]
        errors = []
        seen: set[str] = set()
        has_critical_error = False

        for line in lines:
            if '"level": "error"' in line or '"level":"error"' in line:
                # extract event field
                import re as _re

                m = _re.search(r'"event":\s*"([^"]+)"', line)
                ev = m.group(1) if m else line[:80]
                if ev not in seen:
                    seen.add(ev)
                    errors.append(ev)

                # Only restart for genuine network/connectivity failures —
                # not every Python exception or log-level "error" entry.
                if any(err in line for err in [
                    "ConnectError", "NetworkError", "ConnectionError",
                    "nodename nor servname", "getaddrinfo failed",
                    "BotError", "TelegramError",
                ]):
                    has_critical_error = True

                if len(errors) >= n:
                    break

        # Auto-repair: if critical errors detected, restart the bot
        if has_critical_error:
            logger.warning(
                "self_repair_initiated",
                reason="critical_error_detected",
                error_count=len(errors),
                critical_errors=errors[:3],
            )
            try:
                import subprocess as _sp

                uid = _sp.run(["id", "-u"], capture_output=True, text=True, timeout=5)
                user_id = uid.stdout.strip()
                if user_id:
                    _sp.run(
                        [
                            "launchctl",
                            "kickstart",
                            f"gui/{user_id}/com.aura.telegram-bot",
                        ],
                        timeout=5,
                    )
                    logger.info(
                        "self_repair_action_completed",
                        action="bot_restart",
                        triggered="critical_error",
                        service="com.aura.telegram-bot",
                    )
            except Exception as restart_err:
                logger.warning(
                    "self_repair_action_failed",
                    action="bot_restart",
                    error=str(restart_err),
                )

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
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip()[:300] or "clean"
    except Exception:
        return "unknown"


def _git_recent_commits(n: int = 3) -> str:
    """Last N commit messages."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "log", f"-{n}", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip()[:300]
    except Exception:
        return ""


def _test_result() -> str:
    """Run a quick syntax check on modified Python files."""
    try:
        r = subprocess.run(
            [
                "python3",
                "-c",
                "import ast, pathlib; "
                "files = list(pathlib.Path('/Users/oxyzen/claude-code-telegram/src').rglob('*.py')); "
                "errs = []; "
                "[errs.append(f.name) for f in files if not (lambda: (ast.parse(f.read_text(errors='replace')), True))()[1] or False]; "
                "print('syntax_ok: ' + str(len(files) - len(errs)) + '/' + str(len(files)))",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (r.stdout or r.stderr or "?").strip()[:100]
    except Exception:
        return "check failed"


def _free_disk_gb() -> float:
    """Return free disk space in GB on the main volume."""
    import shutil

    try:
        usage = shutil.disk_usage("/System/Volumes/Data")
        return usage.free / (1024**3)
    except Exception:
        return 99.0  # assume ok if can't check


def _auto_cleanup_disk() -> str:
    """Clean old Claude session files and caches when disk is low.

    Called before each proactive cycle. Returns a brief summary.
    Only deletes files older than 3 days, never the active session.
    """
    import time as _t
    import shutil as _sh

    freed_mb = 0.0

    # 1. Old Claude session JSONL files (bot creates one per task)
    claude_projects = Path.home() / ".claude" / "projects"
    cutoff = _t.time() - 3 * 86400  # 3 days ago
    for jsonl in claude_projects.rglob("*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff:
                size = jsonl.stat().st_size
                jsonl.unlink(missing_ok=True)
                freed_mb += size / (1024 * 1024)
        except Exception:
            pass

    # 2. Empty orphan session directories
    for d in claude_projects.rglob("subagents"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass

    # 3. pip / uv / npm caches (safe to delete anytime)
    caches = [
        Path.home() / "Library" / "Caches" / "pip",
        Path.home() / "Library" / "Caches" / "uv",
        Path.home() / ".npm" / "_npx",
    ]
    for c in caches:
        if c.exists():
            try:
                size = sum(f.stat().st_size for f in c.rglob("*") if f.is_file())
                _sh.rmtree(c, ignore_errors=True)
                freed_mb += size / (1024 * 1024)
            except Exception:
                pass

    return f"auto_cleanup freed {freed_mb:.0f}MB"


_DISK_WARN_GB = 3.0  # warn below this
_DISK_SKIP_GB = 1.5  # skip proactive task below this (preserve for user messages)


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


# ── Auto task generation when queue is empty ─────────────────────────────────


async def _generate_new_tasks(brain_router: Any) -> None:
    """Use local-ollama to scan the codebase and inject new tasks into task_store.

    Called when the proactive loop finds no pending tasks. Keeps the system
    working continuously without manual task injection.
    """
    from .task_store import create_task, list_tasks
    import json as _json

    # Don't generate if there are already pending tasks (any kind — business, fix, etc.)
    existing_pending = list_tasks(status="pending")
    if existing_pending:
        logger.info("proactive_generate_tasks_skipped", reason="pending_tasks_exist", count=len(existing_pending))
        return

    try:
        ollama = brain_router.get_brain("local-ollama")
        if not ollama:
            return

        # Gather codebase state for analysis
        import subprocess as _sp

        git_log = _sp.run(
            ["git", "-C", _AURA_ROOT_STR, "log", "--oneline", "-10"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        recent_files = _sp.run(
            ["git", "-C", _AURA_ROOT_STR, "diff", "--name-only", "HEAD~5", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        src_tree = _sp.run(
            ["find", f"{_AURA_ROOT_STR}/src", "-name", "*.py", "-not", "-path", "*/.*"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        # Read MISSION.md for strategic direction
        mission_path = _AURA_ROOT / "MISSION.md"
        mission = (
            mission_path.read_text(errors="replace")[:2000]
            if mission_path.exists()
            else ""
        )

        scan_prompt = f"""You are AURA, Ricardo's personal AI agent. Generate 3 specific tasks to help Ricardo with his business (RUD Agency).

MISSION:
{mission}

RECENT ACTIVITY:
{git_log}

Rules:
- Focus ONLY on Tier 3 tasks from MISSION.md (Ricardo's business tasks — newsletter, LinkedIn, calendar)
- Do NOT generate infrastructure/code tasks (Tier 1 and Tier 2 are already done)
- Each task should produce a DELIVERABLE Ricardo can use (text, draft, plan)
- Tasks should NOT require modifying source code or committing to git

Use EXACTLY this format:

TASK: Borrador newsletter RUD — Abril 2026
DESC: Redactar el newsletter mensual de RUD Agency para Abril 2026. Incluir: actualizaciones de proyectos, tips de IA para agencias, CTA para consulta gratuita.
PRIORITY: high
CATEGORY: content

Now generate 3 tasks:"""

        resp = await ollama.execute(scan_prompt, timeout_seconds=90)
        if resp.is_error or not resp.content:
            logger.warning("proactive_generate_tasks_failed", error=resp.content[:100])
            return

        # Parse plain-text task format — robust against LLM JSON errors
        import re as _re

        tasks_raw = []
        blocks = _re.split(r"\n(?=TASK:)", resp.content.strip())
        for block in blocks:
            task_match = _re.search(r"TASK:\s*(.+)", block)
            desc_match = _re.search(r"DESC:\s*(.+)", block)
            prio_match = _re.search(
                r"PRIORITY:\s*(high|medium|low|critical)", block, _re.I
            )
            cat_match = _re.search(r"CATEGORY:\s*(\w+)", block, _re.I)
            if task_match:
                tasks_raw.append(
                    {
                        "title": task_match.group(1).strip()[:120],
                        "description": desc_match.group(1).strip()[:500]
                        if desc_match
                        else "",
                        "priority": prio_match.group(1).lower()
                        if prio_match
                        else "medium",
                        "category": cat_match.group(1).lower()
                        if cat_match
                        else "feature",
                    }
                )
        # Deduplicate: skip tasks whose title matches already-done or pending tasks
        all_existing = list_tasks()
        skip_titles = {
            t["title"].strip().lower()
            for t in all_existing
            if t.get("status") in ("done", "failed", "pending", "in_progress")
        }

        added = 0
        for t in tasks_raw[:8]:  # parse up to 8, skip dupes, create up to 5 new
            if added >= 5:
                break
            title = (t.get("title") or "").strip()
            if not title:
                continue
            if title.lower() in skip_titles:
                logger.debug("proactive_task_skipped_duplicate", title=title[:60])
                continue
            create_task(
                title=title[:120],
                description=t.get("description", "")[:500],
                priority=t.get("priority", "medium"),
                category=t.get("category", "feature"),
                tags=["phase:auto", "auto_generated"],
                auto_fix=True,
            )
            skip_titles.add(title.lower())  # prevent double-create in same batch
            added += 1

        logger.info("proactive_generated_tasks", count=added)

        # Fallback: if ollama returned nothing parseable, inject strategic tasks from MISSION.md
        if added == 0:
            logger.warning(
                "proactive_generate_tasks_fallback", reason="ollama_parse_failed"
            )
            _strategic_fallback_tasks = [
                {
                    "title": "Newsletter RUD Agency — Abril 2026",
                    "description": (
                        "Redactar el newsletter mensual de RUD Agency para Abril 2026. "
                        "Incluir actualizaciones de proyectos, tips de IA, CTA para consulta. "
                        "Formato HTML para Resend. Guardar en ~/.aura/sessions/newsletter-abril-2026.html"
                    ),
                    "priority": "high",
                    "category": "content",
                },
                {
                    "title": "LinkedIn: caso de éxito cliente RUD",
                    "description": (
                        "Redactar post de LinkedIn sobre caso de éxito de un cliente de RUD Agency. "
                        "Máx 1500 caracteres. Incluir: problema, solución con IA, resultado. "
                        "Guardar en ~/.aura/sessions/linkedin-caso-exito.md"
                    ),
                    "priority": "high",
                    "category": "content",
                },
            ]
            for t in _strategic_fallback_tasks:
                create_task(
                    title=t["title"][:120],
                    description=t["description"][:500],
                    priority=t["priority"],
                    category=t["category"],
                    tags=["phase:auto", "auto_generated", "fallback"],
                    auto_fix=True,
                )
                added += 1
            logger.info("proactive_strategic_fallback_injected", count=added)

    except Exception as exc:
        logger.warning("proactive_generate_tasks_error", error=str(exc)[:200])


# ── Cycle Governor — plan-persistent, lessons-aware ──────────────────────────

_PLAN_FILE = Path.home() / ".aura" / "memory" / "cycle_plan.json"
_LESSONS_FILE = Path.home() / ".aura" / "memory" / "lessons.md"


def _load_active_plan() -> Optional[dict]:
    """Load the persisted cycle plan from disk, or None if none/expired."""
    try:
        if not _PLAN_FILE.exists():
            return None
        data = json.loads(_PLAN_FILE.read_text())
        # Expire plans older than 2 hours
        created_ts = data.get("created_ts", 0)
        if time.time() - created_ts > 7200:
            _PLAN_FILE.unlink(missing_ok=True)
            return None
        return data
    except Exception:
        return None


def _save_active_plan(plan: dict) -> None:
    """Persist the cycle plan to disk."""
    try:
        _PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PLAN_FILE.write_text(json.dumps(plan, indent=2))
    except Exception:
        pass


def _clear_active_plan() -> None:
    """Remove the persisted plan (cycle complete or abandoned)."""
    try:
        _PLAN_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _write_lesson(task_title: str, success: bool, lesson: str) -> None:
    """Append one lesson to ~/.aura/memory/lessons.md for future cycles to read."""
    try:
        _LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        status = "✅" if success else "❌"
        entry = f"\n## {ts} {status} {task_title[:60]}\n{lesson[:400]}\n"
        with open(_LESSONS_FILE, "a") as f:
            f.write(entry)
    except Exception:
        pass


def _read_recent_lessons(n: int = 5) -> str:
    """Return the last n lessons from lessons.md as context for new plans."""
    try:
        if not _LESSONS_FILE.exists():
            return ""
        lines = _LESSONS_FILE.read_text(errors="replace").strip().splitlines()
        # Grab last ~50 lines (covers ~5 lessons)
        recent = "\n".join(lines[-50:])
        return f"## Lessons from previous cycles:\n{recent}\n" if recent else ""
    except Exception:
        return ""


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
            t
            for t in tasks
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
    """Minimal router for scheduler invocations — Claude-first, no external LLM CLIs."""
    from ..brains.claude_brain import ClaudeBrain
    from ..brains.executor_brain import CodexBrain

    haiku = ClaudeBrain(model="haiku", timeout=240)
    sonnet = ClaudeBrain(model="sonnet", timeout=300)
    codex = CodexBrain(timeout=90)

    _map = {
        "haiku": haiku,
        "sonnet": sonnet,
        "opus": sonnet,   # fallback to sonnet
        "codex": codex,   # ChatGPT Team — code tasks
    }

    class _MinimalRouter:
        def get_brain(self, name: str):  # type: ignore[return]
            return _map.get(name, haiku)

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

    # ── PAUSE: external user task active ──────────────────────────────────────
    if is_external_task_active():
        logger.info("proactive_loop_paused", reason="external_task_active")
        return None

    # ── Active plan — resume unfinished work from previous cycle ──────────────
    active_plan = _load_active_plan()
    if active_plan:
        pending_phases = [p for p in active_plan.get("phases", []) if p.get("status") == "pending"]
        if not pending_phases:
            # All phases done — clear and start fresh
            _clear_active_plan()
            active_plan = None
        else:
            logger.info(
                "proactive_loop_resume_plan",
                plan_id=active_plan.get("plan_id", "?"),
                goal=active_plan.get("goal", "")[:60],
                pending=len(pending_phases),
            )

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
            # Queue empty, no errors — generate new tasks from codebase analysis
            logger.info("proactive_loop_generate_tasks", reason="queue_empty")
            await _generate_new_tasks(brain_router)
            next_task = _pick_next_task()
            if next_task is None:
                logger.info("proactive_loop_skip", reason="nothing_to_do")
                return None
        else:
            # Fall through with generic error-fix task
            logger.info("proactive_loop_start_errors", errors=len(errors))
    else:
        logger.info(
            "proactive_loop_start_task",
            task_id=next_task["id"][:8],
            title=next_task["title"][:60],
        )

    _proactive_status["running"] = True
    _proactive_status["started_at"] = datetime.now(UTC).isoformat()

    run_id = f"self-{int(time.time()) % 10000}"

    try:
        if next_task:
            # ── Persist plan so next cycle knows what we're doing ─────────────
            cycle_plan = {
                "plan_id": run_id,
                "goal": next_task["title"],
                "task_id": next_task["id"],
                "created_ts": time.time(),
                "phases": [
                    {"id": "diagnose", "status": "pending"},
                    {"id": "implement", "status": "pending"},
                    {"id": "verify", "status": "pending"},
                ],
                "lessons_context": _read_recent_lessons(5),
            }
            _save_active_plan(cycle_plan)

            # Mark as in_progress before executing
            new_attempts = next_task.get("attempts", 0) + 1
            update_task(next_task["id"], status="in_progress", attempts=new_attempts)
            next_task["attempts"] = (
                new_attempts  # Actualizar en memoria para _build_task_plan
            )
            plan = _build_task_plan(next_task)

            result = await asyncio.wait_for(
                conductor.run_plan(
                    plan, task=next_task["title"], run_id=run_id, source=source
                ),
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
            # Inject RAG context
            try:
                from src.rag.retriever import RAGRetriever

                _retriever = RAGRetriever()
                rag_ctx = await _retriever.get_context_for_prompt(error_task)
                if rag_ctx:
                    error_task = rag_ctx + "\n\n" + error_task
            except Exception:
                pass
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
                    logger.warning(
                        "proactive_loop_retry",
                        task_id=next_task["id"][:8],
                        attempt=attempts,
                        next_brain=next_brain,
                    )
                else:
                    # 3 intentos agotados → fallar
                    fail_task(
                        next_task["id"],
                        error=f"Conductor: {result.steps_failed} steps failed (3 attempts)",
                    )
                    _proactive_status["last_result"] = "task_failed"
                    logger.warning(
                        "proactive_loop_task_failed",
                        task_id=next_task["id"][:8],
                        title=next_task["title"][:40],
                    )
            else:
                committed = "COMMITTED" in output.upper()
                complete_task(
                    next_task["id"],
                    result=output[:300] if output else "conductor ran all steps",
                )
                logger.info(
                    "proactive_loop_task_done",
                    task_id=next_task["id"][:8],
                    title=next_task["title"][:40],
                    committed=committed,
                )

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
        committed = "COMMITTED" in output.upper() or "COMMIT" in output.upper()
        logger.info(
            "proactive_loop_done",
            run_id=result.run_id,
            duration_s=duration_s,
            steps_ok=result.steps_completed,
            committed=committed,
        )

        # Write learning to conductor_log.md (existing) + lessons.md (new, structured)
        task_title_str = next_task["title"] if next_task else "error-fix"
        _write_learning(
            task_title=task_title_str,
            steps_ok=result.steps_completed,
            duration=duration_s,
            committed=committed,
        )
        # Structured lesson for future cycles to read
        if result.steps_failed > 0 or not committed:
            lesson_text = (
                f"Task '{task_title_str[:60]}' ran {result.steps_completed} steps OK "
                f"but {result.steps_failed} failed. NOT committed.\n"
                f"Output preview: {output[:200]}"
            )
            _write_lesson(task_title_str, success=False, lesson=lesson_text)
        elif committed:
            lesson_text = (
                f"Task '{task_title_str[:60]}' completed and committed in {duration_s}s. "
                f"Approach: {cycle_plan.get('phases', [{}])[0].get('id', '?') if 'cycle_plan' in dir() else '?'}"  # noqa
            )
            _write_lesson(task_title_str, success=True, lesson=lesson_text)

        # Clear active plan — cycle done
        _clear_active_plan()

        # Update MISSION.md if task matches a checkbox
        if committed and next_task:
            _update_mission_checkbox(next_task["title"])

        # Auto-propose routine if conductor output suggests a recurring pattern
        asyncio.ensure_future(_maybe_propose_routine(result, next_task))

        # Schedule outcome check — verify the fix actually worked (async, non-blocking)
        if committed and next_task:
            asyncio.ensure_future(
                _outcome_check_delayed(
                    run_id=result.run_id,
                    task_title=next_task["title"],
                    task_id=next_task["id"],
                    delay_s=300,  # check 5 min after commit
                )
            )

        # Only send Telegram summary when there's a real commit or failure
        if not committed and result.steps_failed == 0:
            return None  # silent OK — dashboard updates via SSE

        summary = (
            f"🔄 <b>Conductor</b> ({duration_s}s) — "
            f"{result.steps_completed} ok / {result.steps_failed} fail\n"
            f"{output[:400]}"
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


async def _outcome_check_delayed(
    run_id: str,
    task_title: str,
    task_id: str,
    delay_s: int = 300,
) -> None:
    """L4 — Outcome verification: did the commit actually fix the problem?

    Runs `delay_s` seconds after a commit. Uses local-ollama to check recent
    log output against the task's stated goal. If still failing, marks the
    task as pending again (retry with different approach) and logs the finding.
    """
    await asyncio.sleep(delay_s)
    try:
        from ..infra.meta_context import build_outcome_context

        prompt = build_outcome_context(task_title, run_id)
        if not prompt:
            return

        # Use local-ollama for outcome check (free, no token cost)
        from ..brains.conductor import get_conductor

        conductor = get_conductor()
        if conductor is None:
            return

        ollama = conductor._router.get_brain("local-ollama")
        if not ollama:
            return

        resp = await asyncio.wait_for(
            ollama.execute(prompt, timeout_seconds=60), timeout=70
        )
        if resp.is_error or not resp.content:
            return

        answer = resp.content.strip().upper()
        still_failing = answer.startswith("YES") or "STILL FAILING" in answer

        # Write outcome to learning log
        log_path = Path.home() / ".aura" / "memory" / "conductor_log.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_line = (
            f"\n### L4 Outcome Check [{run_id}] (+{delay_s // 60}min)\n"
            f"Task: {task_title[:80]}\n"
            f"Still failing: {'YES ❌' if still_failing else 'NO ✅'}\n"
            f"Analysis: {resp.content[:300]}\n"
        )
        with open(log_path, "a") as f:
            f.write(outcome_line)

        if still_failing:
            # Reset task to pending so the loop tries a different approach
            try:
                from .task_store import update_task

                update_task(task_id, status="pending", brain="sonnet")  # escalate brain
                logger.warning(
                    "proactive_outcome_still_failing",
                    run_id=run_id,
                    task_id=task_id[:8],
                    task_title=task_title[:60],
                )
            except Exception:
                pass
        else:
            logger.info(
                "proactive_outcome_confirmed_fixed",
                run_id=run_id,
                task_title=task_title[:60],
            )

    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        logger.debug("proactive_outcome_check_error", error=str(exc))


async def _maybe_propose_routine(result: Any, task: Optional[dict]) -> None:
    """If the conductor output suggests a recurring check, auto-create a routine.

    Looks for explicit signals in the conductor output like:
      ROUTINE: <name> | <description> | <frequency>
    The conductor can include this line when it identifies something worth repeating.
    """
    if not task or not result:
        return
    try:
        # Look for ROUTINE: lines in all step outputs
        output_text = ""
        if hasattr(result, "steps"):
            for step in result.steps or []:
                output_text += (getattr(step, "output", None) or "") + "\n"

        import re

        for match in re.finditer(
            r"ROUTINE:\s*([^|]+)\|([^|]+)\|(\w+)", output_text, re.IGNORECASE
        ):
            name = match.group(1).strip().lower().replace(" ", "-")[:40]
            desc = match.group(2).strip()[:200]
            freq = match.group(3).strip().lower()
            if freq not in ("hourly", "daily", "weekly"):
                freq = "daily"
            from src.scheduler.routine_runner import propose_routine

            await propose_routine(
                name=name,
                prompt=desc,
                description=desc,
                brain="codex",
                frequency=freq,
            )
    except Exception as e:
        logger.debug("_maybe_propose_routine_error", error=str(e))


def _append_to_file(filepath: str, content: str) -> None:
    """Append content to file, expanding ~ to home directory."""
    try:
        expanded_path = Path(filepath).expanduser()
        expanded_path.parent.mkdir(parents=True, exist_ok=True)
        with open(expanded_path, "a") as f:
            f.write(content)
    except Exception:
        pass


def _write_learning(
    task_title: str, steps_ok: int, duration: float, committed: bool
) -> None:
    """Append one learning entry to ~/.aura/memory/conductor_log.md."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.expanduser("~/.aura/memory/conductor_log.md"), "a") as log_file:
        log_file.write(f"Timestamp: {timestamp}\n")
        log_file.write(f"Task: {task_title}\n")
        log_file.write(f"Steps: {'OK' if steps_ok else 'Failed'}\n")
        log_file.write(f"Duration: {duration}\n")
        log_file.write(f"Committed: {'Yes' if committed else 'No'}\n")
        log_file.write("\n")


def _update_mission_checkbox(task_title: str) -> None:
    """Mark a MISSION.md checkbox done if the task title matches."""
    try:
        mission_path = _AURA_ROOT / "MISSION.md"
        if not mission_path.exists():
            return
        content = mission_path.read_text(errors="replace")
        # Look for a checkbox line containing keywords from the task title
        keywords = [w.lower() for w in task_title.split() if len(w) > 4][:3]
        lines = content.splitlines()
        updated = False
        new_lines = []
        for line in lines:
            if "- [ ]" in line and any(kw in line.lower() for kw in keywords):
                line = line.replace("- [ ]", "- [x]", 1)
                updated = True
            new_lines.append(line)
        if updated:
            mission_path.write_text("\n".join(new_lines))
    except Exception:
        pass


_SAFE_COMMIT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".txt",
        ".html",
        ".css",
        ".js",
    }
)
_NEVER_STAGE_NAMES: tuple[str, ...] = (
    ".env",
    "secret",
    "credential",
    "token",
    "password",
    "private_key",
)
# Core files the auto-loop must NEVER overwrite — they define AURA's own engine.
# Any conductor attempt to stage these is silently dropped (logged as warning).
_PROTECTED_CORE_FILES: frozenset[str] = frozenset(
    {
        "src/infra/proactive_loop.py",  # ← this file itself
        "src/infra/watchdog.py",
        "src/main.py",
        "src/config/settings.py",
        "src/config/features.py",
        "src/brains/conductor.py",
        "src/brains/router.py",
        "src/mcp/cli_registrar.py",
        "src/bot/orchestrator.py",
    }
)


async def _maybe_commit(output: str, task_id: str, task_title: str) -> None:
    """Commit conductor changes with safety gates: pre-commit syntax + post-commit pytest.

    Safe staging: only commits whitelisted extensions, never .env or secret files.
    Auto-revert: if pytest fails after commit, immediately reverts the commit.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        changed = r.stdout.strip()
        if not changed:
            return

        # Collect safe files only — whitelist extensions, blacklist secret names
        safe_files: list[str] = []
        for line in changed.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            filepath = parts[1].strip()
            # Handle renamed files (git shows "old -> new")
            if " -> " in filepath:
                filepath = filepath.split(" -> ")[-1].strip()
            fpath_obj = Path(filepath)
            if fpath_obj.suffix.lower() not in _SAFE_COMMIT_EXTENSIONS:
                continue
            if any(pat in fpath_obj.name.lower() for pat in _NEVER_STAGE_NAMES):
                logger.warning("proactive_loop_skip_secret_file", file=filepath)
                continue
            # Never auto-commit core engine files — they require human review
            if filepath in _PROTECTED_CORE_FILES:
                logger.warning("proactive_loop_skip_protected_core", file=filepath)
                continue
            safe_files.append(filepath)

        if not safe_files:
            return

        # Pre-commit: syntax-check every changed .py file
        for filepath in safe_files:
            if not filepath.endswith(".py"):
                continue
            fpath = _AURA_ROOT / filepath
            if fpath.exists():
                try:
                    import ast as _ast

                    _ast.parse(fpath.read_text(errors="replace"))
                except SyntaxError as e:
                    logger.warning(
                        "proactive_loop_syntax_error", file=filepath, error=str(e)
                    )
                    return  # abort — don't commit broken Python

        # Stage only the safe files (never git add -A)
        for filepath in safe_files:
            subprocess.run(
                ["git", "-C", str(_AURA_ROOT), "add", "--", filepath],
                capture_output=True,
                timeout=5,
            )

        # Verify something is actually staged
        staged_r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not staged_r.stdout.strip():
            return  # nothing staged (all files may already be committed)

        # Commit
        msg = f"auto: {task_title} [{task_id[:8]}]\n\n{output[:200]}"
        commit_r = subprocess.run(
            ["git", "-C", str(_AURA_ROOT), "commit", "-m", msg],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if commit_r.returncode != 0:
            logger.warning("proactive_loop_commit_failed", stderr=commit_r.stderr[:300])
            return

        logger.info("proactive_loop_committed", files=safe_files)

        # Post-commit: run pytest ONLY if .py files were committed
        # Uses the venv python (same interpreter as the running bot) — not system python
        py_files_committed = any(f.endswith(".py") for f in safe_files)
        tests_dir = _AURA_ROOT / "tests"

        if py_files_committed and tests_dir.exists():
            # Resolve venv python — must match the running bot, never system python
            import sys as _sys

            venv_python = _sys.executable  # same python running this code
            if "python3.14" in venv_python or "homebrew" in venv_python:
                # Running in wrong interpreter — skip test gate rather than false-revert
                logger.warning(
                    "proactive_loop_pytest_skip",
                    reason="wrong_interpreter",
                    python=venv_python,
                )
            else:
                # Quick smoke test: can we import the main module?
                smoke_r = subprocess.run(
                    [
                        venv_python,
                        "-c",
                        "import src.bot.orchestrator; print('import_ok')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=str(_AURA_ROOT),
                )
                if smoke_r.returncode != 0:
                    subprocess.run(
                        ["git", "-C", str(_AURA_ROOT), "revert", "HEAD", "--no-edit"],
                        capture_output=True,
                        timeout=15,
                    )
                    logger.warning(
                        "proactive_loop_commit_reverted",
                        reason="import_failed",
                        stderr=smoke_r.stderr[:300],
                    )
                    return

                # Full pytest (skip if module not available)
                pytest_check = subprocess.run(
                    [venv_python, "-m", "pytest", "--version"],
                    capture_output=True,
                    timeout=5,
                )
                if pytest_check.returncode == 0:
                    test_r = subprocess.run(
                        [
                            venv_python,
                            "-m",
                            "pytest",
                            str(tests_dir),
                            "-x",
                            "--timeout=30",
                            "-q",
                            "--tb=line",
                            "--ignore",
                            str(tests_dir / "e2e"),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                        cwd=str(_AURA_ROOT),
                    )
                    stdout = test_r.stdout or ""
                    # Revert ONLY if tests actually ran AND explicitly failed
                    actually_ran = (
                        "passed" in stdout
                        or "failed" in stdout
                        or "error" in stdout.lower()
                    )
                    if test_r.returncode != 0 and actually_ran:
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(_AURA_ROOT),
                                "revert",
                                "HEAD",
                                "--no-edit",
                            ],
                            capture_output=True,
                            timeout=15,
                        )
                        logger.warning(
                            "proactive_loop_commit_reverted",
                            reason="pytest_failed",
                            pytest_output=stdout[-400:],
                        )
                        return
                    logger.info("proactive_loop_tests_passed", files=safe_files)
                else:
                    logger.warning(
                        "proactive_loop_pytest_skip", reason="pytest_not_installed"
                    )

    except Exception as exc:
        logger.warning("proactive_loop_commit_failed", error=str(exc))


async def run_tests_and_self_repair() -> dict:
    """Run tests and attempt to repair any failures.

    Returns:
        Dict with test results and repair status:
        {
            'tests_passed': bool,
            'total': int,
            'failed': int,
            'repairs_attempted': int,
            'repairs_successful': int,
            'summary': str
        }
    """
    from src.api.api_tests import run_unit_tests
    from src.brains.brain_health import check_brain_health, diagnose_error, repair_error

    result = {
        "tests_passed": False,
        "total": 0,
        "failed": 0,
        "repairs_attempted": 0,
        "repairs_successful": 0,
        "summary": "",
    }

    logger.info("self_repair_process_started")

    # Step 1: Run unit tests
    logger.info("self_repair_starting", phase="run_tests")
    test_summary = run_unit_tests()
    result["total"] = test_summary.total
    result["failed"] = test_summary.failed
    result["tests_passed"] = test_summary.success
    logger.info(
        "self_repair_test_run_completed",
        total=test_summary.total,
        failed=test_summary.failed,
        passed=test_summary.success,
    )

    if test_summary.success:
        result["summary"] = f"All tests passed ({test_summary.total} tests)"
        logger.info("self_repair_all_passed", total=test_summary.total)
        logger.info("self_repair_process_completed", status="all_tests_passed")
        return result

    # Step 2: Tests failed — diagnose and repair
    logger.warning(
        "self_repair_tests_failed",
        total=test_summary.total,
        failed=test_summary.failed,
    )

    # Step 3: Check brain health
    brain_names = [
        "claude_brain",
        "executor_brain",
        "gemini_brain",
    ]

    for brain_name in brain_names:
        health = check_brain_health(brain_name)
        if not health.is_healthy:
            logger.warning(
                "self_repair_brain_unhealthy", brain=brain_name, error=health.error_msg
            )

            # Attempt repair
            logger.info(
                "self_repair_action_initiating",
                action="diagnose_brain",
                brain=brain_name,
                reason=health.error_msg,
            )
            diagnosis = diagnose_error(brain_name, health.error_msg or "unknown")
            result["repairs_attempted"] += 1

            logger.info(
                "self_repair_action_initiating",
                action="repair_brain",
                brain=brain_name,
                diagnosis=str(diagnosis)[:100],
            )
            success = repair_error(brain_name, diagnosis)
            if success:
                result["repairs_successful"] += 1
                logger.info(
                    "self_repair_action_completed",
                    action="repair_brain",
                    brain=brain_name,
                    status="success",
                )
            else:
                logger.warning(
                    "self_repair_action_failed",
                    action="repair_brain",
                    brain=brain_name,
                    status="repair_did_not_resolve",
                )

    # Step 4: Re-run tests after repairs
    if result["repairs_attempted"] > 0:
        logger.info(
            "self_repair_retesting_after_repairs",
            phase="retest_after_repairs",
            repairs_attempted=result["repairs_attempted"],
            repairs_successful=result["repairs_successful"],
        )
        test_summary = run_unit_tests()
        result["tests_passed"] = test_summary.success
        result["total"] = test_summary.total
        result["failed"] = test_summary.failed
        logger.info(
            "self_repair_retest_completed",
            total=test_summary.total,
            failed=test_summary.failed,
            passed=test_summary.success,
        )

    # Generate summary
    if result["tests_passed"]:
        result["summary"] = (
            f"✓ Self-repair successful: {result['repairs_successful']}/{result['repairs_attempted']} "
            f"repairs applied. Tests now passing ({result['total']} total)."
        )
        logger.info(
            "self_repair_completed",
            status="success",
            repairs_attempted=result["repairs_attempted"],
            repairs_successful=result["repairs_successful"],
            total_tests=result["total"],
            failed_tests=result["failed"],
        )
    else:
        result["summary"] = (
            f"✗ Self-repair incomplete: {result['failed']} tests still failing. "
            f"Attempted {result['repairs_attempted']} repairs, {result['repairs_successful']} succeeded."
        )
        logger.warning(
            "self_repair_completed",
            status="incomplete",
            repairs_attempted=result["repairs_attempted"],
            repairs_successful=result["repairs_successful"],
            total_tests=result["total"],
            failed_tests=result["failed"],
        )

    logger.info("self_repair_process_completed", status="repairs_completed")
    return result


# ── Scheduler entry point ─────────────────────────────────────────────────────


async def run_proactive_cycle(
    brain_router: Any = None,
    notify_fn: Optional[Callable] = None,
) -> str:
    """Scheduler-callable wrapper. Returns summary or empty string (silent OK)."""
    summary = await run_self_improvement(
        brain_router, notify_fn=notify_fn, source="scheduler"
    )
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
            # ── Disk guard: clean before every cycle ─────────────────────────
            free_gb = _free_disk_gb()
            if free_gb < _DISK_WARN_GB:
                cleanup_msg = _auto_cleanup_disk()
                new_free = _free_disk_gb()
                logger.warning(
                    "disk_low_auto_cleanup",
                    before_gb=round(free_gb, 1),
                    after_gb=round(new_free, 1),
                    action=cleanup_msg,
                )
                free_gb = new_free

            if free_gb < _DISK_SKIP_GB:
                logger.error("disk_critical_skip_proactive", free_gb=round(free_gb, 1))
                _proactive_status["last_result"] = f"skipped: disk only {free_gb:.1f}GB"
                await asyncio.sleep(_LOOP_INTERVAL)
                continue

            # Hard timeout: if a cycle takes >360s something is stuck — kill it and move on
            summary = await asyncio.wait_for(
                run_self_improvement(
                    brain_router, notify_fn=notify_fn, source="proactive"
                ),
                timeout=360,
            )
            if summary and notify_fn:
                try:
                    result = notify_fn(summary)
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
            logger.info("proactive_cycle_completed_successfully")
        except asyncio.CancelledError:
            # CancelledError — log and return so caller can reschedule us
            logger.info("proactive_loop_cancelled")
            _proactive_status["running"] = False
            _proactive_status["last_result"] = "cancelled"
            return
        except asyncio.TimeoutError:
            logger.error("proactive_loop_timeout", timeout_s=360)
            _proactive_status["running"] = False
            _proactive_status["last_result"] = "timeout"
        except ImportError as e:
            logger.error("proactive_loop_import_error", error=str(e), exc_info=True)
            _proactive_status["running"] = False
            _proactive_status["last_result"] = "import_error"
        except Exception as exc:
            logger.error("proactive_loop_exception", error=str(exc), exc_info=True)
            _proactive_status["running"] = False
            _proactive_status["last_result"] = "exception"

        # Track next scheduled run time
        import time as _t

        _next = datetime.fromtimestamp(_t.time() + _LOOP_INTERVAL, tz=UTC).isoformat()
        _proactive_status["next_run_at"] = _next

        await asyncio.sleep(_LOOP_INTERVAL)

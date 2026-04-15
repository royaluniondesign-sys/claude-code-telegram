"""Error pattern detector — finds recurring errors and creates auto-fix tasks.

No ML dependencies. Uses simple frequency counting over the bot log.
Called by self_healer to surface actionable recurring issues.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_BOT_LOG = Path.home() / "claude-code-telegram" / "logs" / "bot.stdout.log"
_MIN_OCCURRENCES = 3  # error must appear at least this many times to create a task


def _extract_errors(lines: list[str]) -> list[str]:
    """Extract error event strings from structured JSON log lines."""
    errors: list[str] = []
    for line in lines:
        try:
            if '"level": "error"' not in line and '"level":"error"' not in line:
                continue
            data = json.loads(line)
            event = data.get("event", "")
            if event:
                errors.append(event)
        except (json.JSONDecodeError, KeyError):
            pass
    return errors


def get_recurring_errors(n_lines: int = 500, min_count: int = _MIN_OCCURRENCES) -> dict[str, int]:
    """Return a dict of {error_event: count} for errors seen >= min_count times."""
    if not _BOT_LOG.exists():
        return {}
    try:
        lines = _BOT_LOG.read_text(errors="replace").splitlines()[-n_lines:]
        errors = _extract_errors(lines)
        counts = Counter(errors)
        return {err: cnt for err, cnt in counts.items() if cnt >= min_count}
    except Exception as exc:
        logger.debug("error_pattern_read_fail", error=str(exc))
        return {}


def create_tasks_for_patterns() -> list[str]:
    """Create auto-fix tasks for recurring errors not already in task_store.

    Returns list of created task IDs.
    """
    try:
        from .task_store import create_task, list_tasks
    except ImportError:
        return []

    recurring = get_recurring_errors()
    if not recurring:
        return []

    # Get titles of existing pending tasks to avoid duplicates
    existing_titles = {t.get("title", "").lower() for t in list_tasks(status="pending")}

    created: list[str] = []
    for error_event, count in recurring.items():
        title = f"Fix recurring error: {error_event}"
        if title.lower() in existing_titles:
            continue  # already tracked

        task_id = create_task(
            title=title[:120],
            description=(
                f"Error '{error_event}' appeared {count}× in recent logs. "
                f"Read src/ to find root cause and fix."
            ),
            priority="high" if count >= 5 else "medium",
            category="fix",
            tags=["log", "recurring"],
            auto_fix=False,  # needs conductor, not bare bash
        )
        created.append(task_id)
        logger.info("error_pattern_task_created", event=error_event, count=count)

    return created


class ErrorPatternDetector:
    """Simple frequency-based error pattern detector (no ML required)."""

    def __init__(self) -> None:
        self._extra_errors: list[str] = []

    def log_error(self, error_message: str) -> None:
        """Record an additional error outside the log file."""
        self._extra_errors.append(error_message)

    def get_error_count(self) -> int:
        """Return total number of manually logged errors."""
        return len(self._extra_errors)

    def get_recurring(self) -> dict[str, int]:
        """Return recurring errors from bot log."""
        return get_recurring_errors()

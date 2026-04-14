"""Error pattern detector — finds recurring errors and creates fix tasks.

Analyzes bot.stdout.log for errors that appear 3+ times in the last 24h,
groups them by error type, and auto-creates tasks if no fix task exists.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

_BOT_LOG = Path.home() / "claude-code-telegram/logs/bot.stdout.log"


def _normalize_error(error_msg: str) -> str:
    """Normalize error message by removing variable parts.

    Examples:
      TypeError: unsupported operand type(s) for +: 'int' and 'str'
      → normalized: TypeError: unsupported operand type(s) for +
    """
    # Remove line numbers, file paths, memory addresses
    msg = re.sub(r":\d+:", ":::", error_msg)  # line numbers
    msg = re.sub(r"/Users/.*?/", "~/", msg)  # file paths
    msg = re.sub(r"0x[0-9a-f]+", "0xADDR", msg, flags=re.IGNORECASE)  # memory addrs
    msg = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*?Z", "TIMESTAMP", msg)  # timestamps
    # Keep only up to first colon + error type (e.g., "TypeError:", "KeyError:")
    match = re.match(r"^([A-Za-z]+Error)[^a-zA-Z]*(.{0,100})?", msg)
    if match:
        error_type = match.group(1)
        context = match.group(2) or ""
        return f"{error_type}{context}".strip()
    return msg[:100]


def detect_patterns(hours: int = 24) -> Dict[str, List[dict]]:
    """Detect recurring errors in bot log.

    Returns:
        {
            'error_type': [
                {'normalized': '...', 'count': 3, 'last_seen': '...', 'examples': ['raw_error_1', 'raw_error_2']}
            ]
        }
    """
    if not _BOT_LOG.exists():
        return {}

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    error_groups: Dict[str, List[str]] = defaultdict()
    error_counts: Counter[str] = Counter()
    error_timestamps: Dict[str, str] = {}

    try:
        with open(_BOT_LOG, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("level") != "error":
                    continue

                # Parse timestamp
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                if ts < cutoff:
                    continue

                # Extract error message
                error_msg = entry.get("error", "")
                if not error_msg:
                    continue

                # Normalize
                normalized = _normalize_error(error_msg)

                # Track
                error_counts[normalized] += 1
                if normalized not in error_groups:
                    error_groups[normalized] = []
                error_groups[normalized].append(error_msg)
                error_timestamps[normalized] = ts_str

    except Exception as e:
        logger.debug("error_pattern_read_fail", error=str(e))
        return {}

    # Filter: 3+ occurrences
    result = {}
    for normalized, count in error_counts.items():
        if count >= 3:
            examples = error_groups[normalized][:3]  # Keep up to 3 raw examples
            result[normalized] = {
                'normalized': normalized,
                'count': count,
                'last_seen': error_timestamps.get(normalized, ""),
                'examples': examples,
            }

    return result


def create_tasks_for_patterns(patterns: Optional[Dict] = None) -> List[str]:
    """Create fix tasks for detected patterns if not already pending.

    Returns list of task IDs created.
    """
    if patterns is None:
        patterns = detect_patterns()

    if not patterns:
        return []

    try:
        from .task_store import create_task, list_tasks
    except ImportError:
        logger.debug("task_store_import_fail")
        return []

    task_ids = []
    active_titles = {t["title"] for t in list_tasks() if t.get("status") in ("pending", "in_progress")}

    for normalized, info in patterns.items():
        title = f"Fix recurring error — {normalized[:60]}"

        # Skip if task already exists
        if title in active_titles:
            continue

        description = (
            f"Error appears {info['count']} times in last 24h.\n\n"
            f"**Last seen:** {info['last_seen']}\n\n"
            f"**Examples:**\n"
        )
        for example in info['examples']:
            description += f"- {example[:150]}\n"

        task = create_task(
            title=title,
            description=description,
            priority="high",
            category="fix",
            created_by="error_pattern_detector",
            auto_fix=False,
            tags=["error", "pattern", "recurring"],
        )
        task_ids.append(task["id"])
        logger.info("error_pattern_task_created", error=normalized, count=info['count'], task_id=task["id"])

    return task_ids

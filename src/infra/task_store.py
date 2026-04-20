"""AURA Real Task Store — persistent JSON-backed task queue.

Tasks are stored at ~/.aura/tasks.json.
Every task has a unique ID, status lifecycle, priority, category,
and an optional auto_fix flag so the executor can pick it up.

Schema:
{
  "id": "uuid4",
  "title": "short description",
  "description": "detail / context",
  "status": "pending|in_progress|completed|failed|cancelled",
  "priority": "critical|high|medium|low",
  "category": "fix|optimize|learn|maintenance|user",
  "created_by": "aura|user|scheduler|self_healer",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "auto_fix": true,          # can executor attempt auto-resolution?
  "fix_command": "bash cmd", # command to run for auto-fix (optional)
  "result": "outcome text",  # populated after completion/failure
  "attempts": 0,             # how many times executor has tried
  "tags": ["env", "resend"]
}
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_TASKS_FILE = Path.home() / ".aura" / "tasks.json"
_lock = threading.Lock()


class TaskStore:
    """Optimized in-memory task store with dict-based O(1) lookups."""

    def __init__(self):
        self.tasks = {}

    def add_task(self, task_id: str, task: Dict[str, Any]) -> None:
        self.tasks[task_id] = task

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.tasks.get(task_id)

    def remove_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    def list_all(self) -> List[Dict[str, Any]]:
        return list(self.tasks.values())

    def clear(self) -> None:
        self.tasks.clear()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load() -> List[Dict[str, Any]]:
    try:
        if _TASKS_FILE.exists():
            return json.loads(_TASKS_FILE.read_text())
    except Exception:
        pass
    return []


def _save(tasks: List[Dict[str, Any]]) -> None:
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TASKS_FILE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))


# ── Public API ──────────────────────────────────────────────────────────────


def create_task(
    title: str,
    *,
    description: str = "",
    priority: str = "medium",
    category: str = "fix",
    created_by: str = "aura",
    auto_fix: bool = False,
    fix_command: str = "",
    tags: Optional[List[str]] = None,
    urgent: bool = False,
    brain: str = "",
) -> Dict[str, Any]:
    """Create and persist a new task. Returns the created task dict.

    If a task with the same title already exists in `pending` or `in_progress`
    status, the existing task is returned without creating a duplicate.

    Args:
        urgent: If True, task is moved to front of queue and runs with Haiku (min latency).
        brain: Target brain name (haiku/sonnet/opus/gemini). Empty = auto-route.
    """
    with _lock:
        tasks = _load()
        # Deduplication: return existing active task if same title found
        title_lower = title.strip().lower()
        for existing in tasks:
            if (
                existing.get("title", "").strip().lower() == title_lower
                and existing.get("status") in ("pending", "in_progress")
            ):
                return existing

        task: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "title": title,
            "description": description,
            "status": "pending",
            "priority": "critical" if urgent else priority,  # urgent → always critical
            "category": category,
            "created_by": created_by,
            "created_at": _now(),
            "updated_at": _now(),
            "auto_fix": auto_fix,
            "fix_command": fix_command,
            "result": "",
            "attempts": 0,
            "tags": tags or [],
            "urgent": urgent,
            "brain": brain,
        }
        tasks.append(task)
        _save(tasks)
    return task


def list_tasks(
    status: Optional[str] = None,
    category: Optional[str] = None,
    created_by: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return tasks, optionally filtered. Newest first."""
    with _lock:
        tasks = _load()
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    if created_by:
        tasks = [t for t in tasks if t.get("created_by") == created_by]
    # Sort: critical/high first, then by created_at desc
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    tasks.sort(
        key=lambda t: (
            priority_order.get(t.get("priority", "medium"), 2),
            t.get("created_at", ""),
        ),
        reverse=False,
    )
    return tasks[-limit:]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        tasks = _load()
    return next((t for t in tasks if t["id"] == task_id), None)


def update_task(task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Update fields on a task. Returns updated task or None if not found."""
    with _lock:
        tasks = _load()
        for task in tasks:
            if task["id"] == task_id:
                task.update(fields)
                task["updated_at"] = _now()
                _save(tasks)
                return task
    return None


def delete_task(task_id: str) -> bool:
    with _lock:
        tasks = _load()
        original_len = len(tasks)
        tasks = [t for t in tasks if t["id"] != task_id]
        if len(tasks) < original_len:
            _save(tasks)
            return True
    return False


def complete_task(task_id: str, result: str = "") -> Optional[Dict[str, Any]]:
    return update_task(task_id, status="completed", result=result)


def fail_task(task_id: str, error: str = "") -> Optional[Dict[str, Any]]:
    return update_task(task_id, status="failed", result=error)


def pending_auto_fix_tasks() -> List[Dict[str, Any]]:
    """Return pending tasks with auto_fix=True, ordered by priority."""
    with _lock:
        tasks = _load()
    return [
        t for t in tasks
        if t.get("status") == "pending"
        and t.get("auto_fix") is True
        and t.get("attempts", 0) < 3  # max 3 attempts
    ]


def stats() -> Dict[str, Any]:
    """Return task statistics."""
    with _lock:
        tasks = _load()
    total = len(tasks)
    by_status: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    for t in tasks:
        s = t.get("status", "unknown")
        p = t.get("priority", "medium")
        c = t.get("category", "fix")
        by_status[s] = by_status.get(s, 0) + 1
        by_priority[p] = by_priority.get(p, 0) + 1
        by_category[c] = by_category.get(c, 0) + 1
    return {
        "total": total,
        "by_status": by_status,
        "by_priority": by_priority,
        "by_category": by_category,
    }

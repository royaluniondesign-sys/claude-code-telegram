"""Persistent conductor run history.

Saves recent runs to ~/.aura/conductor_history.json (max 50 entries).
Used by the Sessions panel in the dashboard to replay and inspect runs.

Each run entry:
{
  run_id, task, task_summary, strategy, source,
  started_at, completed_at, total_duration_ms,
  steps_completed, steps_failed, is_error, final_output,
  steps: [{step, layer, brain, role, status, prompt, output, duration_ms, error}]
}
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_HISTORY_FILE = Path.home() / ".aura" / "conductor_history.json"
_lock = threading.Lock()
_MAX_RUNS = 60


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load() -> List[Dict[str, Any]]:
    try:
        if _HISTORY_FILE.exists():
            data = json.loads(_HISTORY_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save(runs: List[Dict[str, Any]]) -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(runs, indent=2, ensure_ascii=False))


def save_run(run_data: Dict[str, Any]) -> None:
    """Persist a conductor run. Oldest entries trimmed beyond MAX_RUNS."""
    entry = {**run_data, "saved_at": _now()}
    with _lock:
        runs = _load()
        runs.append(entry)
        if len(runs) > _MAX_RUNS:
            runs = runs[-_MAX_RUNS:]
        _save(runs)


def get_history(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent runs, newest first."""
    with _lock:
        runs = _load()
    return list(reversed(runs[-limit:]))


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Find a specific run by ID."""
    with _lock:
        runs = _load()
    return next((r for r in runs if r.get("run_id") == run_id), None)


def history_stats() -> Dict[str, Any]:
    """Aggregate stats from all recorded runs."""
    with _lock:
        runs = _load()
    if not runs:
        return {"total": 0, "success": 0, "failed": 0, "avg_duration_ms": 0}
    success = sum(1 for r in runs if not r.get("is_error"))
    failed = len(runs) - success
    total_ms = sum(r.get("total_duration_ms", 0) for r in runs)
    avg_ms = total_ms // len(runs) if runs else 0
    return {
        "total": len(runs),
        "success": success,
        "failed": failed,
        "avg_duration_ms": avg_ms,
    }

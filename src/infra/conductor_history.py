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


def conductor_metrics() -> Dict[str, Any]:
    """Calculate success rates by layer and best performing brain."""
    with _lock:
        runs = _load()
    if not runs:
        return {
            "by_layer": {},
            "by_brain": {},
            "best_brain": None,
            "best_brain_rate": 0,
            "overall_success_rate": 0,
            "total_runs": 0
        }

    # Collect all steps from all runs
    layer_stats: Dict[int, Dict[str, int]] = {}
    brain_stats: Dict[str, Dict[str, Any]] = {}

    for run in runs:
        steps = run.get("steps", [])
        for step in steps:
            layer = step.get("layer")
            brain = step.get("brain")
            status = step.get("status")
            duration_ms = step.get("duration_ms", 0)

            # Track by layer
            if layer not in layer_stats:
                layer_stats[layer] = {"success": 0, "failed": 0}
            if status == "done":
                layer_stats[layer]["success"] += 1
            elif status == "failed":
                layer_stats[layer]["failed"] += 1

            # Track by brain
            if brain not in brain_stats:
                brain_stats[brain] = {"success": 0, "failed": 0, "total_duration_ms": 0, "count": 0}
            if status == "done":
                brain_stats[brain]["success"] += 1
            elif status == "failed":
                brain_stats[brain]["failed"] += 1
            brain_stats[brain]["total_duration_ms"] += duration_ms
            brain_stats[brain]["count"] += 1

    # Calculate success rates by layer
    by_layer: Dict[str, Dict[str, Any]] = {}
    for layer in sorted(layer_stats.keys()):
        stats = layer_stats[layer]
        total = stats["success"] + stats["failed"]
        rate = (stats["success"] / total * 100) if total > 0 else 0
        by_layer[str(layer)] = {
            "success": stats["success"],
            "failed": stats["failed"],
            "total": total,
            "success_rate": round(rate, 2)
        }

    # Calculate success rates by brain
    by_brain: Dict[str, Dict[str, Any]] = {}
    best_brain = None
    best_rate = -1.0

    for brain in sorted(brain_stats.keys()):
        stats = brain_stats[brain]
        total = stats["success"] + stats["failed"]
        rate = (stats["success"] / total * 100) if total > 0 else 0
        avg_duration_ms = (stats["total_duration_ms"] / stats["count"]) if stats["count"] > 0 else 0
        by_brain[brain] = {
            "success": stats["success"],
            "failed": stats["failed"],
            "total": total,
            "success_rate": round(rate, 2),
            "avg_duration_ms": round(avg_duration_ms, 2)
        }

        if rate > best_rate:
            best_rate = rate
            best_brain = brain

    # Overall success rate
    total_steps = sum(stats["success"] + stats["failed"] for stats in layer_stats.values())
    total_success = sum(stats["success"] for stats in layer_stats.values())
    overall_rate = (total_success / total_steps * 100) if total_steps > 0 else 0

    return {
        "by_layer": by_layer,
        "by_brain": by_brain,
        "best_brain": best_brain,
        "best_brain_rate": round(best_rate, 2) if best_brain else 0,
        "overall_success_rate": round(overall_rate, 2),
        "total_runs": len(runs)
    }

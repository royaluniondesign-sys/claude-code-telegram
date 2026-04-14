"""Extract and persist learnings from conductor runs.

After each conductor run, extract key insights and append to conductor_log.md
in the AURA memory directory. This helps AURA learn from patterns and improve
future orchestration decisions.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..brains.conductor import ConductorResult

logger = logging.getLogger(__name__)

# Thread-safe file append operations
_log_lock = Lock()

CONDUCTOR_LOG_PATH = Path.home() / ".aura" / "memory" / "conductor_log.md"


def save_learnings(result: ConductorResult) -> None:
    """Extract key learnings from a conductor run and persist to log.

    Args:
        result: ConductorResult containing run metrics and outcomes
    """
    if not result or not result.plan:
        return

    try:
        # Extract learnings
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        status = "✅ SUCCESS" if not result.is_error else "❌ FAILED"

        # Success rate
        total_steps = result.steps_completed + result.steps_failed
        success_rate = (
            100 * result.steps_completed / total_steps if total_steps > 0 else 0
        )

        # Brain usage analysis
        brain_freq = {}
        failed_brains = {}
        if result.plan.steps:
            for step in result.plan.steps:
                brain_freq[step.brain] = brain_freq.get(step.brain, 0) + 1
                if step.status == "failed":
                    failed_brains[step.brain] = failed_brains.get(step.brain, 0) + 1

        brain_summary = ", ".join(
            f"{b}×{c}" for b, c in sorted(brain_freq.items(), key=lambda x: -x[1])
        )

        # Layers used
        layers_used = (
            ", ".join(f"Layer {l}" for l in result.plan.layers_used)
            if result.plan.layers_used
            else "N/A"
        )

        # Duration
        duration_s = result.total_duration_ms / 1000.0

        # Format markdown entry
        entry = f"""## {timestamp} — {status}
- **Task**: {result.task[:80]}...
- **Strategy**: {result.plan.strategy[:100]}...
- **Duration**: {duration_s:.1f}s
- **Steps**: {result.steps_completed} completed, {result.steps_failed} failed ({success_rate:.0f}% success)
- **Brains**: {brain_summary}
- **Layers**: {layers_used}
- **Run ID**: `{result.run_id}`
"""
        if failed_brains:
            entry += f"- **Failed brains**: {', '.join(failed_brains.keys())}\n"

        if result.is_error:
            entry += f"- **Error**: {result.error[:100]}\n"

        entry += "\n"

        # Thread-safe append
        _log_lock.acquire()
        try:
            # Ensure directory exists
            CONDUCTOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

            # Append entry
            with open(CONDUCTOR_LOG_PATH, "a", encoding="utf-8") as f:
                # Add header if file is empty
                if f.tell() == 0 or CONDUCTOR_LOG_PATH.stat().st_size == 0:
                    f.write(
                        "# Conductor Learnings Log\n\n"
                        "Real-time insights from each orchestration run.\n"
                        "Used by AURA to improve future task planning and brain assignments.\n\n"
                    )
                f.write(entry)

            logger.info(
                "conductor_learnings_saved",
                run_id=result.run_id,
                log_path=str(CONDUCTOR_LOG_PATH),
            )
        finally:
            _log_lock.release()

    except Exception as e:
        logger.error("conductor_learnings_failed", error=str(e), exc_info=True)

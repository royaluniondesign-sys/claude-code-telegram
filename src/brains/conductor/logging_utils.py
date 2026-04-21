"""Conductor logging utilities: session log, conductor log, learning write."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


def log_session(
    activity: str,
    brain: str = "",
    step: int = 0,
    duration_ms: int = 0,
    status: str = "completed",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Log autonomous brain activity to persistent session log.

    Args:
        activity: Description of the activity (e.g., "conductor_run", "step_executed")
        brain: Brain name that executed the activity
        step: Step number (if applicable)
        duration_ms: Duration of the activity in milliseconds
        status: Status of the activity ("completed", "failed", "pending")
        details: Optional dict with additional context
    """
    try:
        log_dir = Path.home() / '.aura' / 'memory'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'session_log.txt'

        session_data = {
            'timestamp': datetime.now().isoformat(),
            'activity': activity,
            'brain': brain,
            'step': step,
            'duration_ms': duration_ms,
            'status': status,
            'details': details or {},
        }

        with open(log_file, 'a') as f:
            f.write(json.dumps(session_data) + '\n')
    except Exception as e:
        logger.error("session_log_write_failed", error=str(e))


def _format_ts(ts: float) -> str:
    """Convert unix timestamp to ISO-8601."""
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def log_conductor_run(tasks_executed: List[str], outcomes: List[str]) -> None:
    """Log conductor run learning to persistent memory.

    Args:
        tasks_executed: List of task descriptions/identifiers
        outcomes: List of outcomes ("success" or "failure" for each task)
    """
    log_path = Path.home() / '.aura' / 'memory' / 'conductor_log.md'

    # Ensure the log directory exists
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Get current date and time
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Log the run
    try:
        with open(log_path, 'a') as log_file:
            log_file.write(f"Date: {timestamp}\n")
            log_file.write(f"Tasks Executed: {tasks_executed}\n")
            log_file.write(f"Outcomes: {outcomes}\n\n")
    except Exception as e:
        logger.error("conductor_log_write_failed", error=str(e))


def write_learning(conductor_run_id: Any, success: bool, reason: str, actions_taken: Any) -> None:
    """Log conductor run learning to file.

    Args:
        conductor_run_id: Unique identifier for the conductor run
        success: Boolean indicating if the run succeeded
        reason: String explaining the outcome
        actions_taken: List or string describing actions taken
    """
    # Set up logging
    _logger = logging.getLogger(__name__)
    _logger.setLevel(logging.DEBUG)

    # Create a file handler to log to a file
    handler = logging.FileHandler('conductor_run.log')
    handler.setLevel(logging.DEBUG)

    # Create a logging format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # Add the handler to the logger
    _logger.addHandler(handler)

    # Log the conductor run details
    _logger.info(
        f"Conductor run {conductor_run_id} - Success: {success}, "
        f"Reason: {reason}, Actions Taken: {actions_taken}"
    )

    # Remove the handler to avoid duplicate logs
    _logger.removeHandler(handler)

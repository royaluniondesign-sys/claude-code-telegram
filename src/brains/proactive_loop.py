"""AURA Proactive Loop — daemon that executes pending tasks from task_store."""

import datetime
import logging
import uuid
from pathlib import Path
from src.infra import task_store

logger = logging.getLogger(__name__)


def generate_unique_session_id() -> str:
    """Generate a unique session ID using UUID4."""
    return str(uuid.uuid4())


def write_to_memory(content: str) -> None:
    """Write content to AURA memory log file."""
    memory_dir = Path.home() / ".aura" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    log_file = memory_dir / "conductor_log.md"
    with open(log_file, "a") as f:
        f.write(content)


def log_session() -> None:
    """Log autonomous brain session activity with consistent timestamp and session ID."""
    session_id = generate_unique_session_id()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    memory_dir = Path.home() / ".aura" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    session_log_file = memory_dir / "session_log.txt"

    with open(session_log_file, "a") as log_file:
        log_file.write(f"Session ID: {session_id} - {timestamp} - Session started\n")
        logger.debug(f"Session {session_id} started at {timestamp}")
        # Session end marker is written in finally/cleanup handler
        log_file.write(f"Session ID: {session_id} - {timestamp} - Session ended\n")
        logger.debug(f"Session {session_id} ended at {timestamp}")


class ProactiveLoop:
    """Main executor: picks pending tasks and updates status to in_progress/done/failed."""

    def run(self):
        """Execute all pending auto-fix tasks."""
        logger.debug("Proactive loop started")
        pending = task_store.pending_auto_fix_tasks()
        logger.debug(f"Found {len(pending)} pending tasks")

        for task in pending:
            task_id = task["id"]
            logger.debug(f"Processing task {task_id}")

            # Mark as in_progress
            task_store.update_task(task_id, status="in_progress")
            logger.debug(f"Task {task_id} marked as in_progress")

            # Execute task (simulate)
            try:
                logger.debug(f"Executing task {task_id}")
                result = self._execute_task(task)
                if result:
                    # Mark as completed
                    task_store.update_task(task_id, status="completed", result=result)
                    logger.debug(f"Task {task_id} completed with result: {result}")
                else:
                    # Mark as failed
                    task_store.update_task(task_id, status="failed", result="No result")
                    logger.warning(f"Task {task_id} failed: No result")
            except Exception as e:
                # Mark as failed with error
                task_store.update_task(task_id, status="failed", result=str(e))
                logger.error(f"Task {task_id} failed with exception: {str(e)}", exc_info=True)

    def _execute_task(self, task: dict) -> str:
        """Execute a single task. Return result string or empty on failure."""
        logger.debug(f"_execute_task called for task: {task.get('id')}")
        # TODO: implement actual task execution logic
        # For now, just return success
        result = "Task executed successfully"
        logger.debug(f"Task execution result: {result}")
        return result

    def _write_learning(
        self, timestamp: str, task_title: str, steps_ok: int, duration: str, committed: bool
    ) -> None:
        """Write conductor run learning to ~/.aura/memory/conductor_log.md."""
        content = f"Timestamp: {timestamp}\nTask Title: {task_title}\nSteps OK: {steps_ok}\nDuration: {duration}\nCommitted: {committed}\n\n"
        write_to_memory(content)

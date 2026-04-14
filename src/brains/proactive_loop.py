"""AURA Proactive Loop — daemon that executes pending tasks from task_store."""

import logging
from src.infra import task_store

logger = logging.getLogger(__name__)


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

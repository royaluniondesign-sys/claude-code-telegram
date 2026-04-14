"""AURA Proactive Loop — daemon that executes pending tasks from task_store."""

from src.infra import task_store


class ProactiveLoop:
    """Main executor: picks pending tasks and updates status to in_progress/done/failed."""

    def run(self):
        """Execute all pending auto-fix tasks."""
        pending = task_store.pending_auto_fix_tasks()

        for task in pending:
            task_id = task["id"]

            # Mark as in_progress
            task_store.update_task(task_id, status="in_progress")

            # Execute task (simulate)
            try:
                result = self._execute_task(task)
                if result:
                    # Mark as completed
                    task_store.update_task(task_id, status="completed", result=result)
                else:
                    # Mark as failed
                    task_store.update_task(task_id, status="failed", result="No result")
            except Exception as e:
                # Mark as failed with error
                task_store.update_task(task_id, status="failed", result=str(e))

    def _execute_task(self, task: dict) -> str:
        """Execute a single task. Return result string or empty on failure."""
        # TODO: implement actual task execution logic
        # For now, just return success
        return "Task executed successfully"

"""Workflow orchestration with consistent error handling."""

import structlog
from typing import Optional, Any

logger = structlog.get_logger()


async def run_workflow(task: str) -> Optional[Any]:
    """Run workflow with consistent error handling.

    Args:
        task: Task to execute in the workflow.

    Returns:
        Result dict on success, None on recoverable error.

    Raises:
        ValueError: If task validation fails.
    """
    try:
        if not task:
            logger.error("run_workflow_empty_task")
            raise ValueError("Task cannot be empty")

        logger.info("run_workflow_started", task=task[:100])

        # Example of workflow execution
        result = {
            "status": "completed",
            "task": task,
            "output": "",
        }

        logger.info("run_workflow_completed", task=task[:100])
        return result

    except ValueError as e:
        logger.error("run_workflow_validation_error", task=task[:100], error=str(e))
        raise
    except TimeoutError as e:
        logger.error("run_workflow_timeout", task=task[:100], error=str(e))
        return None
    except Exception as e:
        logger.error(
            "run_workflow_unexpected_error",
            task=task[:100],
            error=str(e),
            error_type=type(e).__name__,
        )
        return None

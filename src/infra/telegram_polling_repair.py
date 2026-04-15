"""Self-repairing Telegram polling with exponential backoff.

Handles transient polling failures by automatically retrying with increasing
delays. If polling fails too many times, raises an exception to trigger
full bot restart.
"""

import asyncio
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

# Configuration
MAX_RETRIES = 5
INITIAL_BACKOFF = 5  # seconds
MAX_BACKOFF = 300  # max 5 minutes between retries
BACKOFF_MULTIPLIER = 2.0


async def polling_with_self_repair(
    polling_fn: Callable,
    error_callback: Optional[Callable] = None,
) -> None:
    """Start polling with automatic retry on failure.

    Args:
        polling_fn: Async function that starts polling (e.g., app.updater.start_polling())
        error_callback: Optional async callback for logging retry attempts

    Raises:
        Exception: If max retries exceeded
    """
    retry_count = 0
    backoff = INITIAL_BACKOFF

    while retry_count < MAX_RETRIES:
        try:
            logger.info("telegram_polling_start", attempt=retry_count + 1)
            await polling_fn()
            # If polling completes normally, we're done
            logger.info("telegram_polling_completed")
            break
        except asyncio.CancelledError:
            # Graceful shutdown — don't retry
            logger.info("telegram_polling_cancelled")
            raise
        except Exception as e:
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                logger.error(
                    "telegram_polling_max_retries_exceeded",
                    attempts=retry_count,
                    error=str(e),
                )
                raise Exception(
                    f"Telegram polling failed after {MAX_RETRIES} attempts: {str(e)}"
                ) from e

            # Calculate backoff for next retry
            next_backoff = min(backoff, MAX_BACKOFF)
            logger.warning(
                "telegram_polling_retry",
                attempt=retry_count,
                next_retry_in_s=next_backoff,
                error=str(e),
            )

            # Invoke error callback if provided (for notifications, metrics, etc.)
            if error_callback:
                try:
                    result = error_callback(retry_count, next_backoff, str(e))
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as cb_err:
                    logger.warning("polling_repair_callback_error", error=str(cb_err))

            # Wait before retrying
            await asyncio.sleep(next_backoff)
            backoff = int(backoff * BACKOFF_MULTIPLIER)

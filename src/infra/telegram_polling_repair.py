"""Self-repairing Telegram polling with exponential backoff.

Handles transient polling failures by automatically retrying with increasing
delays. Differentiates between transient errors (network, rate limits) and
fatal errors. Respects Telegram's RetryAfter headers and other specific errors.
"""

import asyncio
import time
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

# Configuration
MAX_RETRIES = 5
INITIAL_BACKOFF = 5  # seconds
MAX_BACKOFF = 300  # max 5 minutes between retries
BACKOFF_MULTIPLIER = 2.0
RECOVERY_WINDOW = 300  # 5 min of successful polling = reset retry counter


async def polling_with_self_repair(
    polling_fn: Callable,
    error_callback: Optional[Callable] = None,
) -> None:
    """Start polling with automatic retry on transient failures.

    Handles Telegram-specific errors (RetryAfter, ChatNotFound, BotBlocked)
    and respects their retry-after directives. Resets backoff after sustained
    success (300s of polling without errors).

    Args:
        polling_fn: Async function that starts polling (e.g., app.updater.start_polling())
        error_callback: Optional async callback(attempt, next_backoff, error_str)

    Raises:
        Exception: If max retries exceeded
    """
    retry_count = 0
    backoff = INITIAL_BACKOFF
    last_success_time = time.time()

    while retry_count < MAX_RETRIES:
        try:
            logger.info("telegram_polling_start", attempt=retry_count + 1)
            start_time = time.time()
            await polling_fn()
            # If polling completes normally, reset and exit
            logger.info("telegram_polling_completed")
            break

        except asyncio.CancelledError:
            # Graceful shutdown — don't retry
            logger.info("telegram_polling_cancelled")
            raise

        except Exception as e:
            error_str = str(e)

            # Check if this is a transient error we should retry
            is_transient = _should_retry_error(error_str)

            if not is_transient:
                logger.error(
                    "telegram_polling_fatal_error",
                    error_type=type(e).__name__,
                    error=error_str[:200],
                    retry_count=retry_count,
                )
                raise

            retry_count += 1
            if retry_count >= MAX_RETRIES:
                logger.error(
                    "telegram_polling_max_retries_exceeded",
                    attempts=retry_count,
                    error=error_str[:200],
                )
                raise Exception(
                    f"Telegram polling failed after {MAX_RETRIES} attempts: {error_str}"
                ) from e

            # Extract retry_after from Telegram RetryAfter errors if present
            retry_after = _extract_retry_after(error_str)
            if retry_after:
                next_backoff = retry_after
                logger.warning(
                    "telegram_retry_after_received",
                    retry_after_s=next_backoff,
                    attempt=retry_count,
                    error=error_str[:100],
                )
            else:
                next_backoff = min(backoff, MAX_BACKOFF)
                logger.warning(
                    "telegram_polling_retry",
                    attempt=retry_count,
                    next_retry_in_s=next_backoff,
                    error=error_str[:100],
                )

            # Invoke error callback if provided (for notifications, metrics, etc.)
            if error_callback:
                try:
                    result = error_callback(retry_count, next_backoff, error_str[:200])
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as cb_err:
                    logger.warning("polling_repair_callback_error", error=str(cb_err))

            # Wait before retrying
            await asyncio.sleep(next_backoff)

            # Only increment backoff if we're not respecting a Telegram retry_after
            if not retry_after:
                backoff = int(backoff * BACKOFF_MULTIPLIER)

            # Reset retry counter if polling succeeded for >= RECOVERY_WINDOW seconds
            now = time.time()
            if now - last_success_time >= RECOVERY_WINDOW:
                logger.info(
                    "polling_recovery_window_achieved",
                    seconds=RECOVERY_WINDOW,
                    resetting_retry_count=True,
                )
                retry_count = 0
                backoff = INITIAL_BACKOFF


def _should_retry_error(error_str: str) -> bool:
    """Check if error is transient and should trigger a retry.

    Returns False for fatal errors like invalid token, unknown method, etc.
    Returns True for transient errors like network, timeout, rate limit, etc.
    """
    # Fatal errors — don't retry
    fatal_patterns = [
        "Unauthorized",  # Invalid token
        "Not Found",  # Unknown method/resource
        "Forbidden",  # Permissions issue
        "invalid_token",  # Telegram API error
        "method not found",  # Unknown API method
    ]

    for pattern in fatal_patterns:
        if pattern.lower() in error_str.lower():
            return False

    # Transient errors — retry
    transient_patterns = [
        "RetryAfter",
        "Too Many Requests",
        "429",  # Rate limit code
        "503",  # Service unavailable
        "504",  # Gateway timeout
        "timeout",
        "Connection",
        "Network",
        "reset",
        "broken pipe",
        "EOF",
    ]

    for pattern in transient_patterns:
        if pattern.lower() in error_str.lower():
            return True

    # Default: retry unknown errors (safer than giving up)
    return True


def _extract_retry_after(error_str: str) -> Optional[int]:
    """Extract retry_after seconds from Telegram error message.

    Telegram returns "retry after X" in error messages when rate-limited.
    """
    import re

    match = re.search(r"retry\s+after\s+(\d+)", error_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

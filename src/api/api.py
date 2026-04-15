"""API client with consistent error handling."""

import structlog
from typing import Optional, Any

logger = structlog.get_logger()


def api_call(endpoint: str, timeout: int = 30) -> Optional[Any]:
    """Make API call with consistent error handling.

    Args:
        endpoint: API endpoint to call.
        timeout: Request timeout in seconds.

    Returns:
        Response dict on success, None on recoverable error.

    Raises:
        ValueError: If endpoint validation fails.
    """
    try:
        if not endpoint:
            logger.error("api_call_empty_endpoint")
            raise ValueError("Endpoint cannot be empty")

        # Code to make API call
        response = {"status": 200, "data": {}}

        logger.debug("api_call_success", endpoint=endpoint, status=response.get("status"))
        return response

    except TimeoutError as e:
        logger.error("api_call_timeout", endpoint=endpoint, timeout_seconds=timeout, error=str(e))
        return None
    except ConnectionError as e:
        logger.error("api_call_connection_error", endpoint=endpoint, error=str(e))
        return None
    except ValueError as e:
        logger.error("api_call_validation_error", endpoint=endpoint, error=str(e))
        raise
    except Exception as e:
        logger.error(
            "api_call_unexpected_error",
            endpoint=endpoint,
            error=str(e),
            error_type=type(e).__name__,
        )
        return None

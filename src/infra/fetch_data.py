"""Data fetching with consistent error handling."""

import structlog
from typing import Optional, Any

logger = structlog.get_logger()


def fetch_data(timeout: int = 30) -> Optional[Any]:
    """Fetch data from external source with consistent error handling.

    Returns:
        Data dict on success, None on recoverable error.

    Raises:
        ValueError: If data validation fails.
    """
    try:
        # Code to fetch data
        data = {"status": "success", "records": []}

        if data is None:
            logger.warning("fetch_data_returned_none")
            return None

        logger.debug("fetch_data_success", record_count=len(data.get("records", [])))
        return data

    except TimeoutError as e:
        logger.error("fetch_data_timeout", timeout_seconds=timeout, error=str(e))
        return None
    except ConnectionError as e:
        logger.error("fetch_data_connection_error", error=str(e))
        return None
    except ValueError as e:
        logger.error("fetch_data_validation_error", error=str(e))
        raise
    except Exception as e:
        logger.error("fetch_data_unexpected_error", error=str(e), error_type=type(e).__name__)
        return None

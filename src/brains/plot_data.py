"""Data visualization with consistent error handling."""

import structlog
from typing import Optional, Any

logger = structlog.get_logger()


def plot_data(data: Optional[Any]) -> None:
    """Plot data with consistent error handling.

    Args:
        data: Data to plot, must be dict-like.

    Raises:
        ValueError: If data is invalid for plotting.
        TypeError: If data type is unsupported.
    """
    try:
        if data is None:
            logger.warning("plot_data_received_none")
            raise ValueError("Cannot plot None data")

        if not isinstance(data, dict):
            logger.error("plot_data_invalid_type", data_type=type(data).__name__)
            raise TypeError(f"Expected dict, got {type(data).__name__}")

        # Code to plot data
        record_count = len(data.get("records", []))
        logger.info("plot_data_success", record_count=record_count)

    except ValueError as e:
        logger.error("plot_data_validation_error", error=str(e))
        raise
    except TypeError as e:
        logger.error("plot_data_type_error", error=str(e))
        raise
    except Exception as e:
        logger.error("plot_data_unexpected_error", error=str(e), error_type=type(e).__name__)
        raise

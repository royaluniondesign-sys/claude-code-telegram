"""Conductor singleton: _conductor global, get_conductor, set_conductor."""
from __future__ import annotations

from typing import Any, Callable, Optional

import structlog

logger = structlog.get_logger()

_conductor: Optional[Any] = None  # type: Optional["Conductor"]


def get_conductor(brain_router: Any = None, notify_fn: Any = None) -> Optional[Any]:
    """Return global conductor. Creates one if brain_router is supplied."""
    global _conductor
    if _conductor is None and brain_router is not None:
        from .orchestrator import Conductor
        _conductor = Conductor(brain_router, notify_fn=notify_fn)
        logger.info("conductor_initialized")
    return _conductor


def set_conductor(conductor: Any) -> None:
    global _conductor
    _conductor = conductor

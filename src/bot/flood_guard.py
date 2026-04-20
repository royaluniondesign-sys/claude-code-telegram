"""Global Telegram flood guard.

Shared state across the entire bot process. When any Telegram API call gets
a 429 Too Many Requests, call `set_flood_wait(seconds)` to pause all sends.
State is persisted to ~/.aura/flood_wait.txt so it survives restarts.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import structlog

logger = structlog.get_logger()

_FLOOD_FILE = Path.home() / ".aura" / "flood_wait.txt"
_flood_wait_until: float = 0.0
_initialized: bool = False


def _load_from_file() -> None:
    """Load persisted flood wait from file on first use."""
    global _flood_wait_until, _initialized
    if _initialized:
        return
    _initialized = True
    try:
        if _FLOOD_FILE.exists():
            val = float(_FLOOD_FILE.read_text().strip())
            if val > time.time():
                _flood_wait_until = val
                remaining = int(val - time.time())
                logger.info("flood_guard_loaded", remaining_s=remaining)
    except Exception:
        pass


def set_flood_wait(retry_after_seconds: int) -> None:
    """Record a flood ban. Call this whenever 429 is received."""
    global _flood_wait_until
    _load_from_file()
    _flood_wait_until = time.time() + retry_after_seconds
    try:
        _FLOOD_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLOOD_FILE.write_text(str(_flood_wait_until))
    except Exception:
        pass
    logger.warning(
        "telegram_flood_ban",
        retry_after_s=retry_after_seconds,
        expires_in_min=round(retry_after_seconds / 60, 1),
    )


def remaining_flood_wait() -> float:
    """Return seconds remaining in flood ban, 0.0 if not banned."""
    _load_from_file()
    remaining = _flood_wait_until - time.time()
    return max(0.0, remaining)


def extract_retry_after(error: str) -> int | None:
    """Parse retry_after seconds from a Telegram error message."""
    m = re.search(r"retry.?after\s+(\d+)", error, re.I)
    return int(m.group(1)) if m else None


@asynccontextmanager
async def flood_guard(max_wait: float = 30.0) -> AsyncGenerator[None, None]:
    """Context manager: wait out flood ban (up to max_wait), then yield.

    Drops the operation silently if ban is longer than max_wait.
    """
    remaining = remaining_flood_wait()
    if remaining > 0:
        if remaining > max_wait:
            logger.info("flood_guard_drop", remaining_s=remaining)
            return
        logger.info("flood_guard_wait", remaining_s=remaining)
        await asyncio.sleep(remaining + 0.5)
    try:
        yield
    except Exception as exc:
        err = str(exc)
        if "429" in err or "Too Many Requests" in err:
            retry_after = extract_retry_after(err) or 60
            set_flood_wait(retry_after)
            logger.warning("flood_guard_caught_429", retry_after=retry_after)
        else:
            raise

"""Event bus for orchestration pub/sub (SSE clients)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List


_subscribers: List[asyncio.Queue] = []


def orch_subscribe() -> asyncio.Queue:
    """Subscribe to orchestration events. Returns a queue to read from."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)
    return q


def orch_unsubscribe(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


async def _broadcast(event: Dict[str, Any]) -> None:
    """Broadcast event to all SSE subscribers (non-blocking)."""
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)  # slow / disconnected client
    for q in dead:
        orch_unsubscribe(q)

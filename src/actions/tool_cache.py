"""TTL cache for tool results — avoids redundant tool calls in same session.

Same tool + same args within TTL → return cached result (0 tokens, 0 latency).

Default TTLs:
  get_aura_status   → 30s  (changes infrequently)
  bash_run          → 5s   (commands can have side effects, keep short)
  file_read         → 15s  (files change, but usually not mid-conversation)
  memory_search     → 60s  (vector DB, expensive + stable in short windows)
  git_status        → 10s
  get_terminal_url  → 120s (URL doesn't change often)
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional, Tuple

_TOOL_TTL: Dict[str, int] = {
    "get_aura_status":  30,
    "bash_run":          5,
    "file_read":        15,
    "file_list":        15,
    "memory_search":    60,
    "memory_store":      0,   # Never cache writes
    "git_status":       10,
    "git_log":          30,
    "git_commit":        0,   # Never cache writes
    "file_write":        0,   # Never cache writes
    "send_email":        0,   # Never cache sends
    "get_terminal_url": 120,
}
_DEFAULT_TTL = 10

_cache: Dict[str, Tuple[Any, float]] = {}   # key → (result, expires_at)


def _cache_key(tool_name: str, kwargs: Dict[str, Any]) -> str:
    payload = f"{tool_name}:{sorted(kwargs.items())}"
    return hashlib.md5(payload.encode()).hexdigest()


def get_cached(tool_name: str, **kwargs: Any) -> Optional[Any]:
    """Return cached result if still valid, else None."""
    ttl = _TOOL_TTL.get(tool_name, _DEFAULT_TTL)
    if ttl == 0:
        return None  # Never cache this tool

    key = _cache_key(tool_name, kwargs)
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def set_cached(tool_name: str, result: Any, **kwargs: Any) -> None:
    """Cache a tool result."""
    ttl = _TOOL_TTL.get(tool_name, _DEFAULT_TTL)
    if ttl == 0:
        return
    key = _cache_key(tool_name, kwargs)
    _cache[key] = (result, time.time() + ttl)


def purge_expired() -> int:
    """Remove expired entries. Returns count removed."""
    now = time.time()
    expired = [k for k, (_, exp) in _cache.items() if exp < now]
    for k in expired:
        del _cache[k]
    return len(expired)

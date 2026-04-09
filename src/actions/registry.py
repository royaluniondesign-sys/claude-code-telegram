"""Tool registry — auto-discovers tools from src/actions/tools/*.py.

Each tool module exports functions decorated with @aura_tool.
The registry loads them at startup and makes them available to:
  - Orchestrator (native execution, no subprocess)
  - MCP server (Claude Desktop / Cursor)
  - Self-healer (diagnostic tools)

Adding a new capability = drop a .py file in tools/, no other changes.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Awaitable[Any]]
    parameters: Dict[str, Any]      # JSON-schema style {"param": {"type":..,"desc":..}}
    category: str = "general"       # email / system / memory / web / files / git


# ── Decorator ─────────────────────────────────────────────────────────────────

def aura_tool(
    name: str | None = None,
    description: str = "",
    category: str = "general",
    parameters: Dict[str, Any] | None = None,
):
    """Decorate an async function to register it as an AURA tool.

    Example:
        @aura_tool(name="send_email", category="email",
                   description="Send email via Resend",
                   parameters={"to": {"type": "str"}, "subject": {"type": "str"},
                                "body": {"type": "str"}})
        async def send_email(to: str, subject: str, body: str) -> str:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        desc = description or (fn.__doc__ or "").strip().split("\n")[0]

        # Auto-derive parameter schema from type hints if not provided
        params = parameters or {}
        if not params:
            hints = fn.__annotations__.copy()
            hints.pop("return", None)
            for param_name, hint in hints.items():
                params[param_name] = {
                    "type": getattr(hint, "__name__", str(hint)),
                    "description": param_name.replace("_", " "),
                }

        spec = ToolSpec(
            name=tool_name,
            description=desc,
            fn=fn,
            parameters=params,
            category=category,
        )
        _REGISTRY[tool_name] = spec
        logger.debug("tool_registered", name=tool_name, category=category)
        return fn
    return decorator


# ── Registry store ─────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, ToolSpec] = {}
_loaded = False


def _load_tools() -> None:
    """Import all modules in src/actions/tools/ to trigger @aura_tool decorators."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    tools_pkg = Path(__file__).parent / "tools"
    if not tools_pkg.exists():
        logger.warning("tools_dir_missing", path=str(tools_pkg))
        return

    import src.actions.tools as _pkg_tools
    for _, module_name, _ in pkgutil.iter_modules([str(tools_pkg)]):
        try:
            importlib.import_module(f"src.actions.tools.{module_name}")
            logger.debug("tools_module_loaded", module=module_name)
        except Exception as e:
            logger.error("tools_module_error", module=module_name, error=str(e))


# ── Public API ─────────────────────────────────────────────────────────────────

def registry() -> Dict[str, ToolSpec]:
    _load_tools()
    return _REGISTRY


def list_tools() -> List[str]:
    _load_tools()
    return sorted(_REGISTRY.keys())


def get_tool(name: str) -> Optional[ToolSpec]:
    _load_tools()
    return _REGISTRY.get(name)


async def call_tool(name: str, **kwargs: Any) -> Any:
    """Call a registered tool by name, with TTL caching for read operations.

    Raises KeyError if tool not found.
    """
    _load_tools()
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"Unknown tool: '{name}'. Available: {list_tools()}")

    # Transparent cache — callers never need to change
    from src.actions.tool_cache import get_cached, set_cached
    cached = get_cached(name, **kwargs)
    if cached is not None:
        logger.debug("tool_cache_hit", name=name)
        return cached

    t0 = time.time()
    try:
        if inspect.iscoroutinefunction(spec.fn):
            result = await spec.fn(**kwargs)
        else:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: spec.fn(**kwargs))
        elapsed = int((time.time() - t0) * 1000)
        logger.info("tool_called", name=name, elapsed_ms=elapsed)
        set_cached(name, result, **kwargs)
        return result
    except Exception as e:
        logger.error("tool_error", name=name, error=str(e))
        raise

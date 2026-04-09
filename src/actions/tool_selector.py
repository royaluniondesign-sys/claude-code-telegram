"""Intent-based tool selector — minimizes token cost per request.

The MCP token problem:
  12 tools × ~60 tokens/tool = 720 tokens injected on EVERY call.
  For 200 messages/day → 144,000 wasted tokens/day.

Solution: only inject tools the brain actually needs for the detected intent.

Token cost tiers:
  ZERO  (0 tokens)  — native action, no brain call (email, bash for known patterns)
  SLIM  (~60 tokens) — 1 tool injected (email compose, status query)  
  LEAN  (~200 tokens) — 2-3 tools injected (report + email)
  FULL  (~720 tokens) — all tools (complex multi-step orchestration)
"""
from __future__ import annotations
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from src.actions.registry import ToolSpec

# ── Intent → tool mapping ──────────────────────────────────────────────────────
# Key = intent value string (matches Intent enum .value)
# Value = list of tool names to inject — ordered by relevance
_INTENT_TOOLS: dict[str, List[str]] = {
    # Native-handled — brain only needs result formatting tools
    "email":     ["send_email", "get_aura_status"],
    "bash":      ["bash_run"],
    "files":     ["file_read", "file_list", "file_write"],
    "git":       ["git_status", "git_log", "git_commit"],
    "memory":    ["memory_search", "memory_store"],

    # Web/search — external brains handle this, no local tools needed
    "search":    [],
    "translate": [],

    # Chat — no tools, just the brain
    "chat":      [],

    # Code tasks — bash for running code
    "code":      ["bash_run", "file_read", "file_write"],

    # Complex analysis / planning — minimal context tools
    "deep":      ["get_aura_status", "memory_search", "bash_run"],

    # Multi-step orchestration (explicitly requested)
    "orchestrate": [],  # use full set — see select_tools()
}

# Tools never injected into free/small models (too expensive context-wise)
_EXCLUDE_FROM_FREE_MODELS = {"git_commit", "file_write"}


def select_tools(
    intent_value: str,
    model_tier: str = "standard",   # "free" | "standard" | "premium"
    max_tools: int = 3,
    full: bool = False,
) -> List[str]:
    """Return minimal tool list for the given intent.

    Args:
        intent_value: Intent enum value string (e.g. "email", "bash")
        model_tier: Controls exclusion of write tools from free models
        max_tools: Hard cap on number of tools injected
        full: If True, return all tools (complex orchestration)

    Returns:
        Ordered list of tool names to inject (empty = no tools)
    """
    if full:
        from src.actions.registry import list_tools
        all_tools = list_tools()
        if model_tier == "free":
            return [t for t in all_tools if t not in _EXCLUDE_FROM_FREE_MODELS][:max_tools * 2]
        return all_tools

    tools = _INTENT_TOOLS.get(intent_value, [])

    if model_tier == "free":
        tools = [t for t in tools if t not in _EXCLUDE_FROM_FREE_MODELS]

    return tools[:max_tools]


def estimate_token_cost(tool_names: List[str]) -> int:
    """Rough estimate of tokens consumed by tool descriptions."""
    # ~60 tokens per tool definition (name + description + params)
    return len(tool_names) * 60


def format_tools_for_prompt(tool_names: List[str]) -> str:
    """Format tool specs as a compact system prompt injection.

    Used when the brain doesn't support native MCP tool calls
    (e.g. openrouter free models) — tools are described in text,
    brain responds with JSON tool call, orchestrator executes.
    """
    from src.actions.registry import get_tool
    if not tool_names:
        return ""

    lines = ["You have these tools available. Call them as JSON: {\"tool\": \"name\", \"args\": {...}}"]
    for name in tool_names:
        spec = get_tool(name)
        if spec:
            params = ", ".join(
                f"{k}: {v.get('type', 'str')}"
                for k, v in spec.parameters.items()
            )
            lines.append(f"  {name}({params}) — {spec.description}")
    return "\n".join(lines)

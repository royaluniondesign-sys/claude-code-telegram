"""Memory tools — search and store facts in AURA's persistent vector memory."""
from __future__ import annotations
from src.actions.registry import aura_tool


@aura_tool(
    name="memory_search",
    description="Search AURA's persistent memory for relevant facts.",
    category="memory",
    parameters={"query": {"type": "str", "description": "What to search for"}},
)
async def memory_search(query: str) -> str:
    from src.context.mempalace_memory import search_memories, format_memories_for_prompt
    memories = await search_memories(query, n=6)
    if not memories:
        return "No memories found for that query."
    return format_memories_for_prompt(memories)


@aura_tool(
    name="memory_store",
    description="Store a fact or note in AURA's persistent memory.",
    category="memory",
    parameters={
        "user_message":  {"type": "str", "description": "What the user said"},
        "aura_response": {"type": "str", "description": "What AURA responded / learned"},
    },
)
async def memory_store(user_message: str, aura_response: str) -> str:
    from src.context.mempalace_memory import store_interaction
    await store_interaction(user_message, aura_response)
    return "✅ Stored in memory."

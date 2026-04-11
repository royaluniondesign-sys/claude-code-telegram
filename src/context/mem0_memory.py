"""Mem0 memory layer — persistent searchable memory for AURA.

Stores facts extracted from conversations across sessions.
Uses fastembed for local embeddings (no API cost).
Uses Ollama (qwen2.5:7b) for fact extraction, falls back to OpenRouter.

Memory is stored in ~/.aura/mem0/ (Qdrant local + SQLite history).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_MEM0_DIR = Path.home() / ".aura" / "mem0"
_USER_ID = "ricardo"  # single-user bot

_memory: Optional[Any] = None
_memory_ready = False


def _build_memory_config() -> Any:
    """Build MemoryConfig using local embeddings + Ollama/OpenRouter LLM."""
    from mem0 import Memory
    from mem0.configs.base import MemoryConfig, LlmConfig, EmbedderConfig, VectorStoreConfig

    _MEM0_DIR.mkdir(parents=True, exist_ok=True)
    qdrant_path = str(_MEM0_DIR / "qdrant")

    # Try Ollama first (free local), fallback to OpenRouter
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    auth_path = Path.home() / ".local/share/opencode/auth.json"
    if not openrouter_key and auth_path.exists():
        try:
            import json
            data = json.loads(auth_path.read_text())
            openrouter_key = data.get("openrouter", {}).get("key")
        except Exception:
            pass

    # LLM: OpenRouter via LiteLLM — set env var so litellm picks it up
    if openrouter_key:
        os.environ["OPENROUTER_API_KEY"] = openrouter_key
        # Use a model that supports function calling (required by Mem0)
        # qwen-2.5-7b:free does NOT support function calling → use mistral or llama
        llm_config = LlmConfig(
            provider="litellm",
            config={
                "model": "openrouter/mistralai/mistral-7b-instruct:free",
                "api_key": openrouter_key,
                "temperature": 0.1,
                "max_tokens": 1000,
            },
        )
        logger.info("mem0_llm_openrouter")
    else:
        # No key — dummy config (store_interaction will fail gracefully, search still works)
        llm_config = LlmConfig(
            provider="openai",
            config={
                "model": "gpt-4.1-nano-2025-04-14",
                "api_key": "dummy",
            },
        )
        logger.warning("mem0_no_llm_key")

    config = MemoryConfig(
        vector_store=VectorStoreConfig(
            provider="qdrant",
            config={
                "collection_name": "aura_memory",
                "embedding_model_dims": 384,  # matches BAAI/bge-small-en-v1.5
                "path": qdrant_path,
                "on_disk": True,
            },
        ),
        llm=llm_config,
        embedder=EmbedderConfig(
            provider="fastembed",
            config={
                "model": "BAAI/bge-small-en-v1.5",
            },
        ),
        history_db_path=str(_MEM0_DIR / "history.db"),
        version="v1.1",
    )
    return Memory(config=config)


def _init_memory_sync() -> None:
    """Initialize Mem0 synchronously."""
    global _memory, _memory_ready
    try:
        _memory = _build_memory_config()
        _memory_ready = True
        logger.info("mem0_ready", path=str(_MEM0_DIR))
    except Exception as e:
        logger.warning("mem0_init_failed", error=str(e))
        _memory = None
        _memory_ready = True  # stop retrying


async def ensure_memory_initialized() -> None:
    """Pre-warm Mem0 at bot startup."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_memory_sync)


def _get_memory() -> Optional[Any]:
    if _memory_ready:
        return _memory
    return None


async def search_memories(query: str, limit: int = 5) -> list[str]:
    """Search relevant memories for a query. Returns list of memory strings."""
    mem = _get_memory()
    if mem is None:
        return []
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: mem.search(query, user_id=_USER_ID, limit=limit),
        )
        memories = []
        if isinstance(results, dict):
            results = results.get("results", [])
        for r in results:
            if isinstance(r, dict):
                text = r.get("memory", "") or r.get("text", "")
            else:
                text = str(r)
            if text:
                memories.append(text)
        return memories
    except Exception as e:
        logger.warning("mem0_search_error", error=str(e))
        return []


async def store_interaction(user_message: str, assistant_response: str) -> None:
    """Extract and store facts from a conversation turn."""
    mem = _get_memory()
    if mem is None:
        return
    try:
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_response},
        ]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: mem.add(messages, user_id=_USER_ID),
        )
        logger.debug("mem0_stored")
    except Exception as e:
        logger.warning("mem0_store_error", error=str(e))


async def get_all_memories(limit: int = 20) -> list[str]:
    """Retrieve all stored memories (for /memory command)."""
    mem = _get_memory()
    if mem is None:
        return []
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: mem.get_all(user_id=_USER_ID, limit=limit),
        )
        if isinstance(results, dict):
            results = results.get("results", [])
        memories = []
        for r in results:
            if isinstance(r, dict):
                text = r.get("memory", "") or r.get("text", "")
            else:
                text = str(r)
            if text:
                memories.append(text)
        return memories
    except Exception as e:
        logger.warning("mem0_get_all_error", error=str(e))
        return []


async def delete_all_memories() -> bool:
    """Clear all memories (for /memory clear command)."""
    mem = _get_memory()
    if mem is None:
        return False
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: mem.delete_all(user_id=_USER_ID),
        )
        logger.info("mem0_cleared")
        return True
    except Exception as e:
        logger.warning("mem0_clear_error", error=str(e))
        return False


def format_memories_for_prompt(memories: list[str]) -> str:
    """Format memories as a context block for system prompts."""
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\n## Mem0 — Relevant context from memory:\n{lines}\n"

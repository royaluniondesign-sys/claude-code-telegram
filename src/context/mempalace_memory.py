"""MemPalace memory layer — persistent verbatim semantic memory for AURA.

Replaces Mem0 with ChromaDB-backed storage via MemPalace.
No LLM required for indexing — stores raw conversation exchanges verbatim.
96.6% LongMemEval accuracy vs Mem0's ~70%.

Memory stored at ~/.aura/palace/ (ChromaDB on disk).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

_PALACE_DIR = Path.home() / ".aura" / "palace"
_WING = "aura"  # namespace within palace
# ChromaDB default metric is L2 (Euclidean). Distances range 0.0 (exact) to ~2.0 (unrelated).
# We filter out only truly garbage matches (dist > 1.6 = completely off-topic).
_MAX_L2_DIST = 1.6

# Suppress ONNX CoreML on Apple Silicon (prevents warnings + crash risk)
os.environ.setdefault("ORT_DISABLE_COREML", "1")

_collection: Optional[Any] = None


def _get_collection() -> Optional[Any]:
    """Lazy-init ChromaDB collection via MemPalace's palace module."""
    global _collection
    if _collection is not None:
        return _collection
    try:
        from mempalace.palace import get_collection

        _PALACE_DIR.mkdir(parents=True, exist_ok=True)
        _collection = get_collection(str(_PALACE_DIR))
        logger.info("mempalace_ready", path=str(_PALACE_DIR), count=_collection.count())
        return _collection
    except Exception as e:
        logger.warning("mempalace_init_failed", error=str(e))
        return None


_GARBAGE_MARKERS = ("[Conversación reciente]", "[Tú]: [Conversación")


async def prewarm() -> None:
    """Pre-warm ChromaDB collection in a thread so first-message latency is avoided."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _get_collection)
    logger.info("mempalace_prewarmed")


async def store_interaction(user_message: str, assistant_response: str) -> None:
    """Store a conversation exchange verbatim into the palace.

    Stores as a single chunk: user turn + assistant summary.
    No LLM extraction required — verbatim is the point.
    """
    # Skip storing compressed/garbage context — prevents feedback loop
    if any(marker in user_message for marker in _GARBAGE_MARKERS):
        logger.debug("mempalace_skip_garbage", reason="compressed_context")
        return

    # Build verbatim text chunk
    user_short = user_message[:300].strip()
    assistant_short = assistant_response[:500].strip()
    text = f"[Usuario]: {user_short}\n[AURA]: {assistant_short}"

    doc_id = str(uuid.uuid4())
    metadata: dict[str, Any] = {
        "wing": _WING,
        "room": _detect_room(user_message),
        "source_file": "telegram_chat",
        "ts": time.time(),
    }

    def _store() -> None:
        col = _get_collection()
        if col is None:
            return
        col.add(documents=[text], ids=[doc_id], metadatas=[metadata])

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _store)
        logger.debug("mempalace_stored", room=metadata["room"])
    except Exception as e:
        logger.warning("mempalace_store_error", error=str(e))


async def search_memories(query: str, n: int = 5) -> list[str]:
    """Semantic search against the palace. Returns relevant verbatim exchanges."""
    def _search() -> list | None:
        col = _get_collection()
        if col is None or col.count() == 0:
            return None
        return col.query(
            query_texts=[query],
            n_results=min(n, col.count()),
            include=["documents", "metadatas", "distances"],
            where={"wing": _WING},
        )

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _search)
        if results is None:
            return []

        docs = results["documents"][0]
        dists = results["distances"][0]

        hits: list[str] = []
        for doc, dist in zip(docs, dists):
            if dist <= _MAX_L2_DIST and not any(m in doc for m in _GARBAGE_MARKERS):
                hits.append(doc)

        return hits

    except Exception as e:
        logger.warning("mempalace_search_error", error=str(e))
        return []


async def get_all_memories(limit: int = 20) -> list[str]:
    """Retrieve recent memories (for /memory command)."""
    def _get_all() -> tuple | None:
        col = _get_collection()
        if col is None or col.count() == 0:
            return None
        # ChromaDB doesn't sort by time natively — get all and slice
        results = col.get(
            where={"wing": _WING},
            include=["documents", "metadatas"],
            limit=limit,
        )
        return results.get("documents", []), results.get("metadatas", [])

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _get_all)
        if data is None:
            return []
        docs, metas = data
        pairs = sorted(
            zip(docs, metas),
            key=lambda x: x[1].get("ts", 0),
            reverse=True,
        )
        return [doc for doc, _ in pairs[:limit]]
    except Exception as e:
        logger.warning("mempalace_get_all_error", error=str(e))
        return []


async def delete_all_memories() -> bool:
    """Clear all palace memories."""
    global _collection

    def _delete_all() -> int:
        col = _get_collection()
        if col is None:
            return 0
        results = col.get(where={"wing": _WING}, include=[])
        ids = results.get("ids", [])
        if ids:
            col.delete(ids=ids)
        return len(ids)

    try:
        loop = asyncio.get_event_loop()
        deleted = await loop.run_in_executor(None, _delete_all)
        logger.info("mempalace_cleared", deleted=deleted)
        return True
    except Exception as e:
        logger.warning("mempalace_clear_error", error=str(e))
        return False


async def palace_count() -> int:
    """Return total document count in the palace."""
    def _count() -> int:
        col = _get_collection()
        if col is None:
            return 0
        return col.count()

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _count)
    except Exception:
        return 0


def format_memories_for_prompt(memories: list[str]) -> str:
    """Format memories as context block for system prompts."""
    if not memories:
        return ""
    lines = "\n".join(f"- {m[:200]}" for m in memories)
    return f"\n## Memoria — Contexto relevante de conversaciones anteriores:\n{lines}\n"


# ── room detection ──────────────────────────────────────────────────────────

_ROOM_KEYWORDS: dict[str, list[str]] = {
    "technical": ["code", "python", "bug", "error", "git", "docker", "api", "server", "deploy"],
    "projects": ["proyecto", "project", "aura", "bot", "dashboard", "squad", "agent"],
    "preferences": ["quiero", "prefiero", "siempre", "nunca", "odio", "me gusta", "quieres"],
    "tasks": ["hazlo", "ejecuta", "crea", "fix", "arregla", "instala", "actualiza"],
}


def _detect_room(text: str) -> str:
    text_lower = text.lower()
    for room, keywords in _ROOM_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return room
    return "general"

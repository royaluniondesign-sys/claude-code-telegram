"""Memory router: /api/memory, /api/rag/*."""

import json
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/memory")
async def get_memory(q: str = "", limit: int = 10) -> Dict[str, Any]:
    """MemPalace stats and search."""
    try:
        from src.context.mempalace_memory import palace_count, search_memories, get_all_memories
        count = await palace_count()
        if q:
            results = await search_memories(q, n=limit)
        else:
            results = await get_all_memories(limit=limit)
        return {"ok": True, "count": count, "results": results}
    except Exception as e:
        return {"ok": False, "count": 0, "results": [], "error": str(e)}


@router.delete("/api/memory")
async def clear_memory() -> Dict[str, Any]:
    """Clear all MemPalace memories."""
    try:
        from src.context.mempalace_memory import delete_all_memories
        ok = await delete_all_memories()
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/rag/status")
async def rag_status() -> Dict[str, Any]:
    try:
        from src.rag.retriever import RAGRetriever
        r = RAGRetriever()
        return await r.status()
    except Exception as e:
        return {"available": False, "error": str(e)}


@router.get("/api/rag/search")
async def rag_search(q: str, top_k: int = 5) -> Dict[str, Any]:
    try:
        from src.rag.retriever import RAGRetriever
        r = RAGRetriever()
        results = await r.search(q, top_k=top_k)
        return {"results": results, "query": q}
    except Exception as e:
        return {"results": [], "error": str(e)}


@router.post("/api/rag/index")
async def rag_reindex() -> Dict[str, Any]:
    """Trigger manual re-indexing of all sources."""
    try:
        from src.rag.indexer import RAGIndexer
        idx = RAGIndexer()
        stats = await idx.index_all()
        return {"ok": True, **stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}



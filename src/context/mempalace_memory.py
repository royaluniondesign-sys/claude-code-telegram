"""MemPalace stub — redirects to RAG indexer (ChromaDB not installed).

All calls transparently use the working RAG/Ollama pipeline instead.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()


async def store_interaction(user_message: str, assistant_response: str) -> None:
    """Store conversation in RAG vector store."""
    try:
        from src.rag.indexer import RAGIndexer
        rag = RAGIndexer()
        text = f"[Usuario]: {user_message[:400]}\n[AURA]: {assistant_response[:600]}"
        await rag.index_text(text, "telegram_chat", "memory")
    except Exception as exc:
        logger.debug("mempalace_rag_error", error=str(exc))


async def search_memory(query: str, top_k: int = 5) -> list:
    """Search conversation memory via RAG."""
    try:
        from src.rag.retriever import RAGRetriever
        retriever = RAGRetriever()
        return await retriever.search(query, top_k=top_k, source_types=["memory"])
    except Exception:
        return []


async def prewarm() -> None:
    """No-op — RAG initializes on first use."""
    pass

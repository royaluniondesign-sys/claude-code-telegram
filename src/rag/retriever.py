"""High-level RAG retrieval API for AURA."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

import structlog

from .embedder import embed, cosine_similarity, MODEL
from .store import RAGStore

logger = structlog.get_logger()


class RAGRetriever:
    """Retrieves relevant context from the vector store given a natural-language query."""

    def __init__(self) -> None:
        self._store = RAGStore()
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self._store.init()
            self._initialized = True

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.25,
        source_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for chunks semantically similar to query.

        Returns list of {"content", "source", "source_type", "score"} dicts,
        ordered by descending score. At most 2 chunks per source to avoid flooding.
        """
        await self._ensure_init()

        query_vec = await embed(query)
        if query_vec is None:
            logger.warning("rag_search_no_embedding", query=query[:80])
            return []

        all_chunks = await self._store.get_all_embeddings()
        if not all_chunks:
            return []

        scored: List[Dict[str, Any]] = []
        for id_, content, source, source_type, vec in all_chunks:
            if source_types and source_type not in source_types:
                continue
            score = cosine_similarity(query_vec, vec)
            if score >= min_score:
                scored.append({
                    "id": id_,
                    "content": content,
                    "source": source,
                    "source_type": source_type,
                    "score": round(float(score), 4),
                })

        # Sort by score descending
        scored.sort(key=lambda x: x["score"], reverse=True)

        # Deduplicate: max 2 chunks per source
        source_counts: Dict[str, int] = defaultdict(int)
        deduped: List[Dict[str, Any]] = []
        for item in scored:
            if source_counts[item["source"]] < 2:
                deduped.append(item)
                source_counts[item["source"]] += 1
            if len(deduped) >= top_k:
                break

        return deduped

    async def get_context_for_prompt(self, prompt: str, max_chars: int = 2000) -> str:
        """Return formatted RAG context block for injection into a prompt.

        Returns empty string if no results or RAG unavailable.
        """
        try:
            results = await self.search(prompt, top_k=5, min_score=0.25)
        except Exception as exc:
            logger.warning("rag_context_search_error", error=str(exc))
            return ""

        if not results:
            return ""

        lines: List[str] = ["[MEMORIA RELEVANTE]"]
        chars_used = 0

        for item in results:
            # Shorten source path for display
            source_display = item["source"].replace(str(__import__("pathlib").Path.home()), "~")
            source_type = item["source_type"]
            content = item["content"].strip()

            # Truncate content if needed
            remaining = max_chars - chars_used - 200  # reserve for headers
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining] + "…"

            block = f'\n📄 {source_display} ({source_type})\n"{content}"\n'
            chars_used += len(block)
            lines.append(block)

            if chars_used >= max_chars:
                break

        lines.append("[/MEMORIA]")
        return "\n".join(lines)

    async def status(self) -> Dict[str, Any]:
        """Return status dict: chunks, sources, model, available."""
        try:
            await self._ensure_init()
            chunk_count = await self._store.chunk_count()
            source_count = await self._store.distinct_sources()
            return {
                "chunks": chunk_count,
                "sources": source_count,
                "model": MODEL,
                "available": True,
            }
        except Exception as exc:
            logger.warning("rag_status_error", error=str(exc))
            return {
                "chunks": 0,
                "sources": 0,
                "model": MODEL,
                "available": False,
                "error": str(exc),
            }

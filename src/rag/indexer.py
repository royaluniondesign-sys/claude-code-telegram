"""Incremental file indexer for AURA RAG."""
from __future__ import annotations

import asyncio
import glob
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from .chunker import chunk_markdown, chunk_text, chunk_logs
from .embedder import embed_batch
from .store import RAGStore

logger = structlog.get_logger()

_HOME = Path.home()
_AURA_ROOT = _HOME / "claude-code-telegram"

# (glob_pattern_or_path, source_type, last_n_lines_or_None)
INDEX_SOURCES: List[tuple[str, str, Optional[int]]] = [
    (str(_HOME / ".aura" / "memory" / "*.md"), "memory", None),
    (str(_AURA_ROOT / "MISSION.md"), "mission", None),
    (str(_AURA_ROOT / "CLAUDE.md"), "mission", None),
    (str(_AURA_ROOT / "conductor_log.md"), "log", None),
    (str(_AURA_ROOT / "logs" / "bot.stdout.log"), "log", 500),
]


class RAGIndexer:
    """Incremental file indexer — only re-embeds changed chunks."""

    def __init__(self) -> None:
        self._store = RAGStore()
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self._store.init()
            self._initialized = True

    def _select_chunker(self, path: Path, source_type: str) -> Any:
        """Return the right chunker function based on source_type and extension."""
        if source_type == "log" or path.suffix in (".log",):
            return chunk_logs
        if path.suffix in (".md", ".markdown") or source_type in ("memory", "mission"):
            return chunk_markdown
        return chunk_text

    async def index_file(self, path: Path, source_type: str, last_n_lines: Optional[int] = None) -> Dict[str, int]:
        """Chunk a file, embed changed chunks, upsert. Returns {"indexed": N, "skipped": N, "errors": N}."""
        await self._ensure_init()

        if not path.exists():
            return {"indexed": 0, "skipped": 0, "errors": 0}

        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("rag_index_read_error", path=str(path), error=str(exc))
            return {"indexed": 0, "skipped": 0, "errors": 1}

        if last_n_lines is not None:
            lines = raw.splitlines()
            raw = "\n".join(lines[-last_n_lines:])

        source = str(path)
        chunker = self._select_chunker(path, source_type)

        try:
            chunks = chunker(raw, source)
        except Exception as exc:
            logger.warning("rag_index_chunk_error", path=source, error=str(exc))
            return {"indexed": 0, "skipped": 0, "errors": 1}

        if not chunks:
            return {"indexed": 0, "skipped": 0, "errors": 0}

        # Get existing hashes to detect changes
        existing_hashes = await self._store.get_chunk_hashes(source)

        to_embed: List[int] = []  # indices into chunks
        skipped = 0

        for i, chunk in enumerate(chunks):
            content_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()[:32]
            existing_hash = existing_hashes.get(chunk["id"])
            if existing_hash == content_hash:
                skipped += 1
            else:
                to_embed.append(i)

        if not to_embed:
            return {"indexed": 0, "skipped": skipped, "errors": 0}

        texts = [chunks[i]["content"] for i in to_embed]
        embeddings = await embed_batch(texts)

        indexed = 0
        errors = 0
        for idx, emb in zip(to_embed, embeddings):
            chunk = chunks[idx]
            if emb is None:
                errors += 1
                continue
            try:
                await self._store.upsert_chunk(
                    id=chunk["id"],
                    source=source,
                    source_type=source_type,
                    content=chunk["content"],
                    embedding=emb,
                    metadata=chunk.get("metadata", {}),
                )
                indexed += 1
            except Exception as exc:
                logger.warning("rag_upsert_error", chunk_id=chunk["id"], error=str(exc))
                errors += 1

        logger.info("rag_file_indexed", path=source, indexed=indexed, skipped=skipped, errors=errors)
        return {"indexed": indexed, "skipped": skipped, "errors": errors}

    async def index_all(self) -> Dict[str, int]:
        """Index all configured sources. Returns aggregate stats."""
        await self._ensure_init()

        total = {"indexed": 0, "skipped": 0, "errors": 0}

        for pattern, source_type, last_n in INDEX_SOURCES:
            # Expand globs
            matched_paths: List[Path] = []
            if "*" in pattern or "?" in pattern:
                matched_paths = [Path(p) for p in glob.glob(pattern)]
            else:
                p = Path(pattern)
                if p.exists():
                    matched_paths = [p]

            for path in matched_paths:
                try:
                    stats = await self.index_file(path, source_type, last_n_lines=last_n)
                    for key in total:
                        total[key] += stats[key]
                except Exception as exc:
                    logger.warning("rag_index_source_error", path=str(path), error=str(exc))
                    total["errors"] += 1

        logger.info("rag_index_all_done", **total)
        return total

    async def index_all_background(self) -> None:
        """Run index_all once immediately (fire-and-forget safe wrapper)."""
        try:
            await self.index_all()
        except Exception as exc:
            logger.warning("rag_index_all_background_error", error=str(exc))

    async def index_text(self, text: str, source: str, source_type: str) -> Dict[str, int]:
        """Index arbitrary text (e.g. chat message, routine result) on demand."""
        await self._ensure_init()

        if source_type == "log":
            chunks = chunk_logs(text, source)
        elif source_type in ("memory", "mission") or source.endswith(".md"):
            chunks = chunk_markdown(text, source)
        else:
            chunks = chunk_text(text, source)

        if not chunks:
            return {"indexed": 0, "skipped": 0, "errors": 0}

        texts = [c["content"] for c in chunks]
        embeddings = await embed_batch(texts)

        indexed = 0
        errors = 0
        for chunk, emb in zip(chunks, embeddings):
            if emb is None:
                errors += 1
                continue
            try:
                await self._store.upsert_chunk(
                    id=chunk["id"],
                    source=source,
                    source_type=source_type,
                    content=chunk["content"],
                    embedding=emb,
                    metadata=chunk.get("metadata", {}),
                )
                indexed += 1
            except Exception as exc:
                logger.warning("rag_index_text_upsert_error", error=str(exc))
                errors += 1

        return {"indexed": indexed, "skipped": 0, "errors": errors}

    async def start_background_indexer(self, interval_seconds: int = 300) -> None:
        """Async task that re-indexes all sources every N seconds."""
        logger.info("rag_background_indexer_started", interval=interval_seconds)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.index_all()
            except Exception as exc:
                logger.warning("rag_background_indexer_error", error=str(exc))

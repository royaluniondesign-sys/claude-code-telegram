"""SQLite vector store for AURA RAG embeddings."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import numpy as np
import structlog

logger = structlog.get_logger()

_DB_PATH = Path.home() / ".aura" / "rag.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rag_chunks (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    content      TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

_CREATE_INDICES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_chunks(source);",
    "CREATE INDEX IF NOT EXISTS idx_rag_source_type ON rag_chunks(source_type);",
    "CREATE INDEX IF NOT EXISTS idx_rag_content_hash ON rag_chunks(content_hash);",
]


class RAGStore:
    """Async SQLite vector store."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        """Create DB directory and tables if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDICES_SQL:
                await db.execute(idx_sql)
            await db.commit()
        logger.info("rag_store_initialized", path=str(self._db_path))

    async def upsert_chunk(
        self,
        id: str,
        source: str,
        source_type: str,
        content: str,
        embedding: np.ndarray,
        metadata: Dict[str, Any],
    ) -> None:
        """Insert or replace a chunk with its embedding."""
        import hashlib
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:32]
        embedding_blob = embedding.astype(np.float32).tobytes()
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        updated_at = datetime.now(UTC).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO rag_chunks
                    (id, source, source_type, content, embedding, metadata, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (id, source, source_type, content, embedding_blob, metadata_json, content_hash, updated_at),
            )
            await db.commit()

    async def delete_by_source(self, source: str) -> int:
        """Remove all chunks for a given source. Returns number of rows deleted."""
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute("DELETE FROM rag_chunks WHERE source = ?", (source,))
            await db.commit()
            return cursor.rowcount

    async def get_all_embeddings(self) -> List[Tuple[str, str, str, str, np.ndarray]]:
        """Return list of (id, content, source, source_type, embedding) for all chunks."""
        rows = []
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT id, content, source, source_type, embedding FROM rag_chunks"
            ) as cursor:
                async for row in cursor:
                    id_, content, source, source_type, blob = row
                    vec = np.frombuffer(blob, dtype=np.float32)
                    rows.append((id_, content, source, source_type, vec))
        return rows

    async def get_chunk_hashes(self, source: str) -> Dict[str, str]:
        """Return {chunk_id: content_hash} for all chunks of a source."""
        result: Dict[str, str] = {}
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT id, content_hash FROM rag_chunks WHERE source = ?", (source,)
            ) as cursor:
                async for row in cursor:
                    result[row[0]] = row[1]
        return result

    async def chunk_count(self) -> int:
        """Return total number of stored chunks."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM rag_chunks") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def distinct_sources(self) -> int:
        """Return number of distinct sources."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(DISTINCT source) FROM rag_chunks") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

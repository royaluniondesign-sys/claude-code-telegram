"""Response cache — avoid re-asking the same question.

Simple SQLite cache with TTL. Saves tokens by returning
cached responses for repeated or similar queries.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_CACHE_DB = Path.home() / ".aura" / "cache.db"
_DEFAULT_TTL = 3600  # 1 hour


class ResponseCache:
    """SQLite-backed response cache."""

    def __init__(self, db_path: Path = _CACHE_DB, ttl: int = _DEFAULT_TTL) -> None:
        self._db_path = db_path
        self._ttl = ttl
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create cache table if not exists."""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    brain TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    hit_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_created
                ON cache(created_at)
            """)

    @staticmethod
    def _make_key(prompt: str, brain: str) -> str:
        """Create cache key from prompt + brain."""
        normalized = prompt.strip().lower()
        raw = f"{brain}:{normalized}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, prompt: str, brain: str) -> Optional[str]:
        """Get cached response if fresh."""
        key = self._make_key(prompt, brain)
        cutoff = time.time() - self._ttl

        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT response, created_at FROM cache "
                    "WHERE key = ? AND created_at > ?",
                    (key, cutoff),
                ).fetchone()

                if row:
                    conn.execute(
                        "UPDATE cache SET hit_count = hit_count + 1 WHERE key = ?",
                        (key,),
                    )
                    return row[0]
        except Exception as e:
            logger.warning("cache_get_error", error=str(e))

        return None

    def put(self, prompt: str, brain: str, response: str) -> None:
        """Cache a response."""
        key = self._make_key(prompt, brain)

        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache (key, brain, response, created_at, hit_count) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (key, brain, response, time.time()),
                )
        except Exception as e:
            logger.warning("cache_put_error", error=str(e))

    def cleanup(self) -> int:
        """Remove expired entries. Returns count removed."""
        cutoff = time.time() - self._ttl
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    "DELETE FROM cache WHERE created_at < ?", (cutoff,)
                )
                return cursor.rowcount
        except Exception as e:
            logger.debug("cache_cleanup_error", error=str(e))
            return 0

    def stats(self) -> dict:
        """Get cache statistics."""
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                total = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                hits = conn.execute(
                    "SELECT SUM(hit_count) FROM cache"
                ).fetchone()[0] or 0
                fresh = conn.execute(
                    "SELECT COUNT(*) FROM cache WHERE created_at > ?",
                    (time.time() - self._ttl,),
                ).fetchone()[0]
                return {
                    "total_entries": total,
                    "fresh_entries": fresh,
                    "total_hits": hits,
                    "db_size_kb": round(self._db_path.stat().st_size / 1024, 1)
                    if self._db_path.exists()
                    else 0,
                }
        except Exception as e:
            logger.debug("cache_stats_error", error=str(e))
            return {"total_entries": 0, "fresh_entries": 0, "total_hits": 0}

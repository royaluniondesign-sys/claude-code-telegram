"""Content memory — SQLite store that prevents topic repetition.

Tracks every piece of content planned or published so the brain
never repeats a topic within the configured cooldown window.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".aura" / "content_memory.db"
COOLDOWN_DAYS = 21  # Don't revisit same topic within 3 weeks


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.execute("""
        CREATE TABLE IF NOT EXISTS content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_key TEXT NOT NULL,
            title TEXT,
            format TEXT,
            platform TEXT,
            status TEXT DEFAULT 'planned',
            meta TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_topic ON content_log(topic_key)")
    c.commit()
    return c


def is_fresh(topic_key: str, cooldown_days: int = COOLDOWN_DAYS) -> bool:
    """Return True if this topic hasn't been used recently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM content_log WHERE topic_key=? AND created_at > ? LIMIT 1",
            (topic_key.lower()[:100], cutoff),
        ).fetchone()
    return row is None


def log_planned(
    topic_key: str,
    title: str,
    fmt: str,
    platform: str,
    meta: Optional[dict] = None,
) -> int:
    """Record a planned content item. Returns new row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO content_log (topic_key, title, format, platform, status, meta) "
            "VALUES (?,?,?,?,?,?)",
            (topic_key.lower()[:100], title[:200], fmt, platform, "planned",
             json.dumps(meta or {})),
        )
        return cur.lastrowid or 0


def mark_published(row_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE content_log SET status='published' WHERE id=?", (row_id,))


def mark_failed(row_id: int, error: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE content_log SET status='failed', meta=json_set(meta,'$.error',?) WHERE id=?",
            (error[:200], row_id),
        )


def recent_topics(limit: int = 30) -> list[dict]:
    """Return recent content log for status reporting."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, topic_key, title, format, platform, status, created_at "
            "FROM content_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "topic": r[1], "title": r[2], "format": r[3],
         "platform": r[4], "status": r[5], "created": r[6]}
        for r in rows
    ]

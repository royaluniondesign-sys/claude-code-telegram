"""Routines — persistent store for user-defined & auto-created scheduled tasks.

Routines are named prompts that run on a schedule (daily, hourly, cron).
They can be created by Ricardo or auto-created by the conductor when it
detects a recurring improvement opportunity.

Different from `scheduled_jobs` (APScheduler internal) — routines are
the user-facing concept with full CRUD, logs, and dashboard UI.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

import aiosqlite
import structlog

logger = structlog.get_logger()

_DB_PATH = "/Users/oxyzen/claude-code-telegram/data/bot.db"


@dataclass
class Routine:
    name: str
    prompt: str                           # full prompt sent to brain
    description: str = ""                 # short human label
    brain: str = "codex"                  # which brain executes it
    frequency: str = "daily"              # hourly | daily | weekly | cron:<expr>
    schedule_time: str = "09:00"          # HH:MM (for daily/weekly)
    working_dir: str = "/Users/oxyzen/claude-code-telegram"
    is_local: bool = True                 # local = only runs while Mac is on
    enabled: bool = True
    auto_created: bool = False            # True if conductor created it
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    last_run_at: Optional[str] = None
    last_result: Optional[str] = None     # first 500 chars of output
    last_status: str = "pending"          # pending | ok | error
    run_count: int = 0

    def to_cron(self) -> str:
        """Convert frequency + schedule_time to APScheduler cron args string."""
        if self.frequency.startswith("cron:"):
            return self.frequency[5:]  # raw cron expression
        h, m = (self.schedule_time or "09:00").split(":")
        if self.frequency == "hourly":
            return f"0 * * * *"
        if self.frequency == "daily":
            return f"{m} {h} * * *"
        if self.frequency == "weekly":
            return f"{m} {h} * * 1"  # every Monday
        return f"{m} {h} * * *"       # default daily

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    prompt TEXT NOT NULL,
    brain TEXT DEFAULT 'codex',
    frequency TEXT DEFAULT 'daily',
    schedule_time TEXT DEFAULT '09:00',
    working_dir TEXT DEFAULT '/Users/oxyzen/claude-code-telegram',
    is_local INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    auto_created INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_result TEXT,
    last_status TEXT DEFAULT 'pending',
    run_count INTEGER DEFAULT 0
)
"""

_CREATE_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS routine_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    status TEXT NOT NULL,
    output TEXT,
    duration_ms INTEGER DEFAULT 0,
    brain_used TEXT
)
"""


async def _ensure_tables(db: aiosqlite.Connection) -> None:
    await db.execute(_CREATE_TABLE)
    await db.execute(_CREATE_LOGS_TABLE)
    await db.commit()


async def list_routines() -> List[Routine]:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM routines ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_routine(r) for r in rows]


async def get_routine(routine_id: str) -> Optional[Routine]:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM routines WHERE id = ?", (routine_id,)
        ) as cur:
            row = await cur.fetchone()
    return _row_to_routine(row) if row else None


async def create_routine(r: Routine) -> Routine:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        await db.execute(
            """INSERT INTO routines
               (id, name, description, prompt, brain, frequency, schedule_time,
                working_dir, is_local, enabled, auto_created, created_at,
                last_run_at, last_result, last_status, run_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r.id, r.name, r.description, r.prompt, r.brain,
                r.frequency, r.schedule_time, r.working_dir,
                int(r.is_local), int(r.enabled), int(r.auto_created),
                r.created_at, r.last_run_at, r.last_result,
                r.last_status, r.run_count,
            ),
        )
        await db.commit()
    logger.info("routine_created", id=r.id, name=r.name, auto=r.auto_created)
    return r


async def update_routine(routine_id: str, **fields: Any) -> Optional[Routine]:
    allowed = {
        "name", "description", "prompt", "brain", "frequency",
        "schedule_time", "working_dir", "is_local", "enabled",
        "last_run_at", "last_result", "last_status", "run_count",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return await get_routine(routine_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [routine_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        await db.execute(
            f"UPDATE routines SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
    return await get_routine(routine_id)


async def delete_routine(routine_id: str) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        cur = await db.execute(
            "DELETE FROM routines WHERE id = ?", (routine_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def append_log(
    routine_id: str, status: str, output: str,
    duration_ms: int = 0, brain_used: str = ""
) -> None:
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        await db.execute(
            """INSERT INTO routine_logs
               (routine_id, ran_at, status, output, duration_ms, brain_used)
               VALUES (?,?,?,?,?,?)""",
            (routine_id, now, status, output[:2000], duration_ms, brain_used),
        )
        await db.commit()


async def get_logs(routine_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT ran_at, status, output, duration_ms, brain_used
               FROM routine_logs WHERE routine_id = ?
               ORDER BY ran_at DESC LIMIT ?""",
            (routine_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def routine_exists(name: str) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        await _ensure_tables(db)
        async with db.execute(
            "SELECT 1 FROM routines WHERE name = ?", (name,)
        ) as cur:
            return await cur.fetchone() is not None


def _row_to_routine(row: aiosqlite.Row) -> Routine:
    d = dict(row)
    return Routine(
        id=d["id"],
        name=d["name"],
        description=d.get("description") or "",
        prompt=d["prompt"],
        brain=d.get("brain") or "codex",
        frequency=d.get("frequency") or "daily",
        schedule_time=d.get("schedule_time") or "09:00",
        working_dir=d.get("working_dir") or "/Users/oxyzen/claude-code-telegram",
        is_local=bool(d.get("is_local", 1)),
        enabled=bool(d.get("enabled", 1)),
        auto_created=bool(d.get("auto_created", 0)),
        created_at=d.get("created_at") or datetime.now(UTC).isoformat(),
        last_run_at=d.get("last_run_at"),
        last_result=d.get("last_result"),
        last_status=d.get("last_status") or "pending",
        run_count=d.get("run_count") or 0,
    )

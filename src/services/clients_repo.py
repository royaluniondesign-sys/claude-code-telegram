"""Multi-client social accounts repository.

Manages clients and their social media accounts in SQLite.
Each client can have multiple accounts across platforms (Instagram, Facebook, etc.).

Usage:
    repo = ClientsRepo(db_manager)

    # Create a client
    client = await repo.create_client("royalunion", "Royal Union Design")

    # Add a social account
    account = await repo.add_account(
        client_id=client["id"],
        platform="instagram",
        account_id="17841400008460",
        account_name="@royaluniondesign",
        access_token="EAAxxxx...",
    )

    # List clients with account counts
    clients = await repo.list_clients()

    # Get accounts for a client
    accounts = await repo.get_accounts(client_id=1)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Optional

log = logging.getLogger(__name__)


class ClientsRepo:
    """Repository for social clients and accounts."""

    def __init__(self, db_manager: Any) -> None:
        self._db = db_manager

    # ── Clients ──────────────────────────────────────────────────────────────

    async def create_client(
        self,
        slug: str,
        name: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """Create a new client. Returns the created client row."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO social_clients (slug, name, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (slug, name, notes, datetime.now(UTC), datetime.now(UTC)),
            )
            await conn.commit()
            client_id = cursor.lastrowid
        return await self.get_client(client_id)

    async def get_client(self, client_id: int) -> Optional[dict[str, Any]]:
        """Get a client by ID."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM social_clients WHERE id = ?",
                (client_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_client_by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        """Get a client by slug."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM social_clients WHERE slug = ?",
                (slug,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_clients(self, active_only: bool = True) -> list[dict[str, Any]]:
        """List clients with account counts."""
        where = "WHERE sc.is_active = 1" if active_only else ""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT
                    sc.*,
                    COUNT(sa.id) as account_count
                FROM social_clients sc
                LEFT JOIN social_accounts sa
                    ON sa.client_id = sc.id AND sa.is_active = 1
                {where}
                GROUP BY sc.id
                ORDER BY sc.name
                """,
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_client(
        self,
        client_id: int,
        name: Optional[str] = None,
        notes: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        """Update client fields. Returns updated row."""
        updates: list[str] = []
        params: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(is_active)
        if not updates:
            return await self.get_client(client_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC))
        params.append(client_id)

        async with self._db.get_connection() as conn:
            await conn.execute(
                f"UPDATE social_clients SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await conn.commit()
        return await self.get_client(client_id)

    # ── Social Accounts ───────────────────────────────────────────────────────

    async def add_account(
        self,
        client_id: int,
        platform: str,
        account_id: str,
        account_name: str,
        access_token: str = "",
        token_expiry: Optional[datetime] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Add a social account for a client."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO social_accounts
                    (client_id, platform, account_id, account_name,
                     access_token, token_expiry, meta, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, platform, account_id)
                DO UPDATE SET
                    account_name = excluded.account_name,
                    access_token = excluded.access_token,
                    token_expiry = excluded.token_expiry,
                    meta = excluded.meta,
                    updated_at = excluded.updated_at
                """,
                (
                    client_id,
                    platform.lower(),
                    account_id,
                    account_name,
                    access_token,
                    token_expiry,
                    json.dumps(meta or {}),
                    datetime.now(UTC),
                    datetime.now(UTC),
                ),
            )
            await conn.commit()
            row_id = cursor.lastrowid
        return await self.get_account(row_id)

    async def get_account(self, account_id: int) -> Optional[dict[str, Any]]:
        """Get an account by row ID."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM social_accounts WHERE id = ?",
                (account_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    async def get_accounts(
        self,
        client_id: Optional[int] = None,
        platform: Optional[str] = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List social accounts with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if client_id is not None:
            conditions.append("sa.client_id = ?")
            params.append(client_id)
        if platform is not None:
            conditions.append("sa.platform = ?")
            params.append(platform.lower())
        if active_only:
            conditions.append("sa.is_active = 1")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT sa.*, sc.name as client_name, sc.slug as client_slug
                FROM social_accounts sa
                JOIN social_clients sc ON sc.id = sa.client_id
                {where}
                ORDER BY sc.name, sa.platform, sa.account_name
                """,
                params,
            )
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["meta"] = json.loads(d.get("meta") or "{}")
            result.append(d)
        return result

    async def remove_account(self, account_id: int) -> bool:
        """Soft-delete an account (set is_active=False)."""
        async with self._db.get_connection() as conn:
            await conn.execute(
                "UPDATE social_accounts SET is_active = 0, updated_at = ? WHERE id = ?",
                (datetime.now(UTC), account_id),
            )
            await conn.commit()
        return True

    async def get_default_accounts(
        self, platform: str
    ) -> list[dict[str, Any]]:
        """Get all active accounts for a platform (cross-client)."""
        return await self.get_accounts(platform=platform, active_only=True)

import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from terminal_mcp.auth.continuity import ManagerContinuityLock
from terminal_mcp.auth.foundation import AuthConflictError
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection

PAIRING_SECRET_BYTES = 32
PAIRING_DEFAULT_TTL_SEC = 300


@dataclass(frozen=True)
class PairingExchange:
    device_id: str
    client_id: str
    refresh_token: str
    scope: str


class PairingStore:
    """Durable one-time pairing and device identities.

    Pairing and refresh-token plaintext is returned only to the caller that
    creates it. Durable storage contains hashes and non-secret device metadata.
    """

    def __init__(self, path):
        self.path = path
        self._manager_continuity: ManagerContinuityLock | None = None
        self._manager_revoke_guard: Callable[[str], Awaitable[bool]] | None = None

    def configure_manager_continuity(
        self,
        continuity: ManagerContinuityLock,
        revoke_guard: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._manager_continuity = continuity
        self._manager_revoke_guard = revoke_guard

    async def initialize(self) -> None:
        secure_database_path(self.path)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS console_pairings(
                    secret_hash TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_console_pairings_expires
                    ON console_pairings(expires_at);

                CREATE TABLE IF NOT EXISTS console_devices(
                    device_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL UNIQUE,
                    public_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_console_devices_client
                    ON console_devices(client_id);

                CREATE TABLE IF NOT EXISTS console_pairing_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    device_id TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_console_pairing_audit_created
                    ON console_pairing_audit(created_at);
                """
            )
            await db.commit()

    @staticmethod
    def digest(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    @staticmethod
    async def _audit(
        db,
        event_type: str,
        outcome: str,
        reason: str = "",
        device_id: str | None = None,
        *,
        now: int,
    ) -> None:
        await db.execute(
            "INSERT INTO console_pairing_audit"
            "(event_type,outcome,reason,device_id,created_at) VALUES(?,?,?,?,?)",
            (event_type, outcome, reason, device_id, now),
        )

    async def record_audit(
        self,
        event_type: str,
        outcome: str,
        reason: str = "",
        device_id: str | None = None,
        *,
        now: int | None = None,
    ) -> None:
        recorded_at = int(time.time()) if now is None else int(now)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await self._audit(
                db,
                event_type,
                outcome,
                reason,
                device_id,
                now=recorded_at,
            )
            await db.commit()

    async def create(
        self, ttl_seconds: int = PAIRING_DEFAULT_TTL_SEC, *, now: int | None = None
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued_at = int(time.time()) if now is None else int(now)
        secret = secrets.token_urlsafe(PAIRING_SECRET_BYTES)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute(
                "INSERT INTO console_pairings(secret_hash,created_at,expires_at,consumed_at) "
                "VALUES(?,?,?,NULL)",
                (self.digest(secret), issued_at, issued_at + ttl_seconds),
            )
            await self._audit(db, "pairing_create", "success", now=issued_at)
            await db.commit()
        return secret

    async def consume(self, secret: str, *, now: int | None = None) -> bool:
        """Atomically consume one unexpired secret."""

        consumed_at = int(time.time()) if now is None else int(now)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            cursor = await db.execute(
                "UPDATE console_pairings SET consumed_at=? "
                "WHERE secret_hash=? AND consumed_at IS NULL AND expires_at>?",
                (consumed_at, self.digest(secret), consumed_at),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def exchange(
        self,
        secret: str,
        public_key: str,
        label: str,
        scope: str,
        refresh_ttl_seconds: int,
        *,
        now: int | None = None,
    ) -> tuple[PairingExchange | None, str]:
        """Consume a pairing and create its device credential in one transaction."""

        exchanged_at = int(time.time()) if now is None else int(now)
        secret_hash = self.digest(secret)
        device_id = "dev_" + secrets.token_urlsafe(18)
        client_id = "device_" + secrets.token_urlsafe(24)
        refresh_token = secrets.token_urlsafe(48)

        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT expires_at,consumed_at FROM console_pairings WHERE secret_hash=?",
                    (secret_hash,),
                )
            ).fetchone()
            if row is None:
                await self._audit(
                    db, "pairing_exchange", "rejected", "invalid", now=exchanged_at
                )
                await db.commit()
                return None, "invalid"
            if row[1] is not None:
                await self._audit(
                    db, "pairing_exchange", "rejected", "replayed", now=exchanged_at
                )
                await db.commit()
                return None, "replayed"
            if row[0] <= exchanged_at:
                await self._audit(
                    db, "pairing_exchange", "rejected", "expired", now=exchanged_at
                )
                await db.commit()
                return None, "expired"

            cursor = await db.execute(
                "UPDATE console_pairings SET consumed_at=? "
                "WHERE secret_hash=? AND consumed_at IS NULL AND expires_at>?",
                (exchanged_at, secret_hash, exchanged_at),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None, "replayed"

            await db.execute(
                "INSERT INTO oauth_clients"
                "(client_id,client_secret_hash,redirect_uris,client_name,auth_method,created_at) "
                "VALUES(?,NULL,'',?,'none',?)",
                (client_id, label, exchanged_at),
            )
            await db.execute(
                "INSERT INTO oauth_refresh_tokens"
                "(token_hash,client_id,scope,expires_at,revoked) VALUES(?,?,?,?,0)",
                (
                    self.digest(refresh_token),
                    client_id,
                    scope,
                    exchanged_at + refresh_ttl_seconds,
                ),
            )
            await db.execute(
                "INSERT INTO console_devices"
                "(device_id,client_id,public_key,label,created_at,last_used_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,NULL)",
                (device_id, client_id, public_key, label, exchanged_at, exchanged_at),
            )
            await self._audit(
                db,
                "pairing_exchange",
                "success",
                device_id=device_id,
                now=exchanged_at,
            )
            await db.commit()

        return (
            PairingExchange(
                device_id=device_id,
                client_id=client_id,
                refresh_token=refresh_token,
                scope=scope,
            ),
            "",
        )

    async def list_devices(self) -> list[dict]:
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            rows = await (
                await db.execute(
                    "SELECT device_id,label,created_at,last_used_at,revoked_at "
                    "FROM console_devices ORDER BY created_at DESC,device_id"
                )
            ).fetchall()
        return [
            {
                "device_id": row[0],
                "label": row[1],
                "created_at": row[2],
                "last_used_at": row[3],
                "revoked_at": row[4],
            }
            for row in rows
        ]
    async def active_device_for_client(self, client_id: str) -> dict | None:
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            row = await (
                await db.execute(
                    "SELECT device_id,client_id,label FROM console_devices "
                    "WHERE client_id=? AND revoked_at IS NULL",
                    (client_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {"device_id": row[0], "client_id": row[1], "label": row[2]}

    async def device_active(self, device_id: str, client_id: str) -> bool:
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM console_devices "
                    "WHERE device_id=? AND client_id=? AND revoked_at IS NULL",
                    (device_id, client_id),
                )
            ).fetchone()
        return row is not None

    async def _active_client_id_for_device(self, device_id: str) -> str | None:
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            row = await (
                await db.execute(
                    "SELECT client_id FROM console_devices "
                    "WHERE device_id=? AND revoked_at IS NULL",
                    (device_id,),
                )
            ).fetchone()
        return None if row is None else str(row[0])

    async def _revoke_device_unchecked(self, device_id: str, *, revoked_at: int) -> bool:
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT client_id FROM console_devices "
                    "WHERE device_id=? AND revoked_at IS NULL",
                    (device_id,),
                )
            ).fetchone()
            if row is None:
                await self._audit(
                    db,
                    "device_revoke",
                    "rejected",
                    "not_found_or_revoked",
                    device_id,
                    now=revoked_at,
                )
                await db.commit()
                return False

            client_id = row[0]
            await db.execute(
                "UPDATE console_devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (revoked_at, device_id),
            )
            await db.execute("DELETE FROM oauth_refresh_tokens WHERE client_id=?", (client_id,))
            await db.execute("DELETE FROM oauth_codes WHERE client_id=?", (client_id,))
            await db.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
            await self._audit(
                db,
                "device_revoke",
                "success",
                device_id=device_id,
                now=revoked_at,
            )
            await db.commit()
            return True

    async def revoke_device(self, device_id: str, *, now: int | None = None) -> bool:
        revoked_at = int(time.time()) if now is None else int(now)
        if self._manager_continuity is None:
            return await self._revoke_device_unchecked(device_id, revoked_at=revoked_at)

        async with self._manager_continuity.hold():
            client_id = await self._active_client_id_for_device(device_id)
            if (
                client_id is not None
                and self._manager_revoke_guard is not None
                and not await self._manager_revoke_guard(client_id)
            ):
                raise AuthConflictError("last_auth_manager_required")
            return await self._revoke_device_unchecked(device_id, revoked_at=revoked_at)

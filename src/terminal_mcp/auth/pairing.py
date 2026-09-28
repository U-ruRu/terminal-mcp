import hashlib
import secrets
import time

import aiosqlite

from terminal_mcp.storage.permissions import secure_database_path

PAIRING_SECRET_BYTES = 32
PAIRING_DEFAULT_TTL_SEC = 300


class PairingStore:
    """Durable one-time pairing secrets.

    Only SHA-256 digests are persisted. Plaintext secrets exist only long enough
    to return the one-time pairing URL to the local CLI caller.
    """

    def __init__(self, path):
        self.path = path

    async def initialize(self) -> None:
        secure_database_path(self.path)
        async with aiosqlite.connect(self.path) as db:
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
                """
            )
            await db.commit()

    @staticmethod
    def digest(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    async def create(
        self, ttl_seconds: int = PAIRING_DEFAULT_TTL_SEC, *, now: int | None = None
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued_at = int(time.time()) if now is None else int(now)
        secret = secrets.token_urlsafe(PAIRING_SECRET_BYTES)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO console_pairings(secret_hash,created_at,expires_at,consumed_at) "
                "VALUES(?,?,?,NULL)",
                (self.digest(secret), issued_at, issued_at + ttl_seconds),
            )
            await db.commit()
        return secret

    async def consume(self, secret: str, *, now: int | None = None) -> bool:
        """Atomically consume one unexpired secret.

        A concurrent/replayed/expired consume returns False and never revives the
        record.
        """

        consumed_at = int(time.time()) if now is None else int(now)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            cursor = await db.execute(
                "UPDATE console_pairings SET consumed_at=? "
                "WHERE secret_hash=? AND consumed_at IS NULL AND expires_at>?",
                (consumed_at, self.digest(secret), consumed_at),
            )
            await db.commit()
        return cursor.rowcount == 1

import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable

import aiosqlite

from terminal_mcp.auth.continuity import ManagerContinuityLock
from terminal_mcp.auth.foundation import AuthConflictError
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import cancellation_safe_connection


class OAuthStore:
    def __init__(self, path):
        self.path = path
        self._manager_continuity: ManagerContinuityLock | None = None
        self._manager_delete_guard: Callable[[str], Awaitable[bool]] | None = None

    def configure_manager_continuity(
        self,
        continuity: ManagerContinuityLock,
        delete_guard: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._manager_continuity = continuity
        self._manager_delete_guard = delete_guard

    async def initialize(self):
        secure_database_path(self.path)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.executescript(
                """CREATE TABLE IF NOT EXISTS oauth_clients(client_id TEXT PRIMARY KEY,client_secret_hash TEXT,redirect_uris TEXT NOT NULL,client_name TEXT NOT NULL,auth_method TEXT NOT NULL,created_at INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS oauth_codes(code_hash TEXT PRIMARY KEY,client_id TEXT NOT NULL,redirect_uri TEXT NOT NULL,scope TEXT NOT NULL,code_challenge TEXT NOT NULL,expires_at INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);CREATE TABLE IF NOT EXISTS oauth_refresh_tokens(token_hash TEXT PRIMARY KEY,client_id TEXT NOT NULL,scope TEXT NOT NULL,expires_at INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);"""  # noqa: E501
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_oauth_codes_retention "
                "ON oauth_codes(used,expires_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_oauth_refresh_retention "
                "ON oauth_refresh_tokens(revoked,expires_at)"
            )
            await self._cleanup_rows(db, int(time.time()))
            await db.commit()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    async def _cleanup_rows(db, now):
        codes = await db.execute(
            "DELETE FROM oauth_codes WHERE used=1 OR expires_at<?", (int(now),)
        )
        refresh = await db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE revoked=1 OR expires_at<?", (int(now),)
        )
        return {
            "authorization_codes": max(0, int(codes.rowcount or 0)),
            "refresh_tokens": max(0, int(refresh.rowcount or 0)),
        }

    async def cleanup(self, *, now=None):
        """Purge disposable OAuth credentials without touching clients or live tokens."""
        cutoff = int(time.time()) if now is None else int(now)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            result = await self._cleanup_rows(db, cutoff)
            await db.commit()
        return result

    async def register_client(self, uris, name, method):
        cid = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute(
                "INSERT INTO oauth_clients VALUES(?,?,?,?,?,?)",
                (
                    cid,
                    self.digest(secret) if secret else None,
                    "\n".join(uris),
                    name,
                    method,
                    int(time.time()),
                ),
            )
            await db.commit()
        return cid, secret

    async def get_client(self, cid):
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            return await (
                await db.execute(
                    "SELECT client_id,client_secret_hash,redirect_uris,client_name,auth_method FROM oauth_clients WHERE client_id=?",  # noqa: E501
                    (cid,),
                )
            ).fetchone()

    async def list_clients(self):
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            return await (
                await db.execute(
                    "SELECT client_id, client_secret_hash, redirect_uris, client_name, "
                    "auth_method, created_at "
                    "FROM oauth_clients ORDER BY created_at DESC"
                )
            ).fetchall()

    async def _delete_client_unchecked(self, client_id):
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute("DELETE FROM oauth_refresh_tokens WHERE client_id=?", (client_id,))
            await db.execute("DELETE FROM oauth_codes WHERE client_id=?", (client_id,))
            await db.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
            await db.commit()

    async def delete_client(self, client_id):
        if self._manager_continuity is None:
            await self._delete_client_unchecked(client_id)
            return
        async with self._manager_continuity.hold():
            if (
                self._manager_delete_guard is not None
                and not await self._manager_delete_guard(client_id)
            ):
                raise AuthConflictError("last_auth_manager_required")
            await self._delete_client_unchecked(client_id)

    async def create_code(self, cid, redirect_uri, scope, challenge, ttl):
        code = secrets.token_urlsafe(32)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await self._cleanup_rows(db, int(time.time()))
            await db.execute(
                "INSERT INTO oauth_codes VALUES(?,?,?,?,?,?,0)",
                (self.digest(code), cid, redirect_uri, scope, challenge, int(time.time()) + ttl),
            )
            await db.commit()
        return code

    async def get_code(self, code):
        h = self.digest(code)
        now = int(time.time())
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            row = await (
                await db.execute(
                    "SELECT client_id,redirect_uri,scope,code_challenge,expires_at,used "
                    "FROM oauth_codes WHERE code_hash=?",
                    (h,),
                )
            ).fetchone()
        if not row or row[4] < now or row[5]:
            return None
        return row

    async def consume_code(self, code):
        h = self.digest(code)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            now = int(time.time())
            await self._cleanup_rows(db, now)
            cursor = await db.execute(
                "UPDATE oauth_codes SET used=1 WHERE code_hash=? AND used=0 AND expires_at>=?",
                (h, now),
            )
            if cursor.rowcount == 1:
                await db.execute("DELETE FROM oauth_codes WHERE code_hash=?", (h,))
            await db.commit()
        return cursor.rowcount == 1

    async def create_refresh(self, cid, scope, ttl):
        token = secrets.token_urlsafe(48)
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await self._cleanup_rows(db, int(time.time()))
            await db.execute(
                "INSERT INTO oauth_refresh_tokens VALUES(?,?,?,?,0)",
                (self.digest(token), cid, scope, int(time.time()) + ttl),
            )
            await db.commit()
        return token

    async def rotate_refresh(self, token):
        h = self.digest(token)
        now = int(time.time())
        async with cancellation_safe_connection(aiosqlite.connect, self.path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("BEGIN IMMEDIATE")
            await self._cleanup_rows(db, now)
            row = await (
                await db.execute(
                    "SELECT client_id,scope FROM oauth_refresh_tokens "
                    "WHERE token_hash=? AND revoked=0 AND expires_at>=?",
                    (h, now),
                )
            ).fetchone()
            if not row:
                await db.commit()
                return None
            cursor = await db.execute(
                "UPDATE oauth_refresh_tokens SET revoked=1 "
                "WHERE token_hash=? AND revoked=0 AND expires_at>=?",
                (h, now),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await self._cleanup_rows(db, now)
            await db.commit()
        return row[0], row[1]

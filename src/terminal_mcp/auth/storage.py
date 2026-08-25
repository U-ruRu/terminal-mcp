import hashlib
import secrets
import time

import aiosqlite

from terminal_mcp.storage.permissions import secure_database_path


class OAuthStore:
    def __init__(self, path):
        self.path = path

    async def initialize(self):
        secure_database_path(self.path)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """CREATE TABLE IF NOT EXISTS oauth_clients(client_id TEXT PRIMARY KEY,client_secret_hash TEXT,redirect_uris TEXT NOT NULL,client_name TEXT NOT NULL,auth_method TEXT NOT NULL,created_at INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS oauth_codes(code_hash TEXT PRIMARY KEY,client_id TEXT NOT NULL,redirect_uri TEXT NOT NULL,scope TEXT NOT NULL,code_challenge TEXT NOT NULL,expires_at INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0);CREATE TABLE IF NOT EXISTS oauth_refresh_tokens(token_hash TEXT PRIMARY KEY,client_id TEXT NOT NULL,scope TEXT NOT NULL,expires_at INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);CREATE TABLE IF NOT EXISTS oauth_authorization_requests(request_hash TEXT PRIMARY KEY,client_id TEXT NOT NULL,consumed_at INTEGER NOT NULL);"""  # noqa: E501
            )
            await db.commit()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    async def register_client(self, uris, name, method):
        cid = secrets.token_urlsafe(24)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        async with aiosqlite.connect(self.path) as db:
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
        async with aiosqlite.connect(self.path) as db:
            return await (
                await db.execute(
                    "SELECT client_id,client_secret_hash,redirect_uris,client_name,auth_method FROM oauth_clients WHERE client_id=?",  # noqa: E501
                    (cid,),
                )
            ).fetchone()

    async def list_clients(self):
        async with aiosqlite.connect(self.path) as db:
            return await (
                await db.execute(
                    "SELECT client_id, client_secret_hash, redirect_uris, client_name, "
                    "auth_method, created_at "
                    "FROM oauth_clients ORDER BY created_at DESC"
                )
            ).fetchall()

    async def delete_client(self, client_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM oauth_refresh_tokens WHERE client_id=?", (client_id,))
            await db.execute("DELETE FROM oauth_codes WHERE client_id=?", (client_id,))
            await db.execute(
                "DELETE FROM oauth_authorization_requests WHERE client_id=?", (client_id,)
            )
            await db.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))

            await db.commit()

    async def authorization_request_used(self, request_hash):
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM oauth_authorization_requests WHERE request_hash=?",
                    (request_hash,),
                )
            ).fetchone()
        return row is not None

    async def create_code_once(self, request_hash, cid, redirect_uri, scope, challenge, ttl):
        code = secrets.token_urlsafe(32)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            claimed = await db.execute(
                "INSERT OR IGNORE INTO oauth_authorization_requests VALUES(?,?,?)",
                (request_hash, cid, int(time.time())),
            )
            if claimed.rowcount != 1:
                await db.rollback()
                return None
            await db.execute(
                "INSERT INTO oauth_codes VALUES(?,?,?,?,?,?,0)",
                (self.digest(code), cid, redirect_uri, scope, challenge, int(time.time()) + ttl),
            )
            await db.commit()
        return code

    async def get_code(self, code):
        h = self.digest(code)
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
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
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE oauth_codes SET used=1 WHERE code_hash=? AND used=0",
                (h,),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def create_refresh(self, cid, scope, ttl):
        token = secrets.token_urlsafe(48)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO oauth_refresh_tokens VALUES(?,?,?,?,0)",
                (self.digest(token), cid, scope, int(time.time()) + ttl),
            )
            await db.commit()
        return token

    async def rotate_refresh(self, token):
        h = self.digest(token)
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT client_id,scope,expires_at,revoked FROM oauth_refresh_tokens WHERE token_hash=?",  # noqa: E501
                    (h,),
                )
            ).fetchone()
            if not row or row[2] < now or row[3]:
                return None
            await db.execute("UPDATE oauth_refresh_tokens SET revoked=1 WHERE token_hash=?", (h,))
            await db.commit()
        return row[0], row[1]

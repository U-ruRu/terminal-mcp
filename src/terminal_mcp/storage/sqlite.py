# ruff: noqa: E501
import secrets
from datetime import UTC, datetime
from sqlite3 import IntegrityError

import aiosqlite

from terminal_mcp.core.models import Command, Line
from terminal_mcp.storage.permissions import secure_database_path


class SqliteRepository:
    def __init__(self, path):
        self.path = path

    async def initialize(self):
        secure_database_path(self.path)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands(
                    hash TEXT PRIMARY KEY, cmd TEXT, status TEXT,
                    pid INTEGER, exit_code INTEGER, error TEXT
                );
                CREATE TABLE IF NOT EXISTS lines(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT, appeared_at TEXT, text TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_lines_hash_seq ON lines(hash, seq);
                """
            )
            await db.execute(
                "UPDATE commands SET status='failed', error='startup.recover: application restarted' "
                "WHERE status IN ('queued', 'running')"
            )
            await db.commit()

    async def create(self, cmd, *, status="queued", cmd_hash=None):
        attempts = 1 if cmd_hash is not None else 32
        for _ in range(attempts):
            h = cmd_hash or secrets.token_hex(4)
            command = Command(h, cmd, status)
            try:
                async with aiosqlite.connect(self.path) as db:
                    await db.execute(
                        "INSERT INTO commands VALUES(?,?,?,?,?,?)",
                        (h, cmd, status, None, None, None),
                    )
                    await db.commit()
                return command
            except IntegrityError:
                if cmd_hash is not None:
                    raise
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def update(self, command):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE commands SET status=?,pid=?,exit_code=?,error=? WHERE hash=?",
                (
                    command.status,
                    command.pid,
                    command.exit_code,
                    command.error,
                    command.cmd_hash,
                ),
            )
            await db.commit()

    async def delete_command(self, cmd_hash):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM lines WHERE hash=?", (cmd_hash,))
            await db.execute("DELETE FROM commands WHERE hash=?", (cmd_hash,))
            await db.commit()

    async def get(self, cmd_hash):
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT hash,cmd,status,pid,exit_code,error FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        return Command(*row) if row else None

    async def append_line(self, cmd_hash, text):
        appeared_at = datetime.now(UTC).strftime("%H:%M:%SZ")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO lines(hash,appeared_at,text) VALUES(?,?,?)",
                (cmd_hash, appeared_at, text),
            )
            await db.commit()

    async def count_lines(self, cmd_hash):
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM lines WHERE hash=?", (cmd_hash,))
            ).fetchone()
        return int(row[0])

    async def read_command_lines(self, cmd_hash, limit, offset):
        query = (
            "SELECT seq,hash,appeared_at,text FROM lines WHERE hash=? ORDER BY seq LIMIT ? OFFSET ?"
        )
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(query, (cmd_hash, limit, offset))).fetchall()
        return [Line(*row) for row in rows]

    async def read_global_after_cursor(self, limit, cursor):
        query = "SELECT seq,hash,appeared_at,text FROM lines WHERE seq>? ORDER BY seq LIMIT ?"
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(query, (cursor, limit))).fetchall()
        return [Line(*row) for row in rows]

    async def read_global_tail(self, limit, distance_from_end=None):
        if distance_from_end is None:
            take = limit
            skip = 0
        else:
            distance = max(0, distance_from_end)
            take = min(limit, distance)
            skip = max(distance - take, 0)
        if take == 0:
            return []
        query = "SELECT seq,hash,appeared_at,text FROM lines ORDER BY seq DESC LIMIT ? OFFSET ?"
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(query, (take, skip))).fetchall()
            if distance_from_end is not None and skip > 0 and len(rows) < take:
                rows = await (
                    await db.execute(
                        "SELECT seq,hash,appeared_at,text FROM lines ORDER BY seq LIMIT ?",
                        (limit,),
                    )
                ).fetchall()
                return [Line(*row) for row in rows]
        rows.reverse()
        return [Line(*row) for row in rows]

    async def read_lines(self, cmd_hash, limit, offset):
        if cmd_hash:
            return await self.read_command_lines(cmd_hash, limit, offset)
        return await self.read_global_after_cursor(limit, offset)

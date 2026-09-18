# ruff: noqa: E501
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from sqlite3 import IntegrityError

import aiosqlite

from terminal_mcp.core.models import Command, Line
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.permissions import secure_database_path


class SqliteRepository:
    def __init__(self, path):
        self.path = path
        self.events = None
        self.metrics = None

    def configure_observability(self, events, metrics):
        self.events = events
        self.metrics = metrics

    @asynccontextmanager
    async def _connect(self, operation="unknown"):
        started = time.monotonic()
        try:
            db = await aiosqlite.connect(self.path, timeout=1.0)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=1000")
            await db.execute("PRAGMA foreign_keys=ON")
            if self.metrics:
                self.metrics.inc(
                    "terminal_mcp_sqlite_operations_total",
                    (("operation", operation), ("outcome", "success")),
                )
                self.metrics.observe(
                    "terminal_mcp_sqlite_operation_duration_seconds",
                    time.monotonic() - started,
                    (("operation", operation),),
                )
            yield db
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                if self.metrics:
                    self.metrics.inc("terminal_mcp_sqlite_busy_total")
                if self.events:
                    self.events.emit(
                        "sqlite_busy", level="WARNING", outcome="error", operation=operation
                    )
                raise RuntimeError(f"sqlite.{operation}: database busy after 1000 ms") from exc
            if self.events:
                self.events.emit(
                    "sqlite_error", level="ERROR", outcome="error", operation=operation
                )
            raise
        finally:
            if "db" in locals():
                await db.close()

    async def ping(self):
        async with self._connect("health") as db:
            await (await db.execute("SELECT 1")).fetchone()
        return True

    async def initialize(self):
        secure_database_path(self.path)
        async with self._connect() as db:
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
                CREATE INDEX IF NOT EXISTS idx_lines_hash_seq ON lines(hash, seq);
                CREATE TABLE IF NOT EXISTS agent_sessions(
                    agent_id TEXT PRIMARY KEY, registered_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                    task_summary TEXT NOT NULL, intent TEXT NOT NULL, work_scope TEXT NOT NULL, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_task_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    intent TEXT NOT NULL, work_scope TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_agent_attribution(
                    command_hash TEXT PRIMARY KEY, agent_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    command_type TEXT NOT NULL, command_preview TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_activity_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    tool TEXT NOT NULL, command_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_activity ON agent_sessions(last_activity_at DESC);
                CREATE INDEX IF NOT EXISTS ix_agent_task_events_agent ON agent_task_events(agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_agent ON command_agent_attribution(agent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_hash ON command_agent_attribution(command_hash);
                CREATE INDEX IF NOT EXISTS ix_agent_activity_agent ON agent_activity_events(agent_id, id DESC);
                PRAGMA user_version=1;
                """
            )
            await db.execute(
                "UPDATE commands SET status='failed', error='startup.recover: application restarted' "
                "WHERE status IN ('queued', 'running')"
            )
            await db.commit()

    async def create(
        self,
        cmd,
        *,
        status="queued",
        cmd_hash=None,
        agent_id=None,
        command_type="run",
        command_preview="",
    ):
        attempts = 1 if cmd_hash is not None else 32
        for _ in range(attempts):
            h = cmd_hash or secrets.token_hex(4)
            command = Command(h, cmd, status)
            try:
                async with self._connect() as db:
                    await db.execute(
                        "INSERT INTO commands VALUES(?,?,?,?,?,?)",
                        (h, cmd, status, None, None, None),
                    )
                    if agent_id:
                        await db.execute(
                            "INSERT INTO command_agent_attribution VALUES(?,?,?,?,?)",
                            (h, agent_id, utc_text(), command_type, command_preview),
                        )
                    await db.commit()
                return command
            except IntegrityError:
                if cmd_hash is not None:
                    raise
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def update(self, command):
        async with self._connect() as db:
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
        async with self._connect() as db:
            await db.execute("DELETE FROM lines WHERE hash=?", (cmd_hash,))
            await db.execute(
                "DELETE FROM command_agent_attribution WHERE command_hash=?", (cmd_hash,)
            )
            await db.execute("DELETE FROM commands WHERE hash=?", (cmd_hash,))
            await db.commit()

    async def get(self, cmd_hash):
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT hash,cmd,status,pid,exit_code,error FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        return Command(*row) if row else None

    async def append_line(self, cmd_hash, text):
        appeared_at = datetime.now(UTC).strftime("%H:%M:%SZ")
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO lines(hash,appeared_at,text) VALUES(?,?,?)",
                (cmd_hash, appeared_at, text),
            )
            await db.commit()

    async def count_lines(self, cmd_hash):
        async with self._connect() as db:
            row = await (
                await db.execute("SELECT COUNT(*) FROM lines WHERE hash=?", (cmd_hash,))
            ).fetchone()
        return int(row[0])

    async def read_command_lines(self, cmd_hash, limit, offset):
        query = (
            "SELECT seq,hash,appeared_at,text FROM lines WHERE hash=? ORDER BY seq LIMIT ? OFFSET ?"
        )
        async with self._connect() as db:
            rows = await (await db.execute(query, (cmd_hash, limit, offset))).fetchall()
        return [Line(*row) for row in rows]

    async def read_global_after_cursor(self, limit, cursor):
        query = "SELECT seq,hash,appeared_at,text FROM lines WHERE seq>? ORDER BY seq LIMIT ?"
        async with self._connect() as db:
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
        async with self._connect() as db:
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

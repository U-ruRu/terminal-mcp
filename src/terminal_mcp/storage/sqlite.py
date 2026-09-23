# ruff: noqa: E501
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlite3 import IntegrityError

import aiosqlite

from terminal_mcp.core.models import Command
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.output import (
    DEFAULT_COMMAND_MAX_BYTES,
    DEFAULT_LINE_MAX_BYTES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    DEFAULT_TARGET_BYTES,
    OutputStore,
)
from terminal_mcp.storage.permissions import secure_database_path

_COMMAND_COLUMNS = (
    "hash,cmd,status,pid,exit_code,error,started_at,finished_at,"
    "queue_id,queue_sequence,enqueued_at,claimed_at"
)


class SqliteRepository:
    def __init__(
        self,
        path,
        output_path=None,
        *,
        output_line_max_bytes=DEFAULT_LINE_MAX_BYTES,
        output_command_max_bytes=DEFAULT_COMMAND_MAX_BYTES,
        output_target_bytes=DEFAULT_TARGET_BYTES,
        output_max_bytes=DEFAULT_MAX_BYTES,
        output_max_rows=DEFAULT_MAX_ROWS,
    ):
        self.path = path
        output_path = output_path or Path(path).with_name("output.sqlite3")
        self.output = OutputStore(
            output_path,
            line_max_bytes=output_line_max_bytes,
            command_max_bytes=output_command_max_bytes,
            target_bytes=output_target_bytes,
            max_bytes=output_max_bytes,
            max_rows=output_max_rows,
        )
        self.output_line_max_bytes = self.output.line_max_bytes
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
        await self.output.initialize()
        async with self._connect("initialize") as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands(
                    hash TEXT PRIMARY KEY, cmd TEXT, status TEXT,
                    pid INTEGER, exit_code INTEGER, error TEXT,
                    started_at TEXT, finished_at TEXT,
                    queue_id INTEGER, queue_sequence INTEGER,
                    enqueued_at TEXT, claimed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS command_output_state(
                    command_hash TEXT PRIMARY KEY,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    pruned_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_sessions(
                    agent_id TEXT PRIMARY KEY, registered_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                    task_summary TEXT NOT NULL, intent TEXT NOT NULL, work_scope TEXT NOT NULL, state TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '[]', current_step INTEGER NOT NULL DEFAULT 1,
                    ended_at TEXT, end_reason TEXT, preferred_queue_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS agent_task_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    intent TEXT NOT NULL, work_scope TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS command_agent_attribution(
                    command_hash TEXT PRIMARY KEY, agent_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    command_type TEXT NOT NULL, command_preview TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_activity_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    tool TEXT NOT NULL, command_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS coordination_messages(
                    message_hash TEXT PRIMARY KEY, sender_agent_id TEXT NOT NULL,
                    target_name TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    require_reply INTEGER NOT NULL DEFAULT 0, alert INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS coordination_message_recipients(
                    message_hash TEXT NOT NULL, recipient_agent_id TEXT NOT NULL,
                    delivered_at TEXT, first_seen_at TEXT, last_seen_at TEXT,
                    seen_count INTEGER NOT NULL DEFAULT 0, read_at TEXT,
                    replied_at TEXT, reply_message_hash TEXT,
                    PRIMARY KEY(message_hash, recipient_agent_id)
                );
                CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_activity ON agent_sessions(last_activity_at DESC);
                CREATE INDEX IF NOT EXISTS ix_agent_task_events_agent ON agent_task_events(agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_agent ON command_agent_attribution(agent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_hash ON command_agent_attribution(command_hash);
                CREATE INDEX IF NOT EXISTS ix_agent_activity_agent ON agent_activity_events(agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS ix_coord_message_recipient_legacy
                    ON coordination_message_recipients(recipient_agent_id, read_at);
                """
            )
            await self._migrate(db)
            legacy_output_migrated = await self._migrate_legacy_output(db)
            recovered_at = utc_text()
            await db.execute(
                "UPDATE commands SET status='failed', error='startup.recover: application restarted', "
                "finished_at=COALESCE(finished_at, ?) WHERE status IN ('queued', 'running')",
                (recovered_at,),
            )
            await db.execute("PRAGMA user_version=5")
            await db.commit()
            if legacy_output_migrated:
                await db.execute("VACUUM")

    async def _migrate(self, db):
        async def add_columns(table, definitions):
            columns = {
                row[1] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            }
            for name, definition in definitions:
                if name not in columns:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            return columns

        command_columns = await add_columns(
            "commands",
            [
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("queue_id", "INTEGER"),
                ("queue_sequence", "INTEGER"),
                ("enqueued_at", "TEXT"),
                ("claimed_at", "TEXT"),
            ],
        )
        session_columns = await add_columns(
            "agent_sessions",
            [
                ("details", "TEXT NOT NULL DEFAULT '[]'"),
                ("current_step", "INTEGER NOT NULL DEFAULT 1"),
                ("ended_at", "TEXT"),
                ("end_reason", "TEXT"),
                ("preferred_queue_id", "INTEGER"),
            ],
        )
        await add_columns("agent_task_events", [("step", "INTEGER NOT NULL DEFAULT 1")])
        await add_columns(
            "coordination_messages",
            [
                ("require_reply", "INTEGER NOT NULL DEFAULT 0"),
                ("alert", "INTEGER NOT NULL DEFAULT 0"),
            ],
        )
        recipient_columns = await add_columns(
            "coordination_message_recipients",
            [
                ("delivered_at", "TEXT"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("seen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("replied_at", "TEXT"),
                ("reply_message_hash", "TEXT"),
            ],
        )
        if "details" not in session_columns:
            await db.execute(
                "UPDATE agent_sessions SET state='forced',ended_at=COALESCE(ended_at,last_activity_at),"
                "end_reason='schema_upgrade' WHERE state='active'"
            )
        await db.execute(
            "UPDATE agent_sessions SET state='forced',ended_at=COALESCE(ended_at,last_activity_at),"
            "end_reason=COALESCE(end_reason,'legacy_expired') WHERE state='expired'"
        )
        if "queue_id" not in command_columns:
            await db.execute(
                "UPDATE commands SET queue_id=1, queue_sequence=rowid, enqueued_at=COALESCE(enqueued_at, started_at) "
                "WHERE queue_id IS NULL AND status='queued'"
            )
        if "delivered_at" not in recipient_columns:
            await db.execute(
                "UPDATE coordination_message_recipients SET delivered_at=("
                "SELECT created_at FROM coordination_messages m WHERE m.message_hash=coordination_message_recipients.message_hash"
                ") WHERE delivered_at IS NULL"
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_commands_queue ON commands(queue_id, status, queue_sequence)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_coord_message_recipient "
            "ON coordination_message_recipients(recipient_agent_id, read_at, replied_at)"
        )

    async def _migrate_legacy_output(self, db):
        table = await (
            await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lines'")
        ).fetchone()
        if table is None:
            return False
        count = int((await (await db.execute("SELECT COUNT(*) FROM lines")).fetchone())[0])
        if count == 0:
            await db.execute("DROP INDEX IF EXISTS ix_lines_hash_seq")
            await db.execute("DROP INDEX IF EXISTS idx_lines_hash_seq")
            await db.execute("DROP TABLE lines")
            return True

        # A failed migration can be retried safely because the legacy table remains authoritative
        # until the import succeeds and the final DROP TABLE commits.
        await self.output.reset()
        aggregate = await (
            await db.execute(
                "SELECT hash,MAX(seq),SUM(CASE WHEN length(CAST(text AS BLOB))>? THEN ? "
                "ELSE length(CAST(text AS BLOB)) END) FROM lines GROUP BY hash ORDER BY MAX(seq) DESC",
                (self.output.line_max_bytes, self.output.line_max_bytes),
            )
        ).fetchall()
        selected = []
        budget = 0
        for cmd_hash, _last_seq, clipped_bytes in aggregate:
            effective = min(int(clipped_bytes or 0), self.output.command_max_bytes)
            if selected and budget + effective > self.output.target_bytes:
                break
            selected.append(cmd_hash)
            budget += effective
            if budget >= self.output.target_bytes:
                break

        truncated_hashes = set()
        for start in range(0, len(selected), 250):
            chunk = selected[start : start + 250]
            marks = ",".join("?" for _ in chunk)
            cursor = await db.execute(
                f"SELECT seq,hash,appeared_at,text FROM lines WHERE hash IN ({marks}) ORDER BY seq",
                chunk,
            )
            pending = {}
            while True:
                rows = await cursor.fetchmany(256)
                if not rows:
                    break
                for seq, cmd_hash, appeared_at, text in rows:
                    values = pending.setdefault(cmd_hash, [])
                    values.append((int(seq), appeared_at or "00:00:00", text or ""))
                    if len(values) >= 64:
                        result = await self.output.append_records(cmd_hash, values)
                        if result["truncated"]:
                            truncated_hashes.add(cmd_hash)
                        pending[cmd_hash] = []
            for cmd_hash, records in pending.items():
                if records:
                    result = await self.output.append_records(cmd_hash, records)
                    if result["truncated"]:
                        truncated_hashes.add(cmd_hash)

        migrated_at = utc_text()
        selected_set = set(selected)
        pruned_hashes = [row[0] for row in aggregate if row[0] not in selected_set]
        if truncated_hashes:
            await db.executemany(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,1,NULL) "
                "ON CONFLICT(command_hash) DO UPDATE SET truncated=1",
                [(value,) for value in sorted(truncated_hashes)],
            )
        if pruned_hashes:
            await db.executemany(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,0,?) "
                "ON CONFLICT(command_hash) DO UPDATE SET pruned_at=excluded.pruned_at",
                [(value, migrated_at) for value in pruned_hashes],
            )
        await db.execute("DROP INDEX IF EXISTS ix_lines_hash_seq")
        await db.execute("DROP INDEX IF EXISTS idx_lines_hash_seq")
        await db.execute("DROP TABLE lines")
        return True

    async def create(
        self,
        cmd,
        *,
        status="queued",
        cmd_hash=None,
        agent_id=None,
        command_type="run",
        command_preview="",
        queue_id=None,
    ):
        attempts = 1 if cmd_hash is not None else 32
        for _ in range(attempts):
            h = cmd_hash or secrets.token_hex(4)
            now = utc_text()
            effective_queue = (queue_id or 1) if status == "queued" else queue_id
            try:
                async with self._connect("create_command") as db:
                    if status == "queued":
                        await db.execute("BEGIN IMMEDIATE")
                        row = await (
                            await db.execute(
                                "SELECT COALESCE(MAX(queue_sequence),0)+1 FROM commands WHERE queue_id=?",
                                (effective_queue,),
                            )
                        ).fetchone()
                        queue_sequence = int(row[0])
                    else:
                        queue_sequence = None
                    started_at = now if status == "running" else None
                    finished_at = now if status in {"completed", "failed", "cancelled"} else None
                    enqueued_at = now if status == "queued" else None
                    claimed_at = now if status == "running" else None
                    await db.execute(
                        "INSERT INTO commands("
                        "hash,cmd,status,pid,exit_code,error,started_at,finished_at,"
                        "queue_id,queue_sequence,enqueued_at,claimed_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            h,
                            cmd,
                            status,
                            None,
                            None,
                            None,
                            started_at,
                            finished_at,
                            effective_queue,
                            queue_sequence,
                            enqueued_at,
                            claimed_at,
                        ),
                    )
                    if agent_id:
                        await db.execute(
                            "INSERT INTO command_agent_attribution VALUES(?,?,?,?,?)",
                            (h, agent_id, now, command_type, command_preview),
                        )
                    await db.commit()
                return Command(
                    h,
                    cmd,
                    status,
                    started_at=started_at,
                    finished_at=finished_at,
                    queue_id=effective_queue,
                    queue_sequence=queue_sequence,
                    enqueued_at=enqueued_at,
                    claimed_at=claimed_at,
                )
            except IntegrityError:
                if cmd_hash is not None:
                    raise
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def update(self, command):
        """Compatibility update. Execution paths use compare-and-set helpers below."""
        now = utc_text()
        if command.status == "running" and command.started_at is None:
            command.started_at = now
        if command.status in {"completed", "failed", "cancelled"} and command.finished_at is None:
            command.finished_at = now
        async with self._connect("update_command") as db:
            await db.execute(
                "UPDATE commands SET status=?,pid=?,exit_code=?,error=?,started_at=?,finished_at=?,"
                "queue_id=?,queue_sequence=?,enqueued_at=?,claimed_at=? WHERE hash=?",
                (
                    command.status,
                    command.pid,
                    command.exit_code,
                    command.error,
                    command.started_at,
                    command.finished_at,
                    command.queue_id,
                    command.queue_sequence,
                    command.enqueued_at,
                    command.claimed_at,
                    command.cmd_hash,
                ),
            )
            await db.commit()

    async def claim_next(self, queue_id):
        now = utc_text()
        async with self._connect("claim_command") as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT hash FROM commands WHERE queue_id=? AND status='queued' "
                    "ORDER BY queue_sequence,rowid LIMIT 1",
                    (queue_id,),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return None
            cmd_hash = row[0]
            cur = await db.execute(
                "UPDATE commands SET status='running', started_at=COALESCE(started_at,?), claimed_at=? "
                "WHERE hash=? AND status='queued'",
                (now, now, cmd_hash),
            )
            if cur.rowcount != 1:
                await db.rollback()
                return None
            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?", (cmd_hash,)
                )
            ).fetchone()
            await db.commit()
        return Command(*row)

    async def set_pid(self, cmd_hash, pid):
        async with self._connect("set_pid") as db:
            cur = await db.execute(
                "UPDATE commands SET pid=? WHERE hash=? AND status='running'", (pid, cmd_hash)
            )
            await db.commit()
        return cur.rowcount == 1

    async def finish_running(self, cmd_hash, status, exit_code=None, error=None):
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("finish_running requires a terminal status")
        finished_at = utc_text()
        async with self._connect("finish_command") as db:
            cur = await db.execute(
                "UPDATE commands SET status=?,exit_code=?,error=?,finished_at=? "
                "WHERE hash=? AND status='running'",
                (status, exit_code, error, finished_at, cmd_hash),
            )
            await db.commit()
        return cur.rowcount == 1

    async def cancel_queued(self, cmd_hash):
        finished_at = utc_text()
        async with self._connect("cancel_queued") as db:
            cur = await db.execute(
                "UPDATE commands SET status='cancelled',error=NULL,finished_at=? "
                "WHERE hash=? AND status='queued'",
                (finished_at, cmd_hash),
            )
            await db.commit()
        return cur.rowcount == 1

    async def queue_position(self, cmd_hash):
        async with self._connect("queue_position") as db:
            row = await (
                await db.execute(
                    "SELECT queue_id,queue_sequence,status FROM commands WHERE hash=?", (cmd_hash,)
                )
            ).fetchone()
            if row is None:
                return None
            queue_id, sequence, status = row
            if status != "queued" or queue_id is None or sequence is None:
                return None
            count = await (
                await db.execute(
                    "SELECT COUNT(*) FROM commands WHERE queue_id=? AND status='queued' AND queue_sequence<=?",
                    (queue_id, sequence),
                )
            ).fetchone()
        return int(count[0])

    async def queue_loads(self, queue_count):
        loads = {queue_id: 0 for queue_id in range(1, queue_count + 1)}
        async with self._connect("queue_loads") as db:
            rows = await (
                await db.execute(
                    "SELECT queue_id,COUNT(*) FROM commands WHERE status IN ('queued','running') "
                    "AND queue_id IS NOT NULL GROUP BY queue_id"
                )
            ).fetchall()
        for queue_id, count in rows:
            if queue_id in loads:
                loads[queue_id] = int(count)
        return loads

    async def queue_snapshot(self, queue_count):
        result = {
            queue_id: {"queue_id": queue_id, "running": None, "queued": 0}
            for queue_id in range(1, queue_count + 1)
        }
        async with self._connect("queue_snapshot") as db:
            queued = await (
                await db.execute(
                    "SELECT queue_id,COUNT(*) FROM commands WHERE status='queued' AND queue_id IS NOT NULL GROUP BY queue_id"
                )
            ).fetchall()
            running = await (
                await db.execute(
                    "SELECT queue_id,hash FROM commands WHERE status='running' AND queue_id IS NOT NULL ORDER BY claimed_at"
                )
            ).fetchall()
        for queue_id, count in queued:
            if queue_id in result:
                result[queue_id]["queued"] = int(count)
        for queue_id, cmd_hash in running:
            if queue_id in result and result[queue_id]["running"] is None:
                result[queue_id]["running"] = cmd_hash
        return [result[i] for i in sorted(result)]

    async def command_queue_ids(self, hashes):
        values = list(dict.fromkeys(hashes))
        if not values:
            return {}
        result = {}
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            async with self._connect("command_queue_ids") as db:
                rows = await (
                    await db.execute(
                        f"SELECT hash,queue_id FROM commands WHERE hash IN ({marks})", chunk
                    )
                ).fetchall()
            result.update(rows)
        return result

    async def delete_command(self, cmd_hash):
        await self.output.delete_command(cmd_hash)
        async with self._connect("delete_command") as db:
            await db.execute(
                "DELETE FROM command_agent_attribution WHERE command_hash=?", (cmd_hash,)
            )
            await db.execute("DELETE FROM command_output_state WHERE command_hash=?", (cmd_hash,))
            await db.execute("DELETE FROM commands WHERE hash=?", (cmd_hash,))
            await db.commit()

    async def get(self, cmd_hash):
        async with self._connect("get_command") as db:
            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?", (cmd_hash,)
                )
            ).fetchone()
        return Command(*row) if row else None

    async def mark_output_truncated(self, cmd_hash):
        async with self._connect("mark_output_truncated") as db:
            await db.execute(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,1,NULL) "
                "ON CONFLICT(command_hash) DO UPDATE SET truncated=1",
                (cmd_hash,),
            )
            await db.commit()

    async def append_lines(self, cmd_hash, texts):
        result = await self.output.append_lines(cmd_hash, list(texts))
        if result["truncated"]:
            await self.mark_output_truncated(cmd_hash)
        return result

    async def append_line(self, cmd_hash, text):
        return await self.append_lines(cmd_hash, [text])

    async def output_status(self, cmd_hash):
        meta = await self.output.command_meta(cmd_hash)
        async with self._connect("output_status") as db:
            state = await (
                await db.execute(
                    "SELECT truncated,pruned_at FROM command_output_state WHERE command_hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        durable_truncated = bool(state[0]) if state else False
        pruned_at = state[1] if state else None
        return {
            "output_truncated": durable_truncated or bool(meta and meta["truncated"]),
            "output_retained": pruned_at is None,
            "output_pruned_at": pruned_at,
            "output_bytes": int(meta["stored_bytes"]) if meta else 0,
        }

    async def prune_output_cache(self):
        async with self._connect("output_active_hashes") as db:
            rows = await (
                await db.execute("SELECT hash FROM commands WHERE status IN ('queued','running')")
            ).fetchall()
        pruned = await self.output.prune({row[0] for row in rows})
        if pruned:
            stamp = utc_text()
            async with self._connect("mark_output_pruned") as db:
                await db.executemany(
                    "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,0,?) "
                    "ON CONFLICT(command_hash) DO UPDATE SET pruned_at=excluded.pruned_at",
                    [(cmd_hash, stamp) for cmd_hash in pruned],
                )
                await db.commit()
        return pruned

    async def output_cache_stats(self):
        return await self.output.stats()

    async def count_lines(self, cmd_hash):
        return await self.output.count_lines(cmd_hash)

    async def read_command_lines(self, cmd_hash, limit, offset):
        return await self.output.read_command_lines(cmd_hash, limit, offset)

    async def read_global_after_cursor(self, limit, cursor):
        return await self.output.read_global_after_cursor(limit, cursor)

    async def read_global_tail(self, limit, distance_from_end=None):
        return await self.output.read_global_tail(limit, distance_from_end)

    async def read_lines(self, cmd_hash, limit, offset):
        if cmd_hash:
            return await self.read_command_lines(cmd_hash, limit, offset)
        return await self.read_global_after_cursor(limit, offset)

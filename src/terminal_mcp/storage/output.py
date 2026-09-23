from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from terminal_mcp.core.models import Line
from terminal_mcp.storage.permissions import secure_database_path

MIB = 1024 * 1024
DEFAULT_LINE_MAX_BYTES = 4 * MIB
DEFAULT_COMMAND_MAX_BYTES = 8 * MIB
DEFAULT_TARGET_BYTES = 192 * MIB
DEFAULT_MAX_BYTES = 256 * MIB
DEFAULT_MAX_ROWS = 1_000_000

LINE_TRUNCATED_SUFFIX = " … [truncated: line exceeded 4 MiB]"
COMMAND_TRUNCATED_SUFFIX = " … [truncated: command output exceeded 8 MiB]"


def _clip_utf8(text: str, max_bytes: int, suffix: str) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    suffix_raw = suffix.encode("utf-8")
    if max_bytes <= len(suffix_raw):
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    prefix = raw[: max_bytes - len(suffix_raw)].decode("utf-8", errors="ignore")
    return prefix + suffix, True


class OutputStore:
    def __init__(
        self,
        path: Path,
        *,
        line_max_bytes: int = DEFAULT_LINE_MAX_BYTES,
        command_max_bytes: int = DEFAULT_COMMAND_MAX_BYTES,
        target_bytes: int = DEFAULT_TARGET_BYTES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ):
        self.path = Path(path)
        self.line_max_bytes = int(line_max_bytes)
        self.command_max_bytes = int(command_max_bytes)
        self.target_bytes = int(target_bytes)
        self.max_bytes = int(max_bytes)
        self.max_rows = int(max_rows)
        if self.line_max_bytes <= 0 or self.command_max_bytes <= 0:
            raise ValueError("output line/command limits must be positive")
        if self.line_max_bytes > self.command_max_bytes:
            raise ValueError("output line limit must not exceed command limit")
        if self.target_bytes <= 0 or self.max_bytes <= 0 or self.target_bytes > self.max_bytes:
            raise ValueError("output target must be positive and not exceed max bytes")
        if self.max_rows <= 0:
            raise ValueError("output max rows must be positive")

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.path, timeout=2.0)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=2000")
            yield db
        finally:
            await db.close()

    async def initialize(self):
        secure_database_path(self.path)
        async with self._connect() as db:
            pages = int((await (await db.execute("PRAGMA page_count")).fetchone())[0])
            if pages <= 1:
                await db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lines(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash TEXT NOT NULL,
                    appeared_at TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_lines_hash_seq ON lines(hash, seq);
                CREATE TABLE IF NOT EXISTS output_meta(
                    hash TEXT PRIMARY KEY,
                    stored_bytes INTEGER NOT NULL DEFAULT 0,
                    stored_lines INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    last_seq INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cache_state(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                PRAGMA user_version=1;
                """
            )
            await db.commit()

    async def append_lines(self, cmd_hash: str, texts: list[str]):
        if not texts:
            return {"accepting": True, "truncated": False, "stored_bytes": 0, "stored_lines": 0}
        appeared_at = datetime.now(UTC).strftime("%H:%M:%S")
        return await self.append_records(cmd_hash, [(None, appeared_at, text) for text in texts])

    async def append_records(self, cmd_hash: str, records: list[tuple[int | None, str, str]]):
        if not records:
            return {"accepting": True, "truncated": False, "stored_bytes": 0, "stored_lines": 0}
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT stored_bytes,stored_lines,truncated,last_seq "
                    "FROM output_meta WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
            stored_bytes, stored_lines, truncated, last_seq = (
                (int(row[0]), int(row[1]), bool(row[2]), int(row[3])) if row else (0, 0, False, 0)
            )
            accepting = stored_bytes < self.command_max_bytes
            inserts: list[tuple] = []
            explicit = records[0][0] is not None
            for explicit_seq, appeared_at, text in records:
                if not accepting:
                    truncated = True
                    break
                clipped, line_truncated = _clip_utf8(
                    text, self.line_max_bytes, LINE_TRUNCATED_SUFFIX
                )
                encoded = clipped.encode("utf-8")
                remaining = self.command_max_bytes - stored_bytes
                command_truncated = len(encoded) > remaining
                if command_truncated:
                    clipped, _ = _clip_utf8(clipped, remaining, COMMAND_TRUNCATED_SUFFIX)
                    encoded = clipped.encode("utf-8")
                if explicit_seq is None:
                    inserts.append((cmd_hash, appeared_at, clipped))
                else:
                    inserts.append((explicit_seq, cmd_hash, appeared_at, clipped))
                    last_seq = max(last_seq, int(explicit_seq))
                stored_bytes += len(encoded)
                stored_lines += 1
                truncated = truncated or line_truncated or command_truncated
                if stored_bytes >= self.command_max_bytes or command_truncated:
                    accepting = False
                    break
            if inserts:
                if explicit:
                    await db.executemany(
                        "INSERT OR IGNORE INTO lines(seq,hash,appeared_at,text) VALUES(?,?,?,?)",
                        inserts,
                    )
                else:
                    await db.executemany(
                        "INSERT INTO lines(hash,appeared_at,text) VALUES(?,?,?)", inserts
                    )
                    last_row = await (await db.execute("SELECT last_insert_rowid()")).fetchone()
                    if last_row:
                        last_seq = max(last_seq, int(last_row[0]))
            await db.execute(
                "INSERT INTO output_meta("
                "hash,stored_bytes,stored_lines,truncated,last_seq"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(hash) DO UPDATE SET stored_bytes=excluded.stored_bytes,"
                "stored_lines=excluded.stored_lines,truncated=excluded.truncated,"
                "last_seq=MAX(output_meta.last_seq,excluded.last_seq)",
                (cmd_hash, stored_bytes, stored_lines, int(truncated), last_seq),
            )
            await db.commit()
        return {
            "accepting": accepting,
            "truncated": truncated,
            "stored_bytes": stored_bytes,
            "stored_lines": stored_lines,
        }

    async def command_meta(self, cmd_hash: str):
        async with self._connect() as db:
            row = await (
                await db.execute(
                    "SELECT stored_bytes,stored_lines,truncated,last_seq "
                    "FROM output_meta WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "stored_bytes": int(row[0]),
            "stored_lines": int(row[1]),
            "truncated": bool(row[2]),
            "last_seq": int(row[3]),
        }

    async def count_lines(self, cmd_hash: str):
        meta = await self.command_meta(cmd_hash)
        return int(meta["stored_lines"]) if meta else 0

    async def read_command_lines(self, cmd_hash: str, limit: int, offset: int):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT seq,hash,appeared_at,text FROM lines WHERE hash=? "
                    "ORDER BY seq LIMIT ? OFFSET ?",
                    (cmd_hash, limit, offset),
                )
            ).fetchall()
        return [Line(*row) for row in rows]

    async def read_global_after_cursor(self, limit: int, cursor: int):
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT seq,hash,appeared_at,text FROM lines WHERE seq>? ORDER BY seq LIMIT ?",
                    (cursor, limit),
                )
            ).fetchall()
        return [Line(*row) for row in rows]

    async def read_global_tail(self, limit: int, distance_from_end=None):
        if distance_from_end is None:
            take, skip = limit, 0
        else:
            distance = max(0, distance_from_end)
            take = min(limit, distance)
            skip = max(distance - take, 0)
        if take == 0:
            return []
        async with self._connect() as db:
            rows = await (
                await db.execute(
                    "SELECT seq,hash,appeared_at,text FROM lines "
                    "ORDER BY seq DESC LIMIT ? OFFSET ?",
                    (take, skip),
                )
            ).fetchall()
            if distance_from_end is not None and skip > 0 and len(rows) < take:
                rows = await (
                    await db.execute(
                        "SELECT seq,hash,appeared_at,text FROM lines ORDER BY seq LIMIT ?", (limit,)
                    )
                ).fetchall()
                return [Line(*row) for row in rows]
        rows.reverse()
        return [Line(*row) for row in rows]

    async def reset(self):
        async with self._connect() as db:
            await db.executescript(
                "DELETE FROM lines; DELETE FROM output_meta; DELETE FROM cache_state;"
            )
            await db.commit()
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await db.execute("PRAGMA incremental_vacuum(4096)")

    async def delete_command(self, cmd_hash: str):
        async with self._connect() as db:
            await db.execute("DELETE FROM lines WHERE hash=?", (cmd_hash,))
            await db.execute("DELETE FROM output_meta WHERE hash=?", (cmd_hash,))
            await db.commit()

    async def stats(self):
        async with self._connect() as db:
            totals = await (
                await db.execute(
                    "SELECT COALESCE(SUM(stored_bytes),0),COALESCE(SUM(stored_lines),0),COUNT(*),"
                    "COALESCE(SUM(truncated),0) FROM output_meta"
                )
            ).fetchone()
            state = await (
                await db.execute("SELECT value FROM cache_state WHERE key='last_prune_at'")
            ).fetchone()
            page_size = int((await (await db.execute("PRAGMA page_size")).fetchone())[0])
            page_count = int((await (await db.execute("PRAGMA page_count")).fetchone())[0])
            freelist = int((await (await db.execute("PRAGMA freelist_count")).fetchone())[0])
        return {
            "used_bytes": int(totals[0]),
            "target_bytes": self.target_bytes,
            "max_bytes": self.max_bytes,
            "lines": int(totals[1]),
            "max_lines": self.max_rows,
            "retained_commands": int(totals[2]),
            "truncated_commands": int(totals[3]),
            "allocated_bytes": page_size * page_count,
            "live_page_bytes": page_size * max(0, page_count - freelist),
            "last_prune_at": state[0] if state else None,
        }

    async def prune(self, active_hashes: set[str] | None = None, *, force=False):
        active_hashes = active_hashes or set()
        stats = await self.stats()
        if not force and stats["used_bytes"] <= self.max_bytes and stats["lines"] <= self.max_rows:
            return []
        used = stats["used_bytes"]
        lines = stats["lines"]
        pruned: list[str] = []
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            candidates = await (
                await db.execute(
                    "SELECT hash,stored_bytes,stored_lines FROM output_meta ORDER BY last_seq,hash"
                )
            ).fetchall()
            for cmd_hash, stored_bytes, stored_lines in candidates:
                if used <= self.target_bytes and lines <= self.max_rows:
                    break
                if cmd_hash in active_hashes:
                    continue
                await db.execute("DELETE FROM lines WHERE hash=?", (cmd_hash,))
                await db.execute("DELETE FROM output_meta WHERE hash=?", (cmd_hash,))
                used -= int(stored_bytes)
                lines -= int(stored_lines)
                pruned.append(cmd_hash)
            if pruned:
                stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                await db.execute(
                    "INSERT INTO cache_state(key,value) VALUES('last_prune_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (stamp,),
                )
            await db.commit()
            if pruned:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await db.execute("PRAGMA incremental_vacuum(4096)")
        return pruned

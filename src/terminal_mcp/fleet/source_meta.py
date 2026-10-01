from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.protocol import validate_protocol_id
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection


@dataclass(frozen=True, slots=True)
class SourceMeta:
    fleet_id: str
    node_id: str
    source_stream_generation: str
    served_high_water: int
    updated_at: str


class FleetNodeMetaStore:
    """Critical node-local identity/generation state outside the runtime database."""

    SCHEMA_VERSION = 1

    def __init__(self, path, *, fleet_id: str, node_id: str):
        self.path = path
        self.fleet_id = validate_protocol_id(fleet_id, "fleet_id")
        self.node_id = validate_protocol_id(node_id, "node_id")
        self.sqlite_diagnostics = SqliteDiagnostics("fleet_node_meta")
        self._observe_lock = asyncio.Lock()

    def configure_observability(self, events, metrics) -> None:
        self.sqlite_diagnostics.configure(events, metrics)

    @staticmethod
    def _new_generation() -> str:
        return uuid.uuid4().hex

    @asynccontextmanager
    async def _connect(self, operation: str):
        secure_database_path(self.path)
        async with observed_connection(
            aiosqlite.connect,
            self.path,
            busy_timeout=1.0,
            diagnostics=self.sqlite_diagnostics,
            operation=operation,
            pragmas=(
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=1000",
            ),
        ) as db:
            yield db

    async def initialize(self) -> SourceMeta:
        stamp = utc_text()
        async with self._connect("fleet_node_meta_initialize") as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "CREATE TABLE IF NOT EXISTS meta_schema("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS source_meta("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "fleet_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "source_stream_generation TEXT NOT NULL,"
                "served_high_water INTEGER NOT NULL DEFAULT 0 CHECK(served_high_water>=0),"
                "updated_at TEXT NOT NULL)"
            )
            row = await (
                await db.execute(
                    "SELECT fleet_id,node_id,source_stream_generation,served_high_water,updated_at "
                    "FROM source_meta WHERE singleton=1"
                )
            ).fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO source_meta(singleton,fleet_id,node_id,source_stream_generation,"
                    "served_high_water,updated_at) VALUES(1,?,?,?,?,?)",
                    (self.fleet_id, self.node_id, self._new_generation(), 0, stamp),
                )
                await db.execute(
                    "INSERT INTO meta_schema(singleton,version) VALUES(1,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
                    (self.SCHEMA_VERSION,),
                )
            else:
                if row[0] != self.fleet_id or row[1] != self.node_id:
                    await db.rollback()
                    raise RuntimeError(
                        "fleet node identity does not match persisted fleet_node_meta; "
                        "use a fresh node-meta store for a replacement node"
                    )
                version = await (
                    await db.execute("SELECT version FROM meta_schema WHERE singleton=1")
                ).fetchone()
                if version is None or int(version[0]) != self.SCHEMA_VERSION:
                    await db.rollback()
                    raise RuntimeError("unsupported fleet_node_meta schema version")
            await db.commit()
        return await self.get()

    async def get(self) -> SourceMeta:
        async with self._connect("fleet_node_meta_get") as db:
            row = await (
                await db.execute(
                    "SELECT fleet_id,node_id,source_stream_generation,served_high_water,updated_at "
                    "FROM source_meta WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("fleet_node_meta is not initialized")
        return SourceMeta(row[0], row[1], row[2], int(row[3]), row[4])

    async def observe_journal(self, high_water: int) -> tuple[SourceMeta, bool]:
        high_water = int(high_water)
        if high_water < 0:
            raise ValueError("high_water must be non-negative")
        async with self._observe_lock:
            async with self._connect("fleet_node_meta_observe") as db:
                row = await (
                    await db.execute(
                        "SELECT fleet_id,node_id,source_stream_generation,"
                        "served_high_water,updated_at "
                        "FROM source_meta WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeError("fleet_node_meta is not initialized")
                current = SourceMeta(row[0], row[1], str(row[2]), int(row[3]), row[4])
                if high_water == current.served_high_water:
                    return current, False

                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT source_stream_generation,served_high_water "
                        "FROM source_meta WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    await db.rollback()
                    raise RuntimeError("fleet_node_meta is not initialized")
                generation, served = str(row[0]), int(row[1])
                rotated = False
                if high_water < served:
                    generation = self._new_generation()
                    served = high_water
                    rotated = True
                else:
                    served = max(served, high_water)
                stamp = utc_text()
                await db.execute(
                    "UPDATE source_meta SET source_stream_generation=?,served_high_water=?,"
                    "updated_at=? "
                    "WHERE singleton=1",
                    (generation, served, stamp),
                )
                await db.commit()
                return SourceMeta(self.fleet_id, self.node_id, generation, served, stamp), rotated

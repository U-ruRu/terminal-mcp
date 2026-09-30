from __future__ import annotations

import json
from contextlib import asynccontextmanager

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.protocol import validate_capabilities, validate_protocol_id
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection


class FleetControlError(RuntimeError):
    pass


class FleetControlStore:
    """Critical monotonic Fleet routing metadata; never owns slot/session truth."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path,
        *,
        fleet_id: str,
        node_id: str,
        control_node_id: str,
    ):
        self.path = path
        self.fleet_id = validate_protocol_id(fleet_id, "fleet_id")
        self.node_id = validate_protocol_id(node_id, "node_id")
        self.control_node_id = validate_protocol_id(control_node_id, "control_node_id")
        self.sqlite_diagnostics = SqliteDiagnostics("fleet_control")

    def configure_observability(self, events, metrics) -> None:
        self.sqlite_diagnostics.configure(events, metrics)

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
                "PRAGMA foreign_keys=ON",
            ),
        ) as db:
            yield db

    async def initialize(self) -> None:
        stamp = utc_text()
        async with self._connect("fleet_control_initialize") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS control_schema(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS control_meta(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        fleet_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        control_node_id TEXT NOT NULL,
                        routing_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(routing_revision>=0),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS fleet_members(
                        node_id TEXT PRIMARY KEY,
                        capabilities_json TEXT NOT NULL DEFAULT '[]',
                        state TEXT NOT NULL CHECK(state IN ('active','draining','offline')),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS authority_routes(
                        logical_agent_id TEXT PRIMARY KEY,
                        authority_node_id TEXT NOT NULL,
                        authority_epoch INTEGER NOT NULL CHECK(authority_epoch>0),
                        routing_revision INTEGER NOT NULL CHECK(routing_revision>0),
                        state TEXT NOT NULL DEFAULT 'active'
                            CHECK(state IN (
                                'active','prepare','imported','committed',
                                'recovery_required'
                            )),
                        target_node_id TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS authority_transfers(
                        logical_agent_id TEXT PRIMARY KEY,
                        from_node_id TEXT NOT NULL,
                        to_node_id TEXT NOT NULL,
                        from_authority_epoch INTEGER NOT NULL CHECK(from_authority_epoch>0),
                        target_authority_epoch INTEGER NOT NULL
                            CHECK(target_authority_epoch>0),
                        routing_revision INTEGER NOT NULL CHECK(routing_revision>0),
                        phase TEXT NOT NULL CHECK(phase IN (
                            'prepare','imported','committed','active','recovery_required'
                        )),
                        prepared_at TEXT NOT NULL,
                        imported_at TEXT,
                        committed_at TEXT,
                        activated_at TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_authority_routes_node
                        ON authority_routes(authority_node_id,state,routing_revision);
                    CREATE INDEX IF NOT EXISTS ix_fleet_members_state
                        ON fleet_members(state,node_id);
                    """
                )
                schema = await (
                    await db.execute("SELECT version FROM control_schema WHERE singleton=1")
                ).fetchone()
                if schema is None:
                    await db.execute(
                        "INSERT INTO control_schema(singleton,version) VALUES(1,?)",
                        (self.SCHEMA_VERSION,),
                    )
                elif int(schema[0]) != self.SCHEMA_VERSION:
                    raise FleetControlError("unsupported fleet_control schema version")

                meta = await (
                    await db.execute(
                        "SELECT fleet_id,node_id,control_node_id "
                        "FROM control_meta WHERE singleton=1"
                    )
                ).fetchone()
                if meta is None:
                    await db.execute(
                        "INSERT INTO control_meta("
                        "singleton,fleet_id,node_id,control_node_id,"
                        "routing_revision,updated_at) VALUES(1,?,?,?,?,?)",
                        (
                            self.fleet_id,
                            self.node_id,
                            self.control_node_id,
                            0,
                            stamp,
                        ),
                    )
                elif tuple(meta) != (
                    self.fleet_id,
                    self.node_id,
                    self.control_node_id,
                ):
                    raise FleetControlError(
                        "fleet_control identity does not match persisted metadata"
                    )
                await db.execute(
                    "INSERT INTO fleet_members(node_id,capabilities_json,state,updated_at) "
                    "VALUES(?, '[]','active',?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "state='active',updated_at=excluded.updated_at",
                    (self.node_id, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def register_member(
        self,
        node_id: str,
        capabilities,
        *,
        state: str = "active",
        now: str | None = None,
    ) -> dict:
        node_id = validate_protocol_id(node_id, "node_id")
        caps = validate_capabilities(capabilities)
        if state not in {"active", "draining", "offline"}:
            raise ValueError("invalid fleet member state")
        stamp = now or utc_text()
        encoded = json.dumps(caps, ensure_ascii=True, separators=(",", ":"))
        async with self._connect("fleet_control_member_upsert") as db:
            await db.execute(
                "INSERT INTO fleet_members(node_id,capabilities_json,state,updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "capabilities_json=excluded.capabilities_json,"
                "state=excluded.state,updated_at=excluded.updated_at",
                (node_id, encoded, state, stamp),
            )
            await db.commit()
        return {
            "node_id": node_id,
            "capabilities": list(caps),
            "state": state,
            "updated_at": stamp,
        }

    async def members(self) -> list[dict]:
        async with self._connect("fleet_control_members") as db:
            rows = await (
                await db.execute(
                    "SELECT node_id,capabilities_json,state,updated_at "
                    "FROM fleet_members ORDER BY node_id"
                )
            ).fetchall()
        return [
            {
                "node_id": row[0],
                "capabilities": json.loads(row[1]),
                "state": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]

    async def _next_revision(self, db, stamp: str) -> int:
        row = await (
            await db.execute("SELECT routing_revision FROM control_meta WHERE singleton=1")
        ).fetchone()
        if row is None:
            raise FleetControlError("fleet_control is not initialized")
        revision = int(row[0]) + 1
        await db.execute(
            "UPDATE control_meta SET routing_revision=?,updated_at=? WHERE singleton=1",
            (revision, stamp),
        )
        return revision

    async def route(self, logical_agent_id: str) -> dict | None:
        async with self._connect("fleet_control_route_get") as db:
            row = await (
                await db.execute(
                    "SELECT authority_node_id,authority_epoch,routing_revision,"
                    "state,target_node_id,updated_at "
                    "FROM authority_routes WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "logical_agent_id": logical_agent_id,
            "authority_node_id": row[0],
            "authority_epoch": int(row[1]),
            "routing_revision": int(row[2]),
            "state": row[3],
            "target_node_id": row[4],
            "updated_at": row[5],
        }

    async def publish_route(
        self,
        logical_agent_id: str,
        authority_node_id: str,
        authority_epoch: int,
        *,
        state: str = "active",
        target_node_id: str | None = None,
        now: str | None = None,
    ) -> dict:
        authority_node_id = validate_protocol_id(authority_node_id, "authority_node_id")
        if target_node_id is not None:
            target_node_id = validate_protocol_id(target_node_id, "target_node_id")
        authority_epoch = int(authority_epoch)
        if authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        if state not in {
            "active",
            "prepare",
            "imported",
            "committed",
            "recovery_required",
        }:
            raise ValueError("invalid authority route state")
        stamp = now or utc_text()
        async with self._connect("fleet_control_route_publish") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (
                    await db.execute(
                        "SELECT authority_node_id,authority_epoch,routing_revision,"
                        "state,target_node_id,updated_at "
                        "FROM authority_routes WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                desired = (
                    authority_node_id,
                    authority_epoch,
                    state,
                    target_node_id,
                )
                if existing is not None:
                    current = (
                        existing[0],
                        int(existing[1]),
                        existing[3],
                        existing[4],
                    )
                    if authority_epoch < int(existing[1]):
                        raise FleetControlError("stale_authority_epoch")
                    if current == desired:
                        await db.commit()
                        return {
                            "logical_agent_id": logical_agent_id,
                            "authority_node_id": existing[0],
                            "authority_epoch": int(existing[1]),
                            "routing_revision": int(existing[2]),
                            "state": existing[3],
                            "target_node_id": existing[4],
                            "updated_at": existing[5],
                        }
                revision = await self._next_revision(db, stamp)
                await db.execute(
                    "INSERT INTO authority_routes("
                    "logical_agent_id,authority_node_id,authority_epoch,"
                    "routing_revision,state,target_node_id,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(logical_agent_id) DO UPDATE SET "
                    "authority_node_id=excluded.authority_node_id,"
                    "authority_epoch=excluded.authority_epoch,"
                    "routing_revision=excluded.routing_revision,"
                    "state=excluded.state,target_node_id=excluded.target_node_id,"
                    "updated_at=excluded.updated_at",
                    (
                        logical_agent_id,
                        authority_node_id,
                        authority_epoch,
                        revision,
                        state,
                        target_node_id,
                        stamp,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.route(logical_agent_id)

    async def prepare_transfer(
        self,
        logical_agent_id: str,
        from_node_id: str,
        to_node_id: str,
        authority_epoch: int,
        *,
        now: str | None = None,
    ) -> dict:
        from_node_id = validate_protocol_id(from_node_id, "from_node_id")
        to_node_id = validate_protocol_id(to_node_id, "to_node_id")
        if from_node_id == to_node_id:
            raise ValueError("transfer target must differ from source")
        authority_epoch = int(authority_epoch)
        if authority_epoch < 1:
            raise ValueError("authority_epoch must be positive")
        route = await self.route(logical_agent_id)
        if (
            route is None
            or route["authority_node_id"] != from_node_id
            or route["authority_epoch"] != authority_epoch
            or route["state"] != "active"
        ):
            raise FleetControlError("transfer_source_mismatch")
        stamp = now or utc_text()
        async with self._connect("fleet_control_transfer_prepare") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                revision = await self._next_revision(db, stamp)
                await db.execute(
                    "INSERT INTO authority_transfers("
                    "logical_agent_id,from_node_id,to_node_id,"
                    "from_authority_epoch,target_authority_epoch,"
                    "routing_revision,phase,prepared_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'prepare',?,?) "
                    "ON CONFLICT(logical_agent_id) DO UPDATE SET "
                    "from_node_id=excluded.from_node_id,"
                    "to_node_id=excluded.to_node_id,"
                    "from_authority_epoch=excluded.from_authority_epoch,"
                    "target_authority_epoch=excluded.target_authority_epoch,"
                    "routing_revision=excluded.routing_revision,phase='prepare',"
                    "prepared_at=excluded.prepared_at,imported_at=NULL,"
                    "committed_at=NULL,activated_at=NULL,"
                    "updated_at=excluded.updated_at",
                    (
                        logical_agent_id,
                        from_node_id,
                        to_node_id,
                        authority_epoch,
                        authority_epoch + 1,
                        revision,
                        stamp,
                        stamp,
                    ),
                )
                await db.execute(
                    "UPDATE authority_routes SET routing_revision=?,state='prepare',"
                    "target_node_id=?,updated_at=? WHERE logical_agent_id=?",
                    (revision, to_node_id, stamp, logical_agent_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.transfer(logical_agent_id)

    async def transfer(self, logical_agent_id: str) -> dict | None:
        async with self._connect("fleet_control_transfer_get") as db:
            row = await (
                await db.execute(
                    "SELECT from_node_id,to_node_id,from_authority_epoch,"
                    "target_authority_epoch,routing_revision,phase,"
                    "prepared_at,imported_at,committed_at,activated_at,updated_at "
                    "FROM authority_transfers WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
        if row is None:
            return None
        keys = (
            "from_node_id",
            "to_node_id",
            "from_authority_epoch",
            "target_authority_epoch",
            "routing_revision",
            "phase",
            "prepared_at",
            "imported_at",
            "committed_at",
            "activated_at",
            "updated_at",
        )
        result = dict(zip(keys, row, strict=True))
        result["logical_agent_id"] = logical_agent_id
        result["from_authority_epoch"] = int(result["from_authority_epoch"])
        result["target_authority_epoch"] = int(result["target_authority_epoch"])
        result["routing_revision"] = int(result["routing_revision"])
        return result

    async def advance_transfer(
        self,
        logical_agent_id: str,
        phase: str,
        *,
        now: str | None = None,
    ) -> dict:
        if phase not in {"imported", "committed", "active", "recovery_required"}:
            raise ValueError("invalid transfer phase")
        allowed = {
            "prepare": {"imported", "recovery_required"},
            "imported": {"committed", "recovery_required"},
            "committed": {"active", "recovery_required"},
            "active": set(),
            "recovery_required": set(),
        }
        stamp = now or utc_text()
        async with self._connect("fleet_control_transfer_advance") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT from_node_id,to_node_id,from_authority_epoch,"
                        "target_authority_epoch,phase "
                        "FROM authority_transfers WHERE logical_agent_id=?",
                        (logical_agent_id,),
                    )
                ).fetchone()
                if row is None:
                    raise FleetControlError("transfer_not_found")
                current_phase = row[4]
                if phase not in allowed[current_phase]:
                    raise FleetControlError("invalid_transfer_transition")
                revision = await self._next_revision(db, stamp)
                column = {
                    "imported": "imported_at",
                    "committed": "committed_at",
                    "active": "activated_at",
                    "recovery_required": "updated_at",
                }[phase]
                await db.execute(
                    f"UPDATE authority_transfers SET phase=?,routing_revision=?,"
                    f"{column}=?,updated_at=? WHERE logical_agent_id=?",
                    (phase, revision, stamp, stamp, logical_agent_id),
                )
                if phase == "committed":
                    await db.execute(
                        "UPDATE authority_routes SET authority_node_id=?,"
                        "authority_epoch=?,routing_revision=?,state='committed',"
                        "target_node_id=?,updated_at=? WHERE logical_agent_id=?",
                        (row[1], int(row[3]), revision, row[1], stamp, logical_agent_id),
                    )
                elif phase == "active":
                    await db.execute(
                        "UPDATE authority_routes SET routing_revision=?,state='active',"
                        "target_node_id=NULL,updated_at=? WHERE logical_agent_id=?",
                        (revision, stamp, logical_agent_id),
                    )
                elif phase == "recovery_required":
                    await db.execute(
                        "UPDATE authority_routes SET routing_revision=?,"
                        "state='recovery_required',target_node_id=?,updated_at=? "
                        "WHERE logical_agent_id=?",
                        (revision, row[1], stamp, logical_agent_id),
                    )
                else:
                    await db.execute(
                        "UPDATE authority_routes SET routing_revision=?,state=?,"
                        "target_node_id=?,updated_at=? WHERE logical_agent_id=?",
                        (revision, phase, row[1], stamp, logical_agent_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.transfer(logical_agent_id)

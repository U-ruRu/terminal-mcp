from __future__ import annotations

import json
from contextlib import asynccontextmanager

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.protocol import MAX_RECENT_TERMINAL_COMMANDS, validate_protocol_id
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection


class FleetProjectionError(RuntimeError):
    pass


class FleetProjectionStore:
    """Rebuildable derived Fleet read model. Never owns runtime/domain truth."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path,
        *,
        fleet_id: str,
        node_id: str,
        owner_node_id: str,
        role: str = "owner",
        max_event_rows: int = 10_000,
    ):
        if role not in {"owner", "follower"}:
            raise ValueError("projection role must be owner or follower")
        self.path = path
        self.fleet_id = validate_protocol_id(fleet_id, "fleet_id")
        self.node_id = validate_protocol_id(node_id, "node_id")
        self.owner_node_id = validate_protocol_id(owner_node_id, "owner_node_id")
        if int(max_event_rows) < 1:
            raise ValueError("max_event_rows must be positive")
        self.role = role
        self.max_event_rows = int(max_event_rows)
        self.sqlite_diagnostics = SqliteDiagnostics("fleet_projection")

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
                "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=1000",
                "PRAGMA foreign_keys=ON",
            ),
        ) as db:
            yield db

    async def initialize(self) -> None:
        stamp = utc_text()
        async with self._connect("fleet_projection_initialize") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS projection_schema(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS projection_meta(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        fleet_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        owner_node_id TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('owner','follower')),
                        projection_epoch INTEGER NOT NULL CHECK(projection_epoch>0),
                        projection_seq INTEGER NOT NULL DEFAULT 0 CHECK(projection_seq>=0),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS projection_sources(
                        source_node_id TEXT PRIMARY KEY,
                        source_stream_generation TEXT NOT NULL,
                        source_seq INTEGER NOT NULL DEFAULT 0 CHECK(source_seq>=0),
                        freshness TEXT NOT NULL DEFAULT 'fresh'
                            CHECK(freshness IN ('fresh','stale','reset_required','unavailable')),
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS projection_entities(
                        source_node_id TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        entity_revision INTEGER NOT NULL CHECK(entity_revision>0),
                        payload_version INTEGER NOT NULL DEFAULT 1,
                        payload_json TEXT NOT NULL,
                        authority_node_id TEXT,
                        authority_epoch INTEGER,
                        source_stream_generation TEXT NOT NULL,
                        source_seq INTEGER NOT NULL CHECK(source_seq>=0),
                        projection_seq INTEGER NOT NULL CHECK(projection_seq>=0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(source_node_id,entity_type,entity_id)
                    );
                    CREATE TABLE IF NOT EXISTS projection_events(
                        projection_epoch INTEGER NOT NULL CHECK(projection_epoch>0),
                        projection_seq INTEGER PRIMARY KEY CHECK(projection_seq>0),
                        event_id TEXT NOT NULL UNIQUE,
                        source_node_id TEXT NOT NULL,
                        source_stream_generation TEXT NOT NULL,
                        source_seq INTEGER NOT NULL CHECK(source_seq>0),
                        event_type TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        entity_revision INTEGER NOT NULL CHECK(entity_revision>0),
                        payload_version INTEGER NOT NULL DEFAULT 1,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS projection_runtime_overlay(
                        source_node_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        freshness TEXT NOT NULL DEFAULT 'fresh'
                            CHECK(freshness IN ('fresh','stale','unavailable'))
                    );
                    CREATE TABLE IF NOT EXISTS projection_recovery_scopes(
                        source_node_id TEXT NOT NULL,
                        recovery_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        source_stream_generation TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        barrier_source_seq INTEGER NOT NULL CHECK(barrier_source_seq>=0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(source_node_id,scope)
                    );
                    CREATE TABLE IF NOT EXISTS projection_recovery_entities(
                        source_node_id TEXT NOT NULL,
                        recovery_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        entity_revision INTEGER NOT NULL CHECK(entity_revision>0),
                        payload_version INTEGER NOT NULL DEFAULT 1,
                        payload_json TEXT NOT NULL,
                        authority_node_id TEXT,
                        authority_epoch INTEGER,
                        PRIMARY KEY(source_node_id,recovery_id,scope,entity_type,entity_id)
                    );
                    CREATE TABLE IF NOT EXISTS projection_scope_status(
                        source_node_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN ('LIVE','CATCHING_UP','DEGRADED','OFFLINE_AUTH')),
                        reason TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(source_node_id,scope)
                    );
                    CREATE INDEX IF NOT EXISTS ix_projection_events_epoch_seq
                        ON projection_events(projection_epoch,projection_seq);
                    CREATE INDEX IF NOT EXISTS ix_projection_entities_type
                        ON projection_entities(entity_type,entity_id);
                    """
                )
                schema = await (
                    await db.execute("SELECT version FROM projection_schema WHERE singleton=1")
                ).fetchone()
                if schema is None:
                    await db.execute(
                        "INSERT INTO projection_schema(singleton,version) VALUES(1,?)",
                        (self.SCHEMA_VERSION,),
                    )
                elif int(schema[0]) != self.SCHEMA_VERSION:
                    raise FleetProjectionError("unsupported fleet_projection schema version")

                meta = await (
                    await db.execute(
                        "SELECT fleet_id,node_id,owner_node_id,role "
                        "FROM projection_meta WHERE singleton=1"
                    )
                ).fetchone()
                expected = (self.fleet_id, self.node_id, self.owner_node_id, self.role)
                if meta is None:
                    await db.execute(
                        "INSERT INTO projection_meta("
                        "singleton,fleet_id,node_id,owner_node_id,role,"
                        "projection_epoch,projection_seq,updated_at) "
                        "VALUES(1,?,?,?,?,1,0,?)",
                        (*expected, stamp),
                    )
                elif tuple(meta) != expected:
                    raise FleetProjectionError("fleet_projection identity does not match metadata")
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def meta(self) -> dict:
        async with self._connect("fleet_projection_meta") as db:
            row = await (
                await db.execute(
                    "SELECT fleet_id,node_id,owner_node_id,role,projection_epoch,"
                    "projection_seq,updated_at FROM projection_meta WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            raise FleetProjectionError("fleet_projection is not initialized")
        return {
            "fleet_id": row[0],
            "node_id": row[1],
            "owner_node_id": row[2],
            "role": row[3],
            "projection_epoch": int(row[4]),
            "projection_seq": int(row[5]),
            "updated_at": row[6],
        }

    async def _prune_events(self, db) -> None:
        await db.execute(
            "DELETE FROM projection_events WHERE projection_seq IN ("
            "SELECT projection_seq FROM projection_events "
            "ORDER BY projection_seq DESC LIMIT -1 OFFSET ?)",
            (self.max_event_rows,),
        )

    async def _next_seq(self, db, stamp: str) -> tuple[int, int]:
        row = await (
            await db.execute(
                "SELECT projection_epoch,projection_seq FROM projection_meta WHERE singleton=1"
            )
        ).fetchone()
        if row is None:
            raise FleetProjectionError("fleet_projection is not initialized")
        seq = int(row[1]) + 1
        await db.execute(
            "UPDATE projection_meta SET projection_seq=?,updated_at=? WHERE singleton=1",
            (seq, stamp),
        )
        return int(row[0]), seq

    async def _apply_projection_mutation(
        self,
        db,
        *,
        event_id: str,
        source_node_id: str,
        generation: str,
        source_seq: int,
        event_type: str,
        entity_type: str,
        entity_id: str,
        entity_revision: int,
        payload_version: int,
        payload: dict,
        authority_node_id,
        authority_epoch,
        created_at: str,
        stamp: str,
    ) -> bool:
        duplicate = await (
            await db.execute("SELECT 1 FROM projection_events WHERE event_id=?", (event_id,))
        ).fetchone()
        if duplicate is not None:
            return False
        epoch, projection_seq = await self._next_seq(db, stamp)
        journal_seq = max(1, int(source_seq))
        await db.execute(
            "INSERT INTO projection_events("
            "projection_epoch,projection_seq,event_id,source_node_id,"
            "source_stream_generation,source_seq,event_type,entity_type,entity_id,"
            "entity_revision,payload_version,payload_json,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                epoch,
                projection_seq,
                event_id,
                source_node_id,
                generation,
                journal_seq,
                event_type,
                entity_type,
                entity_id,
                max(1, int(entity_revision)),
                max(1, int(payload_version)),
                json.dumps(payload or {}, separators=(",", ":")),
                created_at,
            ),
        )
        if event_type == "projection.remove":
            await db.execute(
                "DELETE FROM projection_entities "
                "WHERE source_node_id=? AND entity_type=? AND entity_id=?",
                (source_node_id, entity_type, entity_id),
            )
            return True
        await db.execute(
            "INSERT INTO projection_entities("
            "source_node_id,entity_type,entity_id,entity_revision,payload_version,"
            "payload_json,authority_node_id,authority_epoch,"
            "source_stream_generation,source_seq,projection_seq,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_node_id,entity_type,entity_id) DO UPDATE SET "
            "entity_revision=excluded.entity_revision,"
            "payload_version=excluded.payload_version,payload_json=excluded.payload_json,"
            "authority_node_id=excluded.authority_node_id,"
            "authority_epoch=excluded.authority_epoch,"
            "source_stream_generation=excluded.source_stream_generation,"
            "source_seq=excluded.source_seq,projection_seq=excluded.projection_seq,"
            "updated_at=excluded.updated_at",
            (
                source_node_id,
                entity_type,
                entity_id,
                max(1, int(entity_revision)),
                max(1, int(payload_version)),
                json.dumps(payload or {}, separators=(",", ":")),
                authority_node_id,
                authority_epoch,
                generation,
                int(source_seq),
                projection_seq,
                stamp,
            ),
        )
        return True

    async def apply_snapshot(self, snapshot: dict) -> dict:
        if self.role != "owner":
            raise FleetProjectionError("follower cannot independently consume sources")
        if snapshot.get("fleet_id") != self.fleet_id:
            raise FleetProjectionError("snapshot fleet_id mismatch")
        source_node_id = validate_protocol_id(snapshot["node_id"], "source node_id")
        generation = validate_protocol_id(
            snapshot["source_stream_generation"], "source_stream_generation"
        )
        barrier = int(snapshot["barrier_source_seq"])
        complete = {str(item) for item in snapshot.get("complete_entity_types") or []}
        entities = list(snapshot.get("entities") or [])
        stamp = utc_text()
        async with self._connect("fleet_projection_apply_snapshot") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for entity_type in complete:
                    await db.execute(
                        "DELETE FROM projection_entities "
                        "WHERE source_node_id=? AND entity_type=?",
                        (source_node_id, entity_type),
                    )
                for item in entities:
                    entity_type = str(item["entity_type"])
                    if complete and entity_type not in complete:
                        continue
                    epoch, projection_seq = await self._next_seq(db, stamp)
                    del epoch
                    await db.execute(
                        "INSERT INTO projection_entities("
                        "source_node_id,entity_type,entity_id,entity_revision,payload_version,"
                        "payload_json,authority_node_id,authority_epoch,"
                        "source_stream_generation,source_seq,projection_seq,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source_node_id,entity_type,entity_id) DO UPDATE SET "
                        "entity_revision=excluded.entity_revision,"
                        "payload_version=excluded.payload_version,payload_json=excluded.payload_json,"
                        "authority_node_id=excluded.authority_node_id,"
                        "authority_epoch=excluded.authority_epoch,"
                        "source_stream_generation=excluded.source_stream_generation,"
                        "source_seq=excluded.source_seq,projection_seq=excluded.projection_seq,"
                        "updated_at=excluded.updated_at",
                        (
                            source_node_id,
                            entity_type,
                            str(item["entity_id"]),
                            int(item["entity_revision"]),
                            int(item.get("payload_version") or 1),
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item.get("authority_node_id"),
                            item.get("authority_epoch"),
                            generation,
                            barrier,
                            projection_seq,
                            stamp,
                        ),
                    )
                await db.execute(
                    "INSERT INTO projection_sources("
                    "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                    ") VALUES(?,?,?,'fresh',?) "
                    "ON CONFLICT(source_node_id) DO UPDATE SET "
                    "source_stream_generation=excluded.source_stream_generation,"
                    "source_seq=excluded.source_seq,freshness='fresh',updated_at=excluded.updated_at",
                    (source_node_id, generation, barrier, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.meta()

    async def apply_source_page(self, page: dict) -> dict:
        if self.role != "owner":
            raise FleetProjectionError("follower cannot independently consume sources")
        if page.get("fleet_id") != self.fleet_id:
            raise FleetProjectionError("source fleet_id mismatch")
        source_node_id = validate_protocol_id(page["node_id"], "source node_id")
        generation = validate_protocol_id(
            page["source_stream_generation"], "source_stream_generation"
        )
        if page.get("reset_required"):
            await self.mark_source(source_node_id, generation, "reset_required")
            raise FleetProjectionError("source reset required")
        events = sorted(page.get("events") or [], key=lambda item: int(item["source_seq"]))
        stamp = utc_text()
        async with self._connect("fleet_projection_apply_page") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await (
                    await db.execute(
                        "SELECT source_stream_generation,source_seq FROM projection_sources "
                        "WHERE source_node_id=?",
                        (source_node_id,),
                    )
                ).fetchone()
                if cursor is not None and cursor[0] != generation:
                    raise FleetProjectionError("source generation changed; snapshot required")
                last_seq = int(cursor[1]) if cursor else 0
                for item in events:
                    source_seq = int(item["source_seq"])
                    if source_seq <= last_seq:
                        continue
                    event_id = str(item["event_id"])
                    duplicate = await (
                        await db.execute(
                            "SELECT 1 FROM projection_events WHERE event_id=?", (event_id,)
                        )
                    ).fetchone()
                    if duplicate is not None:
                        last_seq = max(last_seq, source_seq)
                        continue
                    epoch, projection_seq = await self._next_seq(db, stamp)
                    payload = item.get("payload") or {}
                    await db.execute(
                        "INSERT INTO projection_events("
                        "projection_epoch,projection_seq,event_id,source_node_id,"
                        "source_stream_generation,source_seq,event_type,entity_type,entity_id,"
                        "entity_revision,payload_version,payload_json,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            epoch,
                            projection_seq,
                            event_id,
                            source_node_id,
                            generation,
                            source_seq,
                            str(item["event_type"]),
                            str(item["entity_type"]),
                            str(item["entity_id"]),
                            int(item["entity_revision"]),
                            int(item.get("payload_version") or 1),
                            json.dumps(payload, separators=(",", ":")),
                            str(item["created_at"]),
                        ),
                    )
                    current = await (
                        await db.execute(
                            "SELECT entity_revision FROM projection_entities "
                            "WHERE source_node_id=? AND entity_type=? AND entity_id=?",
                            (source_node_id, str(item["entity_type"]), str(item["entity_id"])),
                        )
                    ).fetchone()
                    if current is None or int(item["entity_revision"]) >= int(current[0]):
                        await db.execute(
                            "INSERT INTO projection_entities("
                            "source_node_id,entity_type,entity_id,entity_revision,payload_version,"
                            "payload_json,authority_node_id,authority_epoch,"
                            "source_stream_generation,source_seq,projection_seq,updated_at"
                            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(source_node_id,entity_type,entity_id) DO UPDATE SET "
                            "entity_revision=excluded.entity_revision,"
                            "payload_version=excluded.payload_version,"
                            "payload_json=excluded.payload_json,"
                            "authority_node_id=excluded.authority_node_id,"
                            "authority_epoch=excluded.authority_epoch,"
                            "source_stream_generation=excluded.source_stream_generation,"
                            "source_seq=excluded.source_seq,projection_seq=excluded.projection_seq,"
                            "updated_at=excluded.updated_at",
                            (
                                source_node_id,
                                str(item["entity_type"]),
                                str(item["entity_id"]),
                                int(item["entity_revision"]),
                                int(item.get("payload_version") or 1),
                                json.dumps(payload, separators=(",", ":")),
                                item.get("authority_node_id"),
                                item.get("authority_epoch"),
                                generation,
                                source_seq,
                                projection_seq,
                                stamp,
                            ),
                        )
                    last_seq = source_seq
                page_cursor = int(page.get("next_cursor") or last_seq)
                last_seq = max(last_seq, page_cursor)
                await db.execute(
                    "INSERT INTO projection_sources("
                    "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                    ") VALUES(?,?,?,'fresh',?) "
                    "ON CONFLICT(source_node_id) DO UPDATE SET "
                    "source_stream_generation=excluded.source_stream_generation,"
                    "source_seq=MAX(projection_sources.source_seq,excluded.source_seq),"
                    "freshness='fresh',updated_at=excluded.updated_at",
                    (source_node_id, generation, last_seq, stamp),
                )
                await self._prune_events(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.meta()

    async def begin_current_recovery(
        self,
        source_node_id: str,
        generation: str,
        recovery_id: str,
        scopes: list[str],
    ) -> None:
        if self.role != "owner":
            raise FleetProjectionError("follower cannot independently consume sources")
        stamp = utc_text()
        async with self._connect("fleet_projection_recovery_begin") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "DELETE FROM projection_recovery_entities WHERE source_node_id=?",
                    (source_node_id,),
                )
                await db.execute(
                    "DELETE FROM projection_recovery_scopes WHERE source_node_id=?",
                    (source_node_id,),
                )
                await db.execute(
                    "INSERT INTO projection_sources("
                    "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                    ") VALUES(?,?,0,'reset_required',?) "
                    "ON CONFLICT(source_node_id) DO UPDATE SET "
                    "source_stream_generation=excluded.source_stream_generation,"
                    "source_seq=CASE WHEN projection_sources.source_stream_generation="
                    "excluded.source_stream_generation THEN projection_sources.source_seq "
                    "ELSE 0 END,"
                    "freshness='reset_required',updated_at=excluded.updated_at",
                    (source_node_id, generation, stamp),
                )
                for scope in scopes:
                    await db.execute(
                        "INSERT INTO projection_scope_status("
                        "source_node_id,scope,status,reason,updated_at) "
                        "VALUES(?,?,'CATCHING_UP',?,?) "
                        "ON CONFLICT(source_node_id,scope) DO UPDATE SET "
                        "status='CATCHING_UP',reason=excluded.reason,updated_at=excluded.updated_at",
                        (source_node_id, scope, f"recovery:{recovery_id}", stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def stage_current_recovery_page(
        self,
        source_node_id: str,
        recovery_id: str,
        page: dict,
    ) -> dict:
        if self.role != "owner":
            raise FleetProjectionError("follower cannot independently consume sources")
        if page.get("fleet_id") != self.fleet_id:
            raise FleetProjectionError("recovery fleet_id mismatch")
        if str(page.get("node_id") or "") != source_node_id:
            raise FleetProjectionError("recovery source identity mismatch")
        generation = validate_protocol_id(
            page["source_stream_generation"], "source_stream_generation"
        )
        scope = str(page["scope"])
        snapshot_id = str(page["snapshot_id"])
        barrier = int(page["barrier_source_seq"])
        entities = list(page.get("entities") or [])
        entity_types = {str(item["entity_type"]) for item in entities}
        if len(entity_types) > 1:
            raise FleetProjectionError("recovery page mixes entity types")
        entity_type = next(iter(entity_types), str(page.get("entity_type") or scope.rstrip("s")))
        stamp = utc_text()
        mutations = 0
        async with self._connect("fleet_projection_recovery_page") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await (
                    await db.execute(
                        "SELECT recovery_id,source_stream_generation,snapshot_id,"
                        "barrier_source_seq,entity_type FROM projection_recovery_scopes "
                        "WHERE source_node_id=? AND scope=?",
                        (source_node_id, scope),
                    )
                ).fetchone()
                if existing is None:
                    await db.execute(
                        "INSERT INTO projection_recovery_scopes("
                        "source_node_id,recovery_id,scope,entity_type,source_stream_generation,"
                        "snapshot_id,barrier_source_seq,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            source_node_id,
                            recovery_id,
                            scope,
                            entity_type,
                            generation,
                            snapshot_id,
                            barrier,
                            stamp,
                        ),
                    )
                elif tuple(existing[:4]) != (
                    recovery_id,
                    generation,
                    snapshot_id,
                    barrier,
                ):
                    raise FleetProjectionError("recovery page identity changed")
                for item in entities:
                    await db.execute(
                        "INSERT INTO projection_recovery_entities("
                        "source_node_id,recovery_id,scope,entity_type,entity_id,entity_revision,"
                        "payload_version,payload_json,authority_node_id,authority_epoch"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source_node_id,recovery_id,scope,entity_type,entity_id) "
                        "DO UPDATE SET entity_revision=excluded.entity_revision,"
                        "payload_version=excluded.payload_version,payload_json=excluded.payload_json,"
                        "authority_node_id=excluded.authority_node_id,"
                        "authority_epoch=excluded.authority_epoch",
                        (
                            source_node_id,
                            recovery_id,
                            scope,
                            str(item["entity_type"]),
                            str(item["entity_id"]),
                            int(item["entity_revision"]),
                            int(item.get("payload_version") or 1),
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item.get("authority_node_id"),
                            item.get("authority_epoch"),
                        ),
                    )
                if page.get("page_complete"):
                    upserts = await db.execute(
                        "SELECT r.entity_type,r.entity_id,r.entity_revision,r.payload_version,"
                        "r.payload_json,r.authority_node_id,r.authority_epoch "
                        "FROM projection_recovery_entities r "
                        "LEFT JOIN projection_entities e ON "
                        "e.source_node_id=r.source_node_id AND e.entity_type=r.entity_type "
                        "AND e.entity_id=r.entity_id "
                        "WHERE r.source_node_id=? AND r.recovery_id=? AND r.scope=? AND "
                        "(e.entity_id IS NULL OR e.entity_revision!=r.entity_revision OR "
                        "e.payload_version!=r.payload_version OR e.payload_json!=r.payload_json OR "
                        "COALESCE(e.authority_node_id,'')!=COALESCE(r.authority_node_id,'') OR "
                        "COALESCE(e.authority_epoch,0)!=COALESCE(r.authority_epoch,0)) "
                        "ORDER BY r.entity_id",
                        (source_node_id, recovery_id, scope),
                    )
                    while True:
                        rows = await upserts.fetchmany(250)
                        if not rows:
                            break
                        for row in rows:
                            event_id = (
                                f"recovery:{recovery_id}:{scope}:upsert:{row[1]}"
                            )
                            changed = await self._apply_projection_mutation(
                                db,
                                event_id=event_id,
                                source_node_id=source_node_id,
                                generation=generation,
                                source_seq=barrier,
                                event_type="projection.upsert",
                                entity_type=row[0],
                                entity_id=row[1],
                                entity_revision=int(row[2]),
                                payload_version=int(row[3]),
                                payload=json.loads(row[4]),
                                authority_node_id=row[5],
                                authority_epoch=row[6],
                                created_at=stamp,
                                stamp=stamp,
                            )
                            mutations += int(changed)
                    removals = await db.execute(
                        "SELECT e.entity_type,e.entity_id,e.entity_revision "
                        "FROM projection_entities e "
                        "LEFT JOIN projection_recovery_entities r ON "
                        "r.source_node_id=e.source_node_id AND r.recovery_id=? AND "
                        "r.scope=? AND r.entity_type=e.entity_type AND r.entity_id=e.entity_id "
                        "WHERE e.source_node_id=? AND e.entity_type=? AND r.entity_id IS NULL "
                        "ORDER BY e.entity_id",
                        (recovery_id, scope, source_node_id, entity_type),
                    )
                    while True:
                        rows = await removals.fetchmany(250)
                        if not rows:
                            break
                        for row in rows:
                            event_id = (
                                f"recovery:{recovery_id}:{scope}:remove:{row[1]}"
                            )
                            changed = await self._apply_projection_mutation(
                                db,
                                event_id=event_id,
                                source_node_id=source_node_id,
                                generation=generation,
                                source_seq=barrier,
                                event_type="projection.remove",
                                entity_type=row[0],
                                entity_id=row[1],
                                entity_revision=max(int(row[2]) + 1, barrier, 1),
                                payload_version=2,
                                payload={},
                                authority_node_id=None,
                                authority_epoch=None,
                                created_at=stamp,
                                stamp=stamp,
                            )
                            mutations += int(changed)
                    await db.execute(
                        "DELETE FROM projection_recovery_entities "
                        "WHERE source_node_id=? AND recovery_id=? AND scope=?",
                        (source_node_id, recovery_id, scope),
                    )
                    await db.execute(
                        "DELETE FROM projection_recovery_scopes "
                        "WHERE source_node_id=? AND scope=?",
                        (source_node_id, scope),
                    )
                await self._prune_events(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {"mutations": mutations, "scope": scope, "complete": bool(page.get("page_complete"))}

    async def complete_current_recovery(
        self,
        source_node_id: str,
        generation: str,
        barrier_source_seq: int,
    ) -> None:
        stamp = utc_text()
        async with self._connect("fleet_projection_recovery_complete") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE projection_sources SET source_stream_generation=?,source_seq=?,"
                    "freshness='stale',updated_at=? WHERE source_node_id=?",
                    (generation, int(barrier_source_seq), stamp, source_node_id),
                )
                await db.execute(
                    "DELETE FROM projection_recovery_entities WHERE source_node_id=?",
                    (source_node_id,),
                )
                await db.execute(
                    "DELETE FROM projection_recovery_scopes WHERE source_node_id=?",
                    (source_node_id,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _prune_recent_terminal_commands(
        self,
        db,
        *,
        source_node_id: str,
        generation: str,
        source_seq: int,
        stamp: str,
    ) -> int:
        rows = await (
            await db.execute(
                "SELECT entity_id,entity_revision,source_seq "
                "FROM projection_entities "
                "WHERE source_node_id=? AND entity_type='command' "
                "AND json_extract(payload_json,'$.status') "
                "IN ('completed','failed','cancelled') "
                "ORDER BY source_seq DESC,entity_id DESC LIMIT -1 OFFSET ?",
                (source_node_id, MAX_RECENT_TERMINAL_COMMANDS),
            )
        ).fetchall()
        removed = 0
        for entity_id, revision, prior_source_seq in rows:
            event_id = (
                f"retention:{source_node_id}:{generation}:{entity_id}:"
                f"{prior_source_seq}:{source_seq}"
            )
            changed = await self._apply_projection_mutation(
                db,
                event_id=event_id,
                source_node_id=source_node_id,
                generation=generation,
                source_seq=max(1, int(source_seq)),
                event_type="projection.remove",
                entity_type="command",
                entity_id=str(entity_id),
                entity_revision=max(int(revision) + 1, int(source_seq), 1),
                payload_version=2,
                payload={},
                authority_node_id=None,
                authority_epoch=None,
                created_at=stamp,
                stamp=stamp,
            )
            removed += int(changed)
        return removed

    async def apply_materialized_source_page(
        self,
        page: dict,
        mutations: list[dict],
    ) -> dict:
        if self.role != "owner":
            raise FleetProjectionError("follower cannot independently consume sources")
        if page.get("fleet_id") != self.fleet_id:
            raise FleetProjectionError("source fleet_id mismatch")
        source_node_id = validate_protocol_id(page["node_id"], "source node_id")
        generation = validate_protocol_id(
            page["source_stream_generation"], "source_stream_generation"
        )
        if page.get("reset_required"):
            await self.mark_source(source_node_id, generation, "reset_required")
            raise FleetProjectionError("source reset required")
        stamp = utc_text()
        async with self._connect("fleet_projection_apply_materialized_page") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await (
                    await db.execute(
                        "SELECT source_stream_generation,source_seq FROM projection_sources "
                        "WHERE source_node_id=?",
                        (source_node_id,),
                    )
                ).fetchone()
                if cursor is None:
                    raise FleetProjectionError(
                        "source recovery required before delta materialization"
                    )
                if cursor[0] != generation:
                    raise FleetProjectionError("source generation changed; recovery required")
                for item in sorted(mutations, key=lambda value: int(value["source_seq"])):
                    await self._apply_projection_mutation(
                        db,
                        event_id=str(item["event_id"]),
                        source_node_id=source_node_id,
                        generation=generation,
                        source_seq=int(item["source_seq"]),
                        event_type=str(item["event_type"]),
                        entity_type=str(item["entity_type"]),
                        entity_id=str(item["entity_id"]),
                        entity_revision=int(item["entity_revision"]),
                        payload_version=int(item.get("payload_version") or 2),
                        payload=item.get("payload") or {},
                        authority_node_id=item.get("authority_node_id"),
                        authority_epoch=item.get("authority_epoch"),
                        created_at=str(item.get("created_at") or stamp),
                        stamp=stamp,
                    )
                page_cursor = int(page.get("next_cursor") or cursor[1])
                await self._prune_recent_terminal_commands(
                    db,
                    source_node_id=source_node_id,
                    generation=generation,
                    source_seq=page_cursor,
                    stamp=stamp,
                )
                await db.execute(
                    "UPDATE projection_sources SET source_seq=MAX(source_seq,?),"
                    "freshness='stale',updated_at=? WHERE source_node_id=?",
                    (page_cursor, stamp, source_node_id),
                )
                await self._prune_events(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.meta()

    async def mark_scope_status(
        self,
        source_node_id: str,
        scopes: list[str],
        status: str,
        reason: str | None = None,
    ) -> None:
        if status not in {"LIVE", "CATCHING_UP", "DEGRADED", "OFFLINE_AUTH"}:
            raise ValueError("invalid projection scope status")
        stamp = utc_text()
        async with self._connect("fleet_projection_scope_status") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for scope in scopes:
                    await db.execute(
                        "INSERT INTO projection_scope_status("
                        "source_node_id,scope,status,reason,updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(source_node_id,scope) DO UPDATE SET "
                        "status=excluded.status,reason=excluded.reason,updated_at=excluded.updated_at",
                        (source_node_id, scope, status, reason, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_source_live(
        self, source_node_id: str, generation: str, scopes: list[str]
    ) -> None:
        stamp = utc_text()
        async with self._connect("fleet_projection_source_live") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE projection_sources SET freshness='fresh',updated_at=? "
                    "WHERE source_node_id=? AND source_stream_generation=?",
                    (stamp, source_node_id, generation),
                )
                for scope in scopes:
                    await db.execute(
                        "INSERT INTO projection_scope_status("
                        "source_node_id,scope,status,reason,updated_at) VALUES(?,?,'LIVE',NULL,?) "
                        "ON CONFLICT(source_node_id,scope) DO UPDATE SET "
                        "status='LIVE',reason=NULL,updated_at=excluded.updated_at",
                        (source_node_id, scope, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def source_scopes(self, source_node_id: str) -> list[str]:
        async with self._connect("fleet_projection_source_scopes") as db:
            rows = await (
                await db.execute(
                    "SELECT scope FROM projection_scope_status "
                    "WHERE source_node_id=? ORDER BY scope",
                    (source_node_id,),
                )
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def source_state(self, source_node_id: str) -> dict | None:
        async with self._connect("fleet_projection_source_state") as db:
            row = await (
                await db.execute(
                    "SELECT source_stream_generation,source_seq,freshness,updated_at "
                    "FROM projection_sources WHERE source_node_id=?",
                    (source_node_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "source_node_id": source_node_id,
            "source_stream_generation": row[0],
            "source_seq": int(row[1]),
            "freshness": row[2],
            "updated_at": row[3],
        }

    async def mark_source(self, source_node_id: str, generation: str, freshness: str) -> None:
        if freshness not in {"fresh", "stale", "reset_required", "unavailable"}:
            raise ValueError("invalid source freshness")
        stamp = utc_text()
        async with self._connect("fleet_projection_source_mark") as db:
            await db.execute(
                "INSERT INTO projection_sources("
                "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                ") VALUES(?,?,0,?,?) ON CONFLICT(source_node_id) DO UPDATE SET "
                "freshness=excluded.freshness,updated_at=excluded.updated_at",
                (source_node_id, generation, freshness, stamp),
            )
            await db.commit()

    async def put_runtime_overlay(self, source_node_id: str, payload: dict) -> None:
        stamp = utc_text()
        async with self._connect("fleet_projection_overlay_put") as db:
            await db.execute(
                "INSERT INTO projection_runtime_overlay("
                "source_node_id,payload_json,observed_at,freshness) VALUES(?,?,?,'fresh') "
                "ON CONFLICT(source_node_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,observed_at=excluded.observed_at,"
                "freshness='fresh'",
                (source_node_id, json.dumps(payload, separators=(",", ":")), stamp),
            )
            await db.commit()

    async def clear_runtime_overlay(self, source_node_id: str) -> None:
        async with self._connect("fleet_projection_overlay_clear") as db:
            await db.execute(
                "UPDATE projection_runtime_overlay SET freshness='stale' WHERE source_node_id=?",
                (source_node_id,),
            )
            await db.commit()

    async def snapshot(self) -> dict:
        meta = await self.meta()
        async with self._connect("fleet_projection_snapshot") as db:
            sources = await (
                await db.execute(
                    "SELECT source_node_id,source_stream_generation,source_seq,"
                    "freshness,updated_at "
                    "FROM projection_sources ORDER BY source_node_id"
                )
            ).fetchall()
            entities = await (
                await db.execute(
                    "SELECT source_node_id,entity_type,entity_id,entity_revision,payload_version,"
                    "payload_json,authority_node_id,authority_epoch,source_stream_generation,"
                    "source_seq,projection_seq,updated_at FROM projection_entities "
                    "ORDER BY entity_type,entity_id,source_node_id"
                )
            ).fetchall()
            overlays = await (
                await db.execute(
                    "SELECT source_node_id,payload_json,observed_at,freshness "
                    "FROM projection_runtime_overlay ORDER BY source_node_id"
                )
            ).fetchall()
            scope_statuses = await (
                await db.execute(
                    "SELECT source_node_id,scope,status,reason,updated_at "
                    "FROM projection_scope_status ORDER BY source_node_id,scope"
                )
            ).fetchall()
        return {
            **meta,
            "sources": [
                {
                    "source_node_id": row[0],
                    "source_stream_generation": row[1],
                    "source_seq": int(row[2]),
                    "freshness": row[3],
                    "updated_at": row[4],
                }
                for row in sources
            ],
            "entities": [
                {
                    "source_node_id": row[0],
                    "entity_type": row[1],
                    "entity_id": row[2],
                    "entity_revision": int(row[3]),
                    "payload_version": int(row[4]),
                    "payload": json.loads(row[5]),
                    "authority_node_id": row[6],
                    "authority_epoch": int(row[7]) if row[7] is not None else None,
                    "source_stream_generation": row[8],
                    "source_seq": int(row[9]),
                    "projection_seq": int(row[10]),
                    "updated_at": row[11],
                }
                for row in entities
            ],
            "scope_statuses": [
                {
                    "source_node_id": row[0],
                    "scope": row[1],
                    "status": row[2],
                    "reason": row[3],
                    "updated_at": row[4],
                }
                for row in scope_statuses
            ],
            "runtime_overlays": [
                {
                    "source_node_id": row[0],
                    "payload": json.loads(row[1]),
                    "observed_at": row[2],
                    "freshness": row[3],
                }
                for row in overlays
            ],
        }

    async def events(self, *, since: int = 0, limit: int = 100) -> dict:
        since = max(0, int(since))
        limit = max(1, min(int(limit), 1000))
        meta = await self.meta()
        async with self._connect("fleet_projection_events") as db:
            bounds = await (
                await db.execute(
                    "SELECT MIN(projection_seq),MAX(projection_seq) FROM projection_events"
                )
            ).fetchone()
            oldest = int(bounds[0]) if bounds and bounds[0] is not None else None
            newest = int(bounds[1]) if bounds and bounds[1] is not None else None
            reset_required = oldest is not None and since < oldest - 1
            rows = []
            if not reset_required:
                rows = await (
                    await db.execute(
                        "SELECT projection_epoch,projection_seq,event_id,source_node_id,"
                        "source_stream_generation,source_seq,event_type,entity_type,entity_id,"
                        "entity_revision,payload_version,payload_json,created_at "
                        "FROM projection_events WHERE projection_seq>? "
                        "ORDER BY projection_seq LIMIT ?",
                        (since, limit),
                    )
                ).fetchall()
        return {
            "projection_epoch": meta["projection_epoch"],
            "projection_seq": meta["projection_seq"],
            "oldest_projection_seq": oldest,
            "newest_projection_seq": newest,
            "reset_required": reset_required,
            "events": [
                {
                    "projection_epoch": int(row[0]),
                    "projection_seq": int(row[1]),
                    "event_id": row[2],
                    "source_node_id": row[3],
                    "source_stream_generation": row[4],
                    "source_seq": int(row[5]),
                    "event_type": row[6],
                    "entity_type": row[7],
                    "entity_id": row[8],
                    "entity_revision": int(row[9]),
                    "payload_version": int(row[10]),
                    "payload": json.loads(row[11]),
                    "created_at": row[12],
                }
                for row in rows
            ],
        }

    async def apply_owner_overlays(
        self,
        overlays: list[dict],
        *,
        owner_node_id: str,
        projection_epoch: int,
        sources: list[dict] | None = None,
        scope_statuses: list[dict] | None = None,
    ) -> None:
        if self.role != "follower":
            raise FleetProjectionError("only follower can apply owner overlays")
        if owner_node_id != self.owner_node_id:
            raise FleetProjectionError("projection owner mismatch")
        current = await self.meta()
        if int(projection_epoch) != current["projection_epoch"]:
            if int(projection_epoch) < current["projection_epoch"]:
                raise FleetProjectionError("old projection epoch fenced")
            raise FleetProjectionError("projection epoch changed; snapshot required")
        async with self._connect("fleet_projection_apply_owner_overlays") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM projection_runtime_overlay")
                if sources is not None:
                    await db.execute("DELETE FROM projection_sources")
                    for item in sources:
                        await db.execute(
                            "INSERT INTO projection_sources("
                            "source_node_id,source_stream_generation,source_seq,"
                            "freshness,updated_at) VALUES(?,?,?,?,?)",
                            (
                                item["source_node_id"],
                                item["source_stream_generation"],
                                int(item["source_seq"]),
                                item["freshness"],
                                item.get("updated_at") or utc_text(),
                            ),
                        )
                if scope_statuses is not None:
                    await db.execute("DELETE FROM projection_scope_status")
                    for item in scope_statuses:
                        await db.execute(
                            "INSERT INTO projection_scope_status("
                            "source_node_id,scope,status,reason,updated_at) "
                            "VALUES(?,?,?,?,?)",
                            (
                                item["source_node_id"],
                                item["scope"],
                                item["status"],
                                item.get("reason"),
                                item.get("updated_at") or utc_text(),
                            ),
                        )
                for item in overlays:
                    await db.execute(
                        "INSERT INTO projection_runtime_overlay("
                        "source_node_id,payload_json,observed_at,freshness) VALUES(?,?,?,?)",
                        (
                            item["source_node_id"],
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item["observed_at"],
                            item.get("freshness") or "stale",
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def replica_snapshot(self) -> dict:
        if self.role != "owner":
            raise FleetProjectionError("only owner can publish replica snapshot")
        return await self.snapshot()

    async def apply_owner_snapshot(
        self,
        snapshot: dict,
        *,
        owner_node_id: str,
    ) -> dict:
        if self.role != "follower":
            raise FleetProjectionError("only follower can apply owner snapshot")
        if owner_node_id != self.owner_node_id:
            raise FleetProjectionError("projection owner mismatch")
        if snapshot.get("fleet_id") != self.fleet_id:
            raise FleetProjectionError("projection fleet_id mismatch")
        incoming_epoch = int(snapshot["projection_epoch"])
        incoming_seq = int(snapshot["projection_seq"])
        current = await self.meta()
        if incoming_epoch < current["projection_epoch"]:
            raise FleetProjectionError("old projection epoch fenced")
        stamp = utc_text()
        async with self._connect("fleet_projection_apply_owner_snapshot") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM projection_events")
                await db.execute("DELETE FROM projection_entities")
                await db.execute("DELETE FROM projection_sources")
                await db.execute("DELETE FROM projection_runtime_overlay")
                await db.execute("DELETE FROM projection_scope_status")
                for item in snapshot.get("sources") or []:
                    await db.execute(
                        "INSERT INTO projection_sources("
                        "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                        ") VALUES(?,?,?,?,?)",
                        (
                            item["source_node_id"],
                            item["source_stream_generation"],
                            int(item["source_seq"]),
                            item["freshness"],
                            item.get("updated_at") or stamp,
                        ),
                    )
                for item in snapshot.get("entities") or []:
                    await db.execute(
                        "INSERT INTO projection_entities("
                        "source_node_id,entity_type,entity_id,entity_revision,payload_version,"
                        "payload_json,authority_node_id,authority_epoch,source_stream_generation,"
                        "source_seq,projection_seq,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            item["source_node_id"],
                            item["entity_type"],
                            item["entity_id"],
                            int(item["entity_revision"]),
                            int(item.get("payload_version") or 1),
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item.get("authority_node_id"),
                            item.get("authority_epoch"),
                            item["source_stream_generation"],
                            int(item["source_seq"]),
                            int(item["projection_seq"]),
                            item.get("updated_at") or stamp,
                        ),
                    )
                for item in snapshot.get("scope_statuses") or []:
                    await db.execute(
                        "INSERT INTO projection_scope_status("
                        "source_node_id,scope,status,reason,updated_at) VALUES(?,?,?,?,?)",
                        (
                            item["source_node_id"],
                            item["scope"],
                            item["status"],
                            item.get("reason"),
                            item.get("updated_at") or stamp,
                        ),
                    )
                for item in snapshot.get("runtime_overlays") or []:
                    await db.execute(
                        "INSERT INTO projection_runtime_overlay("
                        "source_node_id,payload_json,observed_at,freshness) VALUES(?,?,?,?)",
                        (
                            item["source_node_id"],
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item.get("observed_at") or stamp,
                            item.get("freshness") or "stale",
                        ),
                    )
                await db.execute(
                    "UPDATE projection_meta SET owner_node_id=?,projection_epoch=?,"
                    "projection_seq=?,updated_at=? WHERE singleton=1",
                    (owner_node_id, incoming_epoch, incoming_seq, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.meta()

    async def apply_owner_events(
        self,
        page: dict,
        *,
        owner_node_id: str,
    ) -> dict:
        if self.role != "follower":
            raise FleetProjectionError("only follower can apply owner events")
        if owner_node_id != self.owner_node_id:
            raise FleetProjectionError("projection owner mismatch")
        incoming_epoch = int(page["projection_epoch"])
        current = await self.meta()
        if incoming_epoch != current["projection_epoch"]:
            if incoming_epoch < current["projection_epoch"]:
                raise FleetProjectionError("old projection epoch fenced")
            raise FleetProjectionError("projection epoch changed; snapshot required")
        events = list(page.get("events") or [])
        stamp = utc_text()
        async with self._connect("fleet_projection_apply_owner_events") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                expected = current["projection_seq"] + 1
                for item in events:
                    seq = int(item["projection_seq"])
                    if seq < expected:
                        duplicate = await (
                            await db.execute(
                                "SELECT event_id FROM projection_events WHERE projection_seq=?",
                                (seq,),
                            )
                        ).fetchone()
                        if duplicate is None or duplicate[0] != item["event_id"]:
                            raise FleetProjectionError("projection prefix conflict")
                        continue
                    if seq != expected:
                        raise FleetProjectionError("projection prefix gap")
                    await db.execute(
                        "INSERT INTO projection_events("
                        "projection_epoch,projection_seq,event_id,source_node_id,"
                        "source_stream_generation,source_seq,event_type,entity_type,entity_id,"
                        "entity_revision,payload_version,payload_json,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            incoming_epoch,
                            seq,
                            item["event_id"],
                            item["source_node_id"],
                            item["source_stream_generation"],
                            int(item["source_seq"]),
                            item["event_type"],
                            item["entity_type"],
                            item["entity_id"],
                            int(item["entity_revision"]),
                            int(item.get("payload_version") or 1),
                            json.dumps(item.get("payload") or {}, separators=(",", ":")),
                            item["created_at"],
                        ),
                    )
                    current_entity = await (
                        await db.execute(
                            "SELECT entity_revision FROM projection_entities "
                            "WHERE source_node_id=? AND entity_type=? AND entity_id=?",
                            (
                                item["source_node_id"],
                                item["entity_type"],
                                item["entity_id"],
                            ),
                        )
                    ).fetchone()
                    if item.get("event_type") == "projection.remove":
                        await db.execute(
                            "DELETE FROM projection_entities "
                            "WHERE source_node_id=? AND entity_type=? AND entity_id=?",
                            (
                                item["source_node_id"],
                                item["entity_type"],
                                item["entity_id"],
                            ),
                        )
                    elif current_entity is None or int(item["entity_revision"]) >= int(
                        current_entity[0]
                    ):
                        await db.execute(
                            "INSERT INTO projection_entities("
                            "source_node_id,entity_type,entity_id,entity_revision,payload_version,"
                            "payload_json,authority_node_id,authority_epoch,"
                            "source_stream_generation,source_seq,projection_seq,updated_at"
                            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(source_node_id,entity_type,entity_id) DO UPDATE SET "
                            "entity_revision=excluded.entity_revision,"
                            "payload_version=excluded.payload_version,"
                            "payload_json=excluded.payload_json,"
                            "authority_node_id=excluded.authority_node_id,"
                            "authority_epoch=excluded.authority_epoch,"
                            "source_stream_generation=excluded.source_stream_generation,"
                            "source_seq=excluded.source_seq,projection_seq=excluded.projection_seq,"
                            "updated_at=excluded.updated_at",
                            (
                                item["source_node_id"],
                                item["entity_type"],
                                item["entity_id"],
                                int(item["entity_revision"]),
                                int(item.get("payload_version") or 1),
                                json.dumps(item.get("payload") or {}, separators=(",", ":")),
                                item.get("authority_node_id"),
                                item.get("authority_epoch"),
                                item["source_stream_generation"],
                                int(item["source_seq"]),
                                seq,
                                stamp,
                            ),
                        )
                    await db.execute(
                        "INSERT INTO projection_sources("
                        "source_node_id,source_stream_generation,source_seq,freshness,updated_at"
                        ") VALUES(?,?,?,'fresh',?) "
                        "ON CONFLICT(source_node_id) DO UPDATE SET "
                        "source_stream_generation=excluded.source_stream_generation,"
                        "source_seq=MAX(projection_sources.source_seq,excluded.source_seq),"
                        "freshness='fresh',updated_at=excluded.updated_at",
                        (
                            item["source_node_id"],
                            item["source_stream_generation"],
                            int(item["source_seq"]),
                            stamp,
                        ),
                    )
                    expected = seq + 1
                if events:
                    await db.execute(
                        "UPDATE projection_meta SET projection_seq=?,updated_at=? "
                        "WHERE singleton=1",
                        (expected - 1, stamp),
                    )
                await self._prune_events(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.meta()

    async def promote(
        self,
        *,
        expected_epoch: int,
        control_authorized: bool = False,
    ) -> dict:
        if not control_authorized:
            raise FleetProjectionError("projection promotion requires control authority")
        if self.role != "follower":
            raise FleetProjectionError("only follower can be promoted")
        stamp = utc_text()
        async with self._connect("fleet_projection_promote") as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT projection_epoch FROM projection_meta WHERE singleton=1"
                )
            ).fetchone()
            if row is None or int(row[0]) != int(expected_epoch):
                await db.rollback()
                raise FleetProjectionError("projection epoch conflict")
            await db.execute(
                "UPDATE projection_meta SET role='owner',owner_node_id=?,"
                "projection_epoch=projection_epoch+1,updated_at=? WHERE singleton=1",
                (self.node_id, stamp),
            )
            await db.commit()
        self.role = "owner"
        self.owner_node_id = self.node_id
        return await self.meta()

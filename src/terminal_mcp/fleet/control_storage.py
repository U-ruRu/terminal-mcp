from __future__ import annotations

import base64
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.fleet.protocol import validate_capabilities, validate_protocol_id
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import (
    SqliteDiagnostics,
    cancellation_safe_connection,
    observed_connection,
)


class FleetControlError(RuntimeError):
    pass


SQLITE_MAIN_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_MAGICS = {bytes.fromhex("377f0682"), bytes.fromhex("377f0683")}


class FleetControlStore:
    """Critical monotonic Fleet routing metadata; never owns slot/session truth."""

    SCHEMA_VERSION = 3

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

    def _main_file_ready(self) -> bool:
        path = Path(self.path)
        return path.exists() and path.stat().st_size > 0

    def _validate_main_file_header(self) -> None:
        path = Path(self.path)
        if not self._main_file_ready():
            return
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_MAIN_HEADER))
        if header == SQLITE_MAIN_HEADER:
            return
        if header[:4] in SQLITE_WAL_MAGICS:
            raise FleetControlError("fleet_control_main_is_wal")
        raise FleetControlError("fleet_control_invalid_header")

    async def healthy(self) -> bool:
        try:
            self._validate_main_file_header()
            if not self._main_file_ready():
                return False
            uri = f"file:{Path(self.path)}?mode=ro"
            async with cancellation_safe_connection(aiosqlite.connect, uri, uri=True) as db:
                row = await (await db.execute("PRAGMA quick_check(1)")).fetchone()
            return row is not None and row[0] == "ok"
        except (OSError, aiosqlite.Error, FleetControlError):
            return False

    @asynccontextmanager
    async def _connect(self, operation: str):
        self._validate_main_file_header()
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
                schema_table = await (
                    await db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_schema'"
                    )
                ).fetchone()
                existing_version = None
                if schema_table is not None:
                    row = await (
                        await db.execute("SELECT version FROM control_schema WHERE singleton=1")
                    ).fetchone()
                    existing_version = int(row[0]) if row is not None else None
                    if existing_version is not None and existing_version > self.SCHEMA_VERSION:
                        raise FleetControlError("unsupported fleet_control schema version")
                    if existing_version is not None and existing_version < 1:
                        raise FleetControlError("unsupported fleet_control schema version")

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
                    CREATE TABLE IF NOT EXISTS projection_topology(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        owner_node_id TEXT NOT NULL,
                        follower_node_id TEXT NOT NULL,
                        projection_epoch INTEGER NOT NULL CHECK(projection_epoch>0),
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

                columns = {
                    row[1]
                    for row in await (
                        await db.execute("PRAGMA table_info(control_meta)")
                    ).fetchall()
                }
                for name in (
                    "topology_revision",
                    "trust_revision",
                    "access_policy_revision",
                ):
                    if name not in columns:
                        await db.execute(
                            f"ALTER TABLE control_meta ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                        )

                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS managed_mesh(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        mesh_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        adopted INTEGER NOT NULL DEFAULT 0 CHECK(adopted IN (0,1)),
                        adopted_at TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS managed_meshes(
                        mesh_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS managed_nodes(
                        node_id TEXT PRIMARY KEY,
                        origin TEXT,
                        public_key TEXT,
                        auth_token TEXT,
                        mesh_id TEXT,
                        state TEXT NOT NULL DEFAULT 'active'
                            CHECK(state IN ('active','draining','offline','detached')),
                        desired_topology_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(desired_topology_revision>=0),
                        applied_topology_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(applied_topology_revision>=0),
                        desired_trust_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(desired_trust_revision>=0),
                        applied_trust_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(applied_trust_revision>=0),
                        desired_policy_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(desired_policy_revision>=0),
                        applied_policy_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(applied_policy_revision>=0),
                        last_error TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS managed_identity(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        active_private_key TEXT NOT NULL,
                        active_generation INTEGER NOT NULL CHECK(active_generation>0),
                        pending_private_key TEXT,
                        pending_generation INTEGER,
                        ingress_token TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS access_policy(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        duration_seconds INTEGER NOT NULL CHECK(duration_seconds>0),
                        warning_after_seconds INTEGER NOT NULL CHECK(warning_after_seconds>0),
                        alert_after_seconds INTEGER NOT NULL CHECK(alert_after_seconds>0),
                        rearm_after_seconds INTEGER NOT NULL CHECK(rearm_after_seconds>0),
                        legacy_admission_enabled INTEGER NOT NULL
                            CHECK(legacy_admission_enabled IN (0,1)),
                        revision INTEGER NOT NULL CHECK(revision>0),
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_managed_nodes_state
                        ON managed_nodes(state,node_id);
                    """
                )

                identity_columns = {
                    row[1]
                    for row in await (
                        await db.execute("PRAGMA table_info(managed_identity)")
                    ).fetchall()
                }
                if "ingress_token" not in identity_columns:
                    await db.execute("ALTER TABLE managed_identity ADD COLUMN ingress_token TEXT")

                node_columns = {
                    row[1]
                    for row in await (
                        await db.execute("PRAGMA table_info(managed_nodes)")
                    ).fetchall()
                }
                if "mesh_id" not in node_columns:
                    await db.execute("ALTER TABLE managed_nodes ADD COLUMN mesh_id TEXT")

                if existing_version is not None and existing_version < 3:
                    legacy_mesh = await (
                        await db.execute(
                            "SELECT mesh_id,display_name,adopted,adopted_at,updated_at "
                            "FROM managed_mesh WHERE singleton=1"
                        )
                    ).fetchone()
                    if legacy_mesh is not None and bool(legacy_mesh[2]):
                        await db.execute(
                            "INSERT OR REPLACE INTO managed_meshes("
                            "mesh_id,display_name,active,created_at,updated_at"
                            ") VALUES(?,?,1,?,?)",
                            (
                                legacy_mesh[0],
                                legacy_mesh[1],
                                legacy_mesh[3] or legacy_mesh[4],
                                legacy_mesh[4],
                            ),
                        )
                        await db.execute(
                            "UPDATE managed_nodes SET mesh_id=? "
                            "WHERE state!='detached' AND mesh_id IS NULL",
                            (legacy_mesh[0],),
                        )
                    await db.execute(
                        "UPDATE managed_nodes SET mesh_id=NULL WHERE state='detached'"
                    )

                if existing_version is None:
                    await db.execute(
                        "INSERT OR REPLACE INTO control_schema(singleton,version) VALUES(1,?)",
                        (self.SCHEMA_VERSION,),
                    )
                elif existing_version < self.SCHEMA_VERSION:
                    await db.execute(
                        "UPDATE control_schema SET version=? WHERE singleton=1",
                        (self.SCHEMA_VERSION,),
                    )

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
                        "routing_revision,topology_revision,trust_revision,"
                        "access_policy_revision,updated_at"
                        ") VALUES(1,?,?,?,?,?,?,?,?)",
                        (
                            self.fleet_id,
                            self.node_id,
                            self.control_node_id,
                            0,
                            0,
                            0,
                            0,
                            stamp,
                        ),
                    )
                elif (meta[0], meta[1]) != (self.fleet_id, self.node_id):
                    raise FleetControlError(
                        "fleet_control identity does not match persisted metadata"
                    )
                else:
                    # The persisted control authority is runtime state. The env value is
                    # only a bootstrap default and must not overwrite an explicit rehome.
                    self.control_node_id = validate_protocol_id(
                        str(meta[2]), "control_node_id"
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

    async def claim_local_control_authority(
        self, *, policy: dict | None = None, now: str | None = None
    ) -> str:
        """Make this standalone node its own control-domain authority."""
        stamp = now or utc_text()
        async with self._connect("fleet_control_claim_local_authority") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT mesh_id,state FROM managed_nodes WHERE node_id=?",
                        (self.node_id,),
                    )
                ).fetchone()
                if row is not None and row[1] != "detached" and row[0] is not None:
                    raise FleetControlError("control_authority_rehome_requires_standalone")
                await db.execute(
                    "UPDATE control_meta SET control_node_id=?,routing_revision=routing_revision+1,"
                    "updated_at=? WHERE singleton=1",
                    (self.node_id, stamp),
                )
                ingress_token = secrets.token_urlsafe(32)
                await db.execute(
                    "UPDATE managed_identity SET ingress_token=?,updated_at=? WHERE singleton=1",
                    (ingress_token, stamp),
                )
                # Old authority observations remain historical only. Trust material is kept
                # in rows so an explicit rejoin does not require pairing or file repair.
                await db.execute("UPDATE managed_meshes SET active=0,updated_at=?", (stamp,))
                await db.execute(
                    "UPDATE managed_nodes SET mesh_id=NULL,state='detached',last_error=NULL,"
                    "updated_at=?",
                    (stamp,),
                )
                if row is not None:
                    await db.execute(
                        "UPDATE managed_nodes SET state='active',mesh_id=NULL,auth_token=?,"
                        "updated_at=? "
                        "WHERE node_id=?",
                        (ingress_token, stamp, self.node_id),
                    )
                if policy is None:
                    await db.execute("DELETE FROM access_policy")
                else:
                    duration = int(policy["duration_seconds"])
                    warning = int(policy["warning_after_seconds"])
                    alert = int(policy["alert_after_seconds"])
                    rearm = int(policy["rearm_after_seconds"])
                    legacy = int(bool(policy["legacy_admission_enabled"]))
                    if duration <= 0 or not (0 < warning < alert < duration) or rearm <= 0:
                        raise ValueError("invalid access policy")
                    revision_row = await (
                        await db.execute(
                            "SELECT access_policy_revision FROM control_meta WHERE singleton=1"
                        )
                    ).fetchone()
                    revision = int(revision_row[0]) if revision_row else 1
                    await db.execute(
                        "INSERT INTO access_policy("
                        "singleton,duration_seconds,warning_after_seconds,alert_after_seconds,"
                        "rearm_after_seconds,legacy_admission_enabled,revision,updated_at"
                        ") VALUES(1,?,?,?,?,?,?,?) "
                        "ON CONFLICT(singleton) DO UPDATE SET "
                        "duration_seconds=excluded.duration_seconds,"
                        "warning_after_seconds=excluded.warning_after_seconds,"
                        "alert_after_seconds=excluded.alert_after_seconds,"
                        "rearm_after_seconds=excluded.rearm_after_seconds,"
                        "legacy_admission_enabled=excluded.legacy_admission_enabled,"
                        "revision=excluded.revision,updated_at=excluded.updated_at",
                        (duration, warning, alert, rearm, legacy, revision, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        self.control_node_id = self.node_id
        return self.control_node_id

    async def schema_version(self) -> int:
        async with self._connect("fleet_control_schema_version") as db:
            row = await (
                await db.execute("SELECT version FROM control_schema WHERE singleton=1")
            ).fetchone()
        if row is None:
            raise FleetControlError("fleet_control is not initialized")
        return int(row[0])

    async def _control_revisions(self, db) -> tuple[int, int, int]:
        row = await (
            await db.execute(
                "SELECT topology_revision,trust_revision,access_policy_revision "
                "FROM control_meta WHERE singleton=1"
            )
        ).fetchone()
        if row is None:
            raise FleetControlError("fleet_control is not initialized")
        return int(row[0]), int(row[1]), int(row[2])

    async def _bump_revision(self, db, kind: str, stamp: str) -> int:
        column = {
            "topology": "topology_revision",
            "trust": "trust_revision",
            "policy": "access_policy_revision",
        }.get(kind)
        if column is None:
            raise ValueError("invalid control revision kind")
        row = await (
            await db.execute(f"SELECT {column} FROM control_meta WHERE singleton=1")
        ).fetchone()
        if row is None:
            raise FleetControlError("fleet_control is not initialized")
        revision = int(row[0]) + 1
        await db.execute(
            f"UPDATE control_meta SET {column}=?,updated_at=? WHERE singleton=1",
            (revision, stamp),
        )
        return revision

    async def managed_meshes(self, *, include_inactive: bool = False) -> list[dict]:
        where = "" if include_inactive else " WHERE active=1"
        async with self._connect("fleet_control_managed_meshes") as db:
            rows = await (
                await db.execute(
                    "SELECT mesh_id,display_name,active,created_at,updated_at "
                    f"FROM managed_meshes{where} ORDER BY display_name,mesh_id"
                )
            ).fetchall()
        return [
            {
                "mesh_id": row[0],
                "display_name": row[1],
                "adopted": bool(row[2]),
                "adopted_at": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    async def managed_mesh(self) -> dict | None:
        async with self._connect("fleet_control_managed_mesh") as db:
            row = await (
                await db.execute(
                    "SELECT m.mesh_id,m.display_name,m.active,m.created_at,m.updated_at "
                    "FROM managed_nodes n JOIN managed_meshes m ON m.mesh_id=n.mesh_id "
                    "WHERE n.node_id=? AND n.state!='detached' AND m.active=1",
                    (self.node_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "mesh_id": row[0],
            "display_name": row[1],
            "adopted": bool(row[2]),
            "adopted_at": row[3],
            "updated_at": row[4],
        }

    async def managed_nodes(self, *, include_detached: bool = False) -> list[dict]:
        where = "" if include_detached else " WHERE state!='detached'"
        async with self._connect("fleet_control_managed_nodes") as db:
            rows = await (
                await db.execute(
                    "SELECT node_id,origin,public_key,mesh_id,state,"
                    "desired_topology_revision,applied_topology_revision,"
                    "desired_trust_revision,applied_trust_revision,"
                    "desired_policy_revision,applied_policy_revision,last_error,updated_at "
                    f"FROM managed_nodes{where} ORDER BY node_id"
                )
            ).fetchall()
        return [
            {
                "node_id": row[0],
                "origin": row[1],
                "public_key": row[2],
                "mesh_id": row[3],
                "state": row[4],
                "desired_topology_revision": int(row[5]),
                "applied_topology_revision": int(row[6]),
                "desired_trust_revision": int(row[7]),
                "applied_trust_revision": int(row[8]),
                "desired_policy_revision": int(row[9]),
                "applied_policy_revision": int(row[10]),
                "last_error": row[11],
                "updated_at": row[12],
            }
            for row in rows
        ]

    @staticmethod
    def _encode_private_key(key: Ed25519PrivateKey) -> str:
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @staticmethod
    def _public_from_private(encoded: str) -> str:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        key = Ed25519PrivateKey.from_private_bytes(raw)
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return base64.urlsafe_b64encode(public).rstrip(b"=").decode()

    async def ensure_managed_identity(self, bootstrap_private_key: str) -> dict:
        stamp = utc_text()
        public_key = self._public_from_private(bootstrap_private_key)
        ingress_token = secrets.token_urlsafe(32)
        async with self._connect("fleet_control_managed_identity_ensure") as db:
            await db.execute(
                "INSERT OR IGNORE INTO managed_identity("
                "singleton,active_private_key,active_generation,pending_private_key,"
                "pending_generation,ingress_token,updated_at"
                ") VALUES(1,?,1,NULL,NULL,?,?)",
                (bootstrap_private_key, ingress_token, stamp),
            )
            row = await (
                await db.execute(
                    "SELECT active_private_key,active_generation,pending_private_key,"
                    "pending_generation,ingress_token,updated_at "
                    "FROM managed_identity WHERE singleton=1"
                )
            ).fetchone()
            if row is not None and not row[4]:
                ingress_token = secrets.token_urlsafe(32)
                await db.execute(
                    "UPDATE managed_identity SET ingress_token=?,updated_at=? WHERE singleton=1",
                    (ingress_token, stamp),
                )
                row = (*row[:4], ingress_token, stamp)
            await db.commit()
        if row is None:
            raise FleetControlError("managed identity unavailable")
        return {
            "private_key": row[0],
            "public_key": self._public_from_private(row[0]) if row[0] else public_key,
            "generation": int(row[1]),
            "pending_public_key": self._public_from_private(row[2]) if row[2] else None,
            "pending_generation": int(row[3]) if row[3] is not None else None,
            "ingress_token": row[4],
            "updated_at": row[5],
        }

    async def managed_identity(self) -> dict | None:
        async with self._connect("fleet_control_managed_identity_read") as db:
            row = await (
                await db.execute(
                    "SELECT active_private_key,active_generation,pending_private_key,"
                    "pending_generation,ingress_token,updated_at "
                    "FROM managed_identity WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "private_key": row[0],
            "public_key": self._public_from_private(row[0]),
            "generation": int(row[1]),
            "pending_public_key": self._public_from_private(row[2]) if row[2] else None,
            "pending_generation": int(row[3]) if row[3] is not None else None,
            "ingress_token": row[4],
            "updated_at": row[5],
        }

    async def managed_node(self, node_id: str) -> dict | None:
        node_id = validate_protocol_id(node_id, "node_id")
        async with self._connect("fleet_control_managed_node") as db:
            row = await (
                await db.execute(
                    "SELECT node_id,origin,public_key,auth_token,mesh_id,state "
                    "FROM managed_nodes WHERE node_id=?",
                    (node_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "node_id": row[0],
            "origin": row[1],
            "public_key": row[2],
            "auth_token": row[3],
            "mesh_id": row[4],
            "state": row[5],
        }

    async def prepare_managed_identity_rotation(self) -> dict:
        stamp = utc_text()
        async with self._connect("fleet_control_managed_identity_prepare") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT active_generation,pending_private_key,pending_generation "
                        "FROM managed_identity WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    raise FleetControlError("managed identity unavailable")
                pending_private = row[1]
                pending_generation = row[2]
                if not pending_private:
                    pending_private = self._encode_private_key(Ed25519PrivateKey.generate())
                    pending_generation = int(row[0]) + 1
                    await db.execute(
                        "UPDATE managed_identity SET pending_private_key=?,pending_generation=?,"
                        "updated_at=? WHERE singleton=1",
                        (pending_private, pending_generation, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {
            "public_key": self._public_from_private(pending_private),
            "generation": int(pending_generation),
        }

    async def commit_managed_identity_rotation(self, generation: int) -> dict:
        stamp = utc_text()
        async with self._connect("fleet_control_managed_identity_commit") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT pending_private_key,pending_generation FROM managed_identity "
                        "WHERE singleton=1"
                    )
                ).fetchone()
                if row is None or not row[0] or int(row[1] or 0) != int(generation):
                    raise FleetControlError("managed identity rotation mismatch")
                await db.execute(
                    "UPDATE managed_identity SET active_private_key=pending_private_key,"
                    "active_generation=pending_generation,pending_private_key=NULL,"
                    "pending_generation=NULL,updated_at=? WHERE singleton=1",
                    (stamp,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        identity = await self.managed_identity()
        if identity is None:
            raise FleetControlError("managed identity unavailable")
        return identity

    async def update_managed_node_public_key(
        self,
        node_id: str,
        *,
        public_key: str,
        expected_trust_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        node_id = validate_protocol_id(node_id, "node_id")
        public_key = str(public_key or "").strip()
        if not public_key:
            raise ValueError("public_key is required")
        # Parse as an Ed25519 public key without retaining any private material.
        raw = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        if len(raw) != 32:
            raise ValueError("public_key must decode to 32 bytes")
        stamp = utc_text()
        async with self._connect("fleet_control_managed_trust_rotate") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state,public_key FROM managed_nodes WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if row is None or row[0] == "detached":
                    raise FleetControlError("managed_node_not_found")
                _, trust, _ = await self._control_revisions(db)
                if expected_trust_revision is not None and trust != int(expected_trust_revision):
                    raise FleetControlError("trust_revision_conflict")
                if row[1] != public_key:
                    trust = await self._bump_revision(db, "trust", stamp)
                    await db.execute(
                        "UPDATE managed_nodes SET public_key=?,desired_trust_revision=?,"
                        "last_error=NULL,updated_at=? WHERE node_id=?",
                        (public_key, trust, stamp, node_id),
                    )
                    await db.execute(
                        "UPDATE managed_nodes SET desired_trust_revision=? "
                        "WHERE state!='detached'",
                        (trust,),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def managed_peer_material(self) -> list[dict]:
        async with self._connect("fleet_control_managed_peer_material") as db:
            rows = await (
                await db.execute(
                    "SELECT node_id,origin,public_key,auth_token,mesh_id "
                    "FROM managed_nodes "
                    "WHERE state!='detached' AND node_id!=? ORDER BY node_id",
                    (self.node_id,),
                )
            ).fetchall()
        return [
            {
                "node_id": row[0],
                "origin": row[1],
                "public_key": row[2],
                "auth_token": row[3],
                "mesh_id": row[4],
            }
            for row in rows
            if all((row[1], row[2], row[3]))
        ]

    async def adopt_managed(
        self,
        *,
        mesh_id: str,
        display_name: str,
        nodes: list[dict],
        policy: dict,
        now: str | None = None,
        attach_local_control: bool = False,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        mesh_id = validate_protocol_id(mesh_id, "mesh_id")
        display_name = str(display_name or "").strip()
        if not display_name or len(display_name) > 120:
            raise ValueError("display_name must be 1-120 characters")
        duration = int(policy["duration_seconds"])
        warning = int(policy["warning_after_seconds"])
        alert = int(policy["alert_after_seconds"])
        rearm = int(policy["rearm_after_seconds"])
        legacy = bool(policy["legacy_admission_enabled"])
        if duration <= 0 or not (0 < warning < alert < duration) or rearm <= 0:
            raise ValueError("invalid access policy")
        stamp = now or utc_text()
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in nodes:
            node_id = validate_protocol_id(str(item.get("node_id") or ""), "node_id")
            if node_id in seen:
                raise ValueError("duplicate managed node")
            seen.add(node_id)
            normalized.append(
                {
                    "node_id": node_id,
                    "origin": str(item.get("origin") or "").strip() or None,
                    "public_key": str(item.get("public_key") or "").strip() or None,
                    "auth_token": str(item.get("auth_token") or "").strip() or None,
                }
            )
        if self.node_id not in seen:
            normalized.append(
                {
                    "node_id": self.node_id,
                    "origin": None,
                    "public_key": None,
                    "auth_token": None,
                }
            )

        async with self._connect("fleet_control_managed_adopt") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                topology, trust, policy_revision = await self._control_revisions(db)
                existing_mesh = await (
                    await db.execute(
                        "SELECT active FROM managed_meshes WHERE mesh_id=?",
                        (mesh_id,),
                    )
                ).fetchone()
                if existing_mesh is not None and bool(existing_mesh[0]):
                    raise FleetControlError("mesh_already_exists")
                topology = await self._bump_revision(db, "topology", stamp)
                if existing_mesh is None:
                    await db.execute(
                        "INSERT INTO managed_meshes("
                        "mesh_id,display_name,active,created_at,updated_at"
                        ") VALUES(?,?,1,?,?)",
                        (mesh_id, display_name, stamp, stamp),
                    )
                else:
                    await db.execute(
                        "UPDATE managed_meshes SET display_name=?,active=1,updated_at=? "
                        "WHERE mesh_id=?",
                        (display_name, stamp, mesh_id),
                    )

                first_adoption = await (
                    await db.execute("SELECT 1 FROM access_policy WHERE singleton=1")
                ).fetchone() is None
                if first_adoption:
                    trust = await self._bump_revision(db, "trust", stamp)
                    policy_revision = await self._bump_revision(db, "policy", stamp)
                    await db.execute(
                        "INSERT INTO access_policy("
                        "singleton,duration_seconds,warning_after_seconds,alert_after_seconds,"
                        "rearm_after_seconds,legacy_admission_enabled,revision,updated_at"
                        ") VALUES(1,?,?,?,?,?,?,?)",
                        (duration, warning, alert, rearm, int(legacy), policy_revision, stamp),
                    )

                for item in normalized:
                    existing = await (
                        await db.execute(
                            "SELECT mesh_id,origin,public_key,auth_token,state "
                            "FROM managed_nodes WHERE node_id=?",
                            (item["node_id"],),
                        )
                    ).fetchone()
                    if existing is None:
                        await db.execute(
                            "INSERT INTO managed_nodes("
                            "node_id,origin,public_key,auth_token,mesh_id,state,"
                            "desired_topology_revision,applied_topology_revision,"
                            "desired_trust_revision,applied_trust_revision,"
                            "desired_policy_revision,applied_policy_revision,last_error,updated_at"
                            ") VALUES(?,?,?,?,NULL,'active',?,0,?,0,?,0,NULL,?)",
                            (
                                item["node_id"],
                                item["origin"],
                                item["public_key"],
                                item["auth_token"],
                                topology,
                                trust,
                                policy_revision,
                                stamp,
                            ),
                        )
                    else:
                        await db.execute(
                            "UPDATE managed_nodes SET "
                            "origin=COALESCE(?,origin),public_key=COALESCE(?,public_key),"
                            "auth_token=COALESCE(?,auth_token),"
                            "state=CASE WHEN state='detached' THEN 'active' ELSE state END,"
                            "desired_topology_revision=?,desired_trust_revision=?,"
                            "desired_policy_revision=?,last_error=NULL,updated_at=? "
                            "WHERE node_id=?",
                            (
                                item["origin"],
                                item["public_key"],
                                item["auth_token"],
                                topology,
                                trust,
                                policy_revision,
                                stamp,
                                item["node_id"],
                            ),
                        )
                if attach_local_control:
                    await db.execute(
                        "UPDATE managed_nodes SET mesh_id=?,state='active',"
                        "desired_topology_revision=?,last_error=NULL,updated_at=? "
                        "WHERE node_id=?",
                        (mesh_id, topology, stamp, self.node_id),
                    )
                await db.execute(
                    "UPDATE managed_nodes SET desired_topology_revision=? "
                    "WHERE state!='detached'",
                    (topology,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def delete_managed_mesh(
        self,
        mesh_id: str,
        *,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        mesh_id = validate_protocol_id(mesh_id, "mesh_id")
        stamp = utc_text()
        async with self._connect("fleet_control_mesh_delete") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                mesh = await (
                    await db.execute(
                        "SELECT active FROM managed_meshes WHERE mesh_id=?",
                        (mesh_id,),
                    )
                ).fetchone()
                if mesh is None or not bool(mesh[0]):
                    raise FleetControlError("managed_mesh_not_found")
                topology, trust, _ = await self._control_revisions(db)
                if expected_topology_revision is not None and topology != int(
                    expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                topology = await self._bump_revision(db, "topology", stamp)
                trust = await self._bump_revision(db, "trust", stamp)
                await db.execute(
                    "UPDATE managed_meshes SET active=0,updated_at=? WHERE mesh_id=?",
                    (stamp, mesh_id),
                )
                await db.execute(
                    "UPDATE managed_nodes SET mesh_id=NULL,"
                    "desired_topology_revision=?,desired_trust_revision=?,"
                    "last_error=NULL,updated_at=? WHERE mesh_id=? AND state!='detached'",
                    (topology, trust, stamp, mesh_id),
                )
                await db.execute(
                    "UPDATE managed_nodes SET desired_topology_revision=?,"
                    "desired_trust_revision=? WHERE state!='detached'",
                    (topology, trust),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def rename_managed_mesh(
        self,
        mesh_id: str,
        display_name: str,
        *,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        mesh_id = validate_protocol_id(mesh_id, "mesh_id")
        display_name = str(display_name or "").strip()
        if not display_name or len(display_name) > 120:
            raise ValueError("display_name must be 1-120 characters")
        stamp = utc_text()
        async with self._connect("fleet_control_mesh_rename") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                mesh = await (
                    await db.execute(
                        "SELECT active FROM managed_meshes WHERE mesh_id=?",
                        (mesh_id,),
                    )
                ).fetchone()
                if mesh is None or not bool(mesh[0]):
                    raise FleetControlError("managed_mesh_not_found")
                topology, _, _ = await self._control_revisions(db)
                if expected_topology_revision is not None and topology != int(
                    expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                topology = await self._bump_revision(db, "topology", stamp)
                await db.execute(
                    "UPDATE managed_meshes SET display_name=?,updated_at=? WHERE mesh_id=?",
                    (display_name, stamp, mesh_id),
                )
                await db.execute(
                    "UPDATE managed_nodes SET desired_topology_revision=?,updated_at=? "
                    "WHERE state!='detached'",
                    (topology, stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def upsert_managed_node(
        self,
        *,
        node_id: str,
        mesh_id: str,
        origin: str | None,
        public_key: str | None,
        auth_token: str | None,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        node_id = validate_protocol_id(node_id, "node_id")
        mesh_id = validate_protocol_id(mesh_id, "mesh_id")
        origin = str(origin or "").strip() or None
        public_key = str(public_key or "").strip() or None
        auth_token = str(auth_token or "").strip() or None
        stamp = utc_text()
        async with self._connect("fleet_control_node_upsert") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                mesh = await (
                    await db.execute(
                        "SELECT active FROM managed_meshes WHERE mesh_id=?",
                        (mesh_id,),
                    )
                ).fetchone()
                if mesh is None or not bool(mesh[0]):
                    raise FleetControlError("managed_mesh_not_found")
                topology, trust, policy_revision = await self._control_revisions(db)
                if expected_topology_revision is not None and topology != int(
                    expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                existing = await (
                    await db.execute(
                        "SELECT origin,public_key,auth_token,mesh_id,state "
                        "FROM managed_nodes WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if existing is not None and existing[3] and existing[3] != mesh_id:
                    raise FleetControlError("node_already_in_other_mesh")
                if existing is None and node_id != self.node_id and not all(
                    (origin, public_key, auth_token)
                ):
                    raise ValueError("managed peer requires origin, public_key and auth_token")
                topology_changed = (
                    existing is None
                    or existing[3] != mesh_id
                    or existing[4] == "detached"
                    or (origin is not None and existing[0] != origin)
                )
                trust_changed = (
                    existing is None
                    or (public_key is not None and existing[1] != public_key)
                    or (auth_token is not None and existing[2] != auth_token)
                )
                if topology_changed:
                    topology = await self._bump_revision(db, "topology", stamp)
                if trust_changed:
                    trust = await self._bump_revision(db, "trust", stamp)
                await db.execute(
                    "INSERT INTO managed_nodes("
                    "node_id,origin,public_key,auth_token,mesh_id,state,"
                    "desired_topology_revision,applied_topology_revision,"
                    "desired_trust_revision,applied_trust_revision,"
                    "desired_policy_revision,applied_policy_revision,last_error,updated_at"
                    ") VALUES(?,?,?,?,?,'active',?,0,?,0,?,0,NULL,?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "origin=COALESCE(excluded.origin,managed_nodes.origin),"
                    "public_key=COALESCE(excluded.public_key,managed_nodes.public_key),"
                    "auth_token=COALESCE(excluded.auth_token,managed_nodes.auth_token),"
                    "mesh_id=excluded.mesh_id,state='active',"
                    "desired_topology_revision=excluded.desired_topology_revision,"
                    "desired_trust_revision=excluded.desired_trust_revision,"
                    "desired_policy_revision=excluded.desired_policy_revision,"
                    "last_error=NULL,updated_at=excluded.updated_at",
                    (
                        node_id,
                        origin,
                        public_key,
                        auth_token,
                        mesh_id,
                        topology,
                        trust,
                        policy_revision,
                        stamp,
                    ),
                )
                if topology_changed:
                    await db.execute(
                        "UPDATE managed_nodes SET desired_topology_revision=? "
                        "WHERE state!='detached'",
                        (topology,),
                    )
                if trust_changed or topology_changed:
                    if topology_changed and not trust_changed:
                        trust = await self._bump_revision(db, "trust", stamp)
                    await db.execute(
                        "UPDATE managed_nodes SET desired_trust_revision=? "
                        "WHERE state!='detached'",
                        (trust,),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def release_managed_node(self, node_id: str, *, now: str | None = None) -> dict:
        """Release one standalone/member node from this control domain.

        Unlike detach_managed_node, this marks the node detached from the authority
        itself. Trust material is retained so a later explicit rejoin can reactivate
        the same identity without repairing server files.
        """
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        node_id = validate_protocol_id(node_id, "node_id")
        stamp = now or utc_text()
        async with self._connect("fleet_control_node_release") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT state FROM managed_nodes WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if row is None:
                    raise FleetControlError("managed_node_not_found")
                if row[0] == "detached":
                    await db.commit()
                    return await self.control_state(include_secrets=False)

                topology, trust, _ = await self._control_revisions(db)
                topology = await self._bump_revision(db, "topology", stamp)
                trust = await self._bump_revision(db, "trust", stamp)
                await db.execute(
                    "UPDATE managed_nodes SET mesh_id=NULL,state='detached',"
                    "desired_topology_revision=?,applied_topology_revision=?,"
                    "desired_trust_revision=?,applied_trust_revision=?,"
                    "last_error=NULL,updated_at=? WHERE node_id=?",
                    (topology, topology, trust, trust, stamp, node_id),
                )
                await db.execute(
                    "UPDATE managed_nodes SET desired_topology_revision=?,"
                    "desired_trust_revision=? WHERE state!='detached'",
                    (topology, trust),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def detach_managed_node(
        self, node_id: str, *, expected_topology_revision: int | None = None
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        node_id = validate_protocol_id(node_id, "node_id")
        stamp = utc_text()
        async with self._connect("fleet_control_node_detach") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT mesh_id,state FROM managed_nodes WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if row is None or row[1] == "detached":
                    raise FleetControlError("managed_node_not_found")
                topology, trust, _ = await self._control_revisions(db)
                if expected_topology_revision is not None and topology != int(
                    expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                if row[0] is not None:
                    topology = await self._bump_revision(db, "topology", stamp)
                    trust = await self._bump_revision(db, "trust", stamp)
                    await db.execute(
                        "UPDATE managed_nodes SET mesh_id=NULL,state=?,"
                        "desired_topology_revision=?,desired_trust_revision=?,"
                        "last_error=NULL,updated_at=? WHERE node_id=?",
                        (
                            "active" if node_id == self.node_id else "draining",
                            topology,
                            trust,
                            stamp,
                            node_id,
                        ),
                    )
                    await db.execute(
                        "UPDATE managed_nodes SET desired_topology_revision=?,"
                        "desired_trust_revision=? WHERE state!='detached'",
                        (topology, trust),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def move_managed_node(
        self,
        node_id: str,
        target_mesh_id: str,
        *,
        expected_topology_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        node_id = validate_protocol_id(node_id, "node_id")
        target_mesh_id = validate_protocol_id(target_mesh_id, "mesh_id")
        stamp = utc_text()
        async with self._connect("fleet_control_node_move") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                mesh = await (
                    await db.execute(
                        "SELECT active FROM managed_meshes WHERE mesh_id=?",
                        (target_mesh_id,),
                    )
                ).fetchone()
                if mesh is None or not bool(mesh[0]):
                    raise FleetControlError("managed_mesh_not_found")
                row = await (
                    await db.execute(
                        "SELECT mesh_id,state FROM managed_nodes WHERE node_id=?",
                        (node_id,),
                    )
                ).fetchone()
                if row is None or row[1] == "detached":
                    raise FleetControlError("managed_node_not_found")
                topology, trust, _ = await self._control_revisions(db)
                if expected_topology_revision is not None and topology != int(
                    expected_topology_revision
                ):
                    raise FleetControlError("topology_revision_conflict")
                if row[0] != target_mesh_id:
                    topology = await self._bump_revision(db, "topology", stamp)
                    trust = await self._bump_revision(db, "trust", stamp)
                    await db.execute(
                        "UPDATE managed_nodes SET mesh_id=?,state='active',"
                        "desired_topology_revision=?,desired_trust_revision=?,"
                        "last_error=NULL,updated_at=? WHERE node_id=?",
                        (target_mesh_id, topology, trust, stamp, node_id),
                    )
                    await db.execute(
                        "UPDATE managed_nodes SET desired_topology_revision=?,"
                        "desired_trust_revision=? WHERE state!='detached'",
                        (topology, trust),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.control_state(include_secrets=False)

    async def access_policy(self) -> dict | None:
        async with self._connect("fleet_control_access_policy") as db:
            row = await (
                await db.execute(
                    "SELECT duration_seconds,warning_after_seconds,alert_after_seconds,"
                    "rearm_after_seconds,legacy_admission_enabled,revision,updated_at "
                    "FROM access_policy WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "duration_seconds": int(row[0]),
            "warning_after_seconds": int(row[1]),
            "alert_after_seconds": int(row[2]),
            "rearm_after_seconds": int(row[3]),
            "legacy_admission_enabled": bool(row[4]),
            "revision": int(row[5]),
            "updated_at": row[6],
        }

    async def update_access_policy(
        self,
        *,
        duration_seconds: int,
        warning_after_seconds: int,
        alert_after_seconds: int,
        rearm_after_seconds: int,
        legacy_admission_enabled: bool,
        expected_revision: int | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        duration = int(duration_seconds)
        warning = int(warning_after_seconds)
        alert = int(alert_after_seconds)
        rearm = int(rearm_after_seconds)
        if duration <= 0 or not (0 < warning < alert < duration) or rearm <= 0:
            raise ValueError("invalid access policy")
        stamp = utc_text()
        async with self._connect("fleet_control_access_policy_update") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT duration_seconds,warning_after_seconds,alert_after_seconds,"
                        "rearm_after_seconds,legacy_admission_enabled,revision "
                        "FROM access_policy WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    raise FleetControlError("managed_access_policy_missing")
                current_revision = int(row[5])
                if expected_revision is not None and current_revision != int(expected_revision):
                    raise FleetControlError("access_policy_revision_conflict")
                desired = (duration, warning, alert, rearm, int(bool(legacy_admission_enabled)))
                current = tuple(int(value) for value in row[:5])
                if current != desired:
                    revision = await self._bump_revision(db, "policy", stamp)
                    await db.execute(
                        "UPDATE access_policy SET duration_seconds=?,"
                        "warning_after_seconds=?,alert_after_seconds=?,"
                        "rearm_after_seconds=?,legacy_admission_enabled=?,"
                        "revision=?,updated_at=? WHERE singleton=1",
                        (*desired, revision, stamp),
                    )
                    await db.execute(
                        "UPDATE managed_nodes SET desired_policy_revision=?,"
                        "last_error=NULL,updated_at=? WHERE state!='detached'",
                        (revision, stamp),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        result = await self.access_policy()
        if result is None:
            raise FleetControlError("managed_access_policy_missing")
        return result

    async def mark_managed_applied(
        self,
        node_id: str,
        *,
        topology_revision: int | None = None,
        trust_revision: int | None = None,
        policy_revision: int | None = None,
        error: str | None = None,
    ) -> dict:
        node_id = validate_protocol_id(node_id, "node_id")
        stamp = utc_text()
        assignments = ["last_error=?", "updated_at=?"]
        values: list[object] = [str(error)[:1000] if error else None, stamp]
        for column, value in (
            ("applied_topology_revision", topology_revision),
            ("applied_trust_revision", trust_revision),
            ("applied_policy_revision", policy_revision),
        ):
            if value is not None:
                assignments.append(f"{column}=MAX({column}, ?)")
                values.append(int(value))
        values.append(node_id)
        async with self._connect("fleet_control_mark_applied") as db:
            cursor = await db.execute(
                f"UPDATE managed_nodes SET {','.join(assignments)} WHERE node_id=?",
                tuple(values),
            )
            if cursor.rowcount != 1:
                raise FleetControlError("managed_node_not_found")
            await db.commit()
        nodes = await self.managed_nodes(include_detached=True)
        return next(item for item in nodes if item["node_id"] == node_id)

    async def control_state(self, *, include_secrets: bool = False) -> dict:
        mesh = await self.managed_mesh()
        meshes = await self.managed_meshes()
        nodes = await self.managed_nodes(include_detached=True)
        policy = await self.access_policy()
        async with self._connect("fleet_control_state") as db:
            row = await (
                await db.execute(
                    "SELECT routing_revision,topology_revision,trust_revision,"
                    "access_policy_revision,updated_at FROM control_meta WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            raise FleetControlError("fleet_control is not initialized")
        result = {
            "schema_version": self.SCHEMA_VERSION,
            "fleet_id": self.fleet_id,
            "node_id": self.node_id,
            "control_node_id": self.control_node_id,
            "managed": bool(nodes),
            "mesh": mesh,
            "meshes": meshes,
            "nodes": nodes,
            "policy": policy,
            "revisions": {
                "routing": int(row[0]),
                "topology": int(row[1]),
                "trust": int(row[2]),
                "access_policy": int(row[3]),
            },
            "updated_at": row[4],
        }
        if include_secrets:
            result["peer_material"] = await self.managed_peer_material()
        return result

    async def apply_managed_replica(
        self,
        snapshot: dict,
        *,
        bootstrap_tokens: dict[str, str] | None = None,
        now: str | None = None,
    ) -> dict:
        if not bool(snapshot.get("managed")):
            raise FleetControlError("managed_snapshot_required")
        if str(snapshot.get("fleet_id") or "") != self.fleet_id:
            raise FleetControlError("fleet_id_mismatch")
        incoming_control_node_id = validate_protocol_id(
            str(snapshot.get("control_node_id") or ""), "control_node_id"
        )
        authority_changed = incoming_control_node_id != self.control_node_id

        legacy_mesh = snapshot.get("mesh") or {}
        incoming_meshes = snapshot.get("meshes")
        if incoming_meshes is None:
            incoming_meshes = (
                [legacy_mesh]
                if legacy_mesh and bool(legacy_mesh.get("adopted"))
                else []
            )
        normalized_meshes: list[dict] = []
        for item in incoming_meshes:
            mesh_id = validate_protocol_id(str(item.get("mesh_id") or ""), "mesh_id")
            display_name = str(item.get("display_name") or "").strip()
            if not display_name or len(display_name) > 120:
                raise ValueError("invalid mesh display_name")
            normalized_meshes.append(
                {
                    "mesh_id": mesh_id,
                    "display_name": display_name,
                    "adopted_at": item.get("adopted_at") or item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        active_mesh_ids = {item["mesh_id"] for item in normalized_meshes}

        revisions = snapshot.get("revisions") or {}
        topology = int(revisions.get("topology") or 0)
        trust = int(revisions.get("trust") or 0)
        policy_revision = int(revisions.get("access_policy") or 0)
        if min(topology, trust, policy_revision) < 1:
            raise FleetControlError("managed_snapshot_revision_invalid")
        policy = snapshot.get("policy") or {}
        duration = int(policy.get("duration_seconds") or 0)
        warning = int(policy.get("warning_after_seconds") or 0)
        alert = int(policy.get("alert_after_seconds") or 0)
        rearm = int(policy.get("rearm_after_seconds") or 0)
        legacy = bool(policy.get("legacy_admission_enabled"))
        if duration <= 0 or not (0 < warning < alert < duration) or rearm <= 0:
            raise ValueError("invalid access policy")
        tokens = dict(bootstrap_tokens or {})
        stamp = now or utc_text()
        incoming_nodes = snapshot.get("nodes") or []
        normalized_nodes = []
        legacy_mesh_id = (
            validate_protocol_id(str(legacy_mesh.get("mesh_id") or ""), "mesh_id")
            if legacy_mesh and bool(legacy_mesh.get("adopted"))
            else None
        )
        for item in incoming_nodes:
            node_id = validate_protocol_id(str(item.get("node_id") or ""), "node_id")
            state = str(item.get("state") or "active")
            if "mesh_id" in item:
                raw_mesh_id = item.get("mesh_id")
                mesh_id = (
                    validate_protocol_id(str(raw_mesh_id), "mesh_id")
                    if raw_mesh_id is not None
                    else None
                )
            else:
                mesh_id = legacy_mesh_id if legacy_mesh_id and state != "detached" else None
            if mesh_id is not None and mesh_id not in active_mesh_ids:
                raise FleetControlError("managed_node_mesh_missing")
            normalized_nodes.append(
                {
                    "node_id": node_id,
                    "origin": str(item.get("origin") or "").strip() or None,
                    "public_key": str(item.get("public_key") or "").strip() or None,
                    "mesh_id": mesh_id,
                    "state": state,
                    "applied_topology_revision": int(item.get("applied_topology_revision") or 0),
                    "applied_trust_revision": int(item.get("applied_trust_revision") or 0),
                    "applied_policy_revision": int(item.get("applied_policy_revision") or 0),
                }
            )

        async with self._connect("fleet_control_managed_replica_apply") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await self._control_revisions(db)
                if authority_changed:
                    local = await (
                        await db.execute(
                            "SELECT mesh_id,state FROM managed_nodes WHERE node_id=?",
                            (self.node_id,),
                        )
                    ).fetchone()
                    if local is not None and local[1] != "detached" and local[0] is not None:
                        raise FleetControlError("control_node_mismatch")
                    await db.execute(
                        "UPDATE control_meta SET control_node_id=?,updated_at=? WHERE singleton=1",
                        (incoming_control_node_id, stamp),
                    )
                elif topology < current[0] or trust < current[1] or policy_revision < current[2]:
                    raise FleetControlError("managed_snapshot_stale")
                existing_tokens = {
                    row[0]: row[1]
                    for row in await (
                        await db.execute(
                            "SELECT node_id,auth_token FROM managed_nodes "
                            "WHERE auth_token IS NOT NULL"
                        )
                    ).fetchall()
                }
                await db.execute(
                    "UPDATE control_meta SET topology_revision=?,trust_revision=?,"
                    "access_policy_revision=?,updated_at=? WHERE singleton=1",
                    (topology, trust, policy_revision, stamp),
                )

                await db.execute("UPDATE managed_meshes SET active=0,updated_at=?", (stamp,))
                for item in normalized_meshes:
                    await db.execute(
                        "INSERT INTO managed_meshes("
                        "mesh_id,display_name,active,created_at,updated_at"
                        ") VALUES(?,?,1,?,?) "
                        "ON CONFLICT(mesh_id) DO UPDATE SET "
                        "display_name=excluded.display_name,active=1,updated_at=excluded.updated_at",
                        (
                            item["mesh_id"],
                            item["display_name"],
                            item["adopted_at"] or stamp,
                            item["updated_at"] or stamp,
                        ),
                    )

                present = set()
                for item in normalized_nodes:
                    present.add(item["node_id"])
                    token = tokens.get(item["node_id"]) or existing_tokens.get(item["node_id"])
                    await db.execute(
                        "INSERT INTO managed_nodes("
                        "node_id,origin,public_key,auth_token,mesh_id,state,"
                        "desired_topology_revision,applied_topology_revision,"
                        "desired_trust_revision,applied_trust_revision,"
                        "desired_policy_revision,applied_policy_revision,last_error,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(node_id) DO UPDATE SET "
                        "origin=excluded.origin,public_key=excluded.public_key,"
                        "auth_token=COALESCE(excluded.auth_token,managed_nodes.auth_token),"
                        "mesh_id=excluded.mesh_id,state=excluded.state,"
                        "desired_topology_revision=excluded.desired_topology_revision,"
                        "applied_topology_revision=excluded.applied_topology_revision,"
                        "desired_trust_revision=excluded.desired_trust_revision,"
                        "applied_trust_revision=excluded.applied_trust_revision,"
                        "desired_policy_revision=excluded.desired_policy_revision,"
                        "applied_policy_revision=excluded.applied_policy_revision,"
                        "last_error=NULL,updated_at=excluded.updated_at",
                        (
                            item["node_id"],
                            item["origin"],
                            item["public_key"],
                            token,
                            item["mesh_id"],
                            item["state"],
                            topology,
                            min(topology, item["applied_topology_revision"]),
                            trust,
                            min(trust, item["applied_trust_revision"]),
                            policy_revision,
                            min(policy_revision, item["applied_policy_revision"]),
                            None,
                            stamp,
                        ),
                    )
                if present:
                    placeholders = ",".join("?" for _ in present)
                    await db.execute(
                        f"UPDATE managed_nodes SET state='detached',mesh_id=NULL,updated_at=? "
                        f"WHERE node_id NOT IN ({placeholders})",
                        (stamp, *sorted(present)),
                    )

                await db.execute(
                    "INSERT INTO access_policy("
                    "singleton,duration_seconds,warning_after_seconds,alert_after_seconds,"
                    "rearm_after_seconds,legacy_admission_enabled,revision,updated_at"
                    ") VALUES(1,?,?,?,?,?,?,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET "
                    "duration_seconds=excluded.duration_seconds,"
                    "warning_after_seconds=excluded.warning_after_seconds,"
                    "alert_after_seconds=excluded.alert_after_seconds,"
                    "rearm_after_seconds=excluded.rearm_after_seconds,"
                    "legacy_admission_enabled=excluded.legacy_admission_enabled,"
                    "revision=excluded.revision,updated_at=excluded.updated_at",
                    (
                        duration,
                        warning,
                        alert,
                        rearm,
                        int(legacy),
                        policy_revision,
                        stamp,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        if authority_changed:
            self.control_node_id = incoming_control_node_id
        return await self.control_state(include_secrets=False)

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

    async def ensure_projection_topology(
        self,
        owner_node_id: str,
        follower_node_id: str,
        *,
        now: str | None = None,
    ) -> dict:
        owner_node_id = validate_protocol_id(owner_node_id, "projection owner_node_id")
        follower_node_id = validate_protocol_id(follower_node_id, "projection follower_node_id")
        if owner_node_id == follower_node_id:
            raise ValueError("projection owner and follower must differ")
        stamp = now or utc_text()
        async with self._connect("fleet_control_projection_topology_ensure") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT owner_node_id,follower_node_id,projection_epoch,updated_at "
                        "FROM projection_topology WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    await db.execute(
                        "INSERT INTO projection_topology("
                        "singleton,owner_node_id,follower_node_id,projection_epoch,updated_at"
                        ") VALUES(1,?,?,1,?)",
                        (owner_node_id, follower_node_id, stamp),
                    )
                elif (row[0], row[1]) != (owner_node_id, follower_node_id):
                    raise FleetControlError("projection_topology_mismatch")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.projection_topology()

    async def projection_topology(self) -> dict | None:
        async with self._connect("fleet_control_projection_topology_get") as db:
            row = await (
                await db.execute(
                    "SELECT owner_node_id,follower_node_id,projection_epoch,updated_at "
                    "FROM projection_topology WHERE singleton=1"
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_node_id": row[0],
            "follower_node_id": row[1],
            "projection_epoch": int(row[2]),
            "updated_at": row[3],
        }

    async def promote_projection(
        self,
        *,
        expected_epoch: int,
        now: str | None = None,
    ) -> dict:
        if self.node_id != self.control_node_id:
            raise FleetControlError("control_authority_required")
        stamp = now or utc_text()
        async with self._connect("fleet_control_projection_promote") as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        "SELECT owner_node_id,follower_node_id,projection_epoch "
                        "FROM projection_topology WHERE singleton=1"
                    )
                ).fetchone()
                if row is None:
                    raise FleetControlError("projection_topology_missing")
                if int(row[2]) != int(expected_epoch):
                    raise FleetControlError("projection_epoch_conflict")
                await db.execute(
                    "UPDATE projection_topology SET owner_node_id=?,follower_node_id=?,"
                    "projection_epoch=projection_epoch+1,updated_at=? WHERE singleton=1",
                    (row[1], row[0], stamp),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.projection_topology()

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

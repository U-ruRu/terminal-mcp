"""The current Mesh registry, its local transition fence and primary handoff.

Topology owns authorization and the distributed commit. These storage ports own
one SQLite transaction: a fenced node cannot accept a new slot or attachment, and
leaving a namespace only clears that node's local copy of its Access state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from contextlib import nullcontext
from contextvars import ContextVar

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.access_mesh_scope import assert_registry_scope

_RUNTIME_IMPORT_CONNECTION = ContextVar("mesh_runtime_import_connection", default=None)

REGISTRY_TABLES = (
    "access_mesh_slot_replicas",
    "access_mesh_event_log",
    "access_mesh_slot_catalog",
    "access_mesh_number_claims",
    "access_mesh_number_starts",
    "access_mesh_number_ends",
    "access_mesh_number_incidents",
    "access_mesh_operator_defaults",
    "access_mesh_issuer_bindings",
    "access_mesh_issuer_receipts",
)
# Command results and attribution remain local application data. Their journal
# is deliberately retained when the local Access namespace is cleared.
LOCAL_RETAINED_TABLES = {
    "access_mesh_registry_scope",
    "access_mesh_command_journal",
    "access_mesh_migration_receipts",
}


class AccessRegistryStore:
    def initialize_registry(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_registry_scope (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                namespace_id TEXT NOT NULL, authority_id TEXT NOT NULL,
                mesh_id TEXT, transition_id TEXT,
                upgrade_pending INTEGER NOT NULL DEFAULT 0,
                receipts_migrated INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_migration_receipts (
                name TEXT PRIMARY KEY, namespace_id TEXT NOT NULL,
                completed_at TEXT NOT NULL, details_json TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_registry_upgrade_export (
                namespace_id TEXT NOT NULL, table_name TEXT NOT NULL,
                row_offset INTEGER NOT NULL, row_json TEXT NOT NULL,
                PRIMARY KEY(namespace_id,table_name,row_offset)
            )""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(access_mesh_registry_scope)")}
            for column in ("upgrade_pending", "receipts_migrated"):
                if column not in columns:
                    db.execute(
                        "ALTER TABLE access_mesh_registry_scope "
                        f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
                    if column == "upgrade_pending":
                        db.execute("UPDATE access_mesh_registry_scope SET upgrade_pending=1")
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_registry_scope "
                "(singleton,namespace_id,authority_id,mesh_id,transition_id,upgrade_pending) "
                "VALUES(1,?,?,NULL,NULL,?)",
                (
                    "standalone-" + secrets.token_hex(16),
                    self.local_node_id,
                    int(getattr(self, "_pre_unified_registry", False)),
                ),
            )
        self._load_registry_scope()

    def _load_registry_scope(self):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()
        self.namespace_id = row["namespace_id"]
        self.registry_authority_id = row["authority_id"]
        self.registry_mesh_id = row["mesh_id"]

    def assert_registry_ready(self, db=None):
        # Core-only projections have no native topology store.
        with self._connect() if db is None else nullcontext(db) as connection:
            assert_registry_scope(connection)

    def assert_registry_writer(self, db, authenticated_peer_id):
        row = db.execute(
            "SELECT mesh_id,authority_id FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()
        if row and row[0] and row[1] != authenticated_peer_id:
            if (
                authenticated_peer_id == self.local_node_id
                and _RUNTIME_IMPORT_CONNECTION.get() is db
            ):
                return
            raise AccessMeshError("access_mesh_untrusted_issuer")

    def import_runtime_event(self, event, *, _connection, issued_kind=None, mutation_number=None):
        """Convert one proven local registration in the one-time offline transaction.

        A member can stage its old local identities for the primary's union. This
        port is unavailable after migration completion and does not authorize
        ordinary member issuance, peer events or a topology transition.
        """
        db = _connection
        if not db.in_transaction:
            raise AccessMeshError("mesh_runtime_import_invalid")
        self.assert_registry_ready(db)
        scope = db.execute(
            "SELECT upgrade_pending,transition_id FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()
        receipt_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unified_runtime_migration'"
        ).fetchone()
        receipt = (
            db.execute("SELECT 1 FROM unified_runtime_migration WHERE singleton=1").fetchone()
            if receipt_table
            else None
        )
        if not scope["upgrade_pending"] or scope["transition_id"] is not None or receipt:
            raise AccessMeshError("mesh_runtime_import_invalid")
        agent_digest = hashlib.sha256(event.logical_agent_id.encode()).hexdigest()[:32]
        expected_slot = "upgrade-" + agent_digest
        spec = {
            "SlotIssued": ("runtime-upgrade-", {1}),
            "SessionUpdated": ("runtime-upgrade-session-", {2}),
            "SlotSuspended": ("runtime-upgrade-suspend-", {2, 3}),
        }.get(event.kind)
        if (
            spec is None
            or event.issuer_id != self.local_node_id
            or event.slot_id != expected_slot
            or event.event_id != spec[0] + expected_slot
            or event.revision not in spec[1]
            or (event.kind == "SlotIssued" and issued_kind not in {"temporary", "persistent"})
            or (event.kind != "SlotIssued" and (issued_kind or mutation_number))
        ):
            raise AccessMeshError("mesh_runtime_import_invalid")
        owner = db.execute(
            "SELECT 1 FROM logical_agents a JOIN access_mesh_native_owners n "
            "USING(logical_agent_id) WHERE a.logical_agent_id=? AND a.authority_node_id=? "
            "AND a.state NOT IN ('deleted','deleting') AND n.issuer_id=? AND n.slot_id=?",
            (event.logical_agent_id, self.local_node_id, self.local_node_id, expected_slot),
        ).fetchone()
        if owner is None:
            raise AccessMeshError("mesh_runtime_import_invalid")
        token = _RUNTIME_IMPORT_CONNECTION.set(db)
        try:
            return self.apply_event(
                event,
                authenticated_peer_id=self.local_node_id,
                issued_kind=issued_kind,
                mutation_number=mutation_number,
                _connection=db,
            )
        finally:
            _RUNTIME_IMPORT_CONNECTION.reset(token)

    def registry_verification_keys(self):
        keys = tuple(
            dict.fromkeys(
                (
                    self._proof_key,
                    *([self._managed_proof_key] if self._managed_proof_key else []),
                    *self._local_proof_history,
                )
            )
        )
        return [base64.b64encode(key).decode() for key in keys]

    def registry_issuers(self):
        with self._connect() as db:
            stored = {
                row[0]
                for row in db.execute("SELECT DISTINCT issuer_id FROM access_mesh_slot_replicas")
            }
        return frozenset(stored | self.trusted_issuers)

    def registry_code_slot(self, code):
        found = [
            slot
            for issuer in self.registry_issuers()
            if (slot := self.code_slot(issuer, code)) is not None
        ]
        if len(found) > 1:
            raise AccessMeshError("access_mesh_ambiguous_code")
        return found[0] if found else None

    @property
    def registry_upgrade_pending(self):
        with self._connect() as db:
            return bool(
                db.execute(
                    "SELECT upgrade_pending FROM access_mesh_registry_scope WHERE singleton=1"
                ).fetchone()[0]
            )

    def assert_registry_available(self):
        self.assert_registry_ready()
        if self.registry_mesh_id and self.registry_upgrade_pending:
            raise AccessMeshError("mesh_registry_migrating")

    def install_registry_scope(self, *, mesh_id, authority_id):
        """Install verified membership; the first upgrade adopts existing data.

        Subsequent namespace changes require the durable transition port. A
        normal topology refresh or primary replacement preserves every slot.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()
            if row["mesh_id"] is not None and mesh_id != row["mesh_id"]:
                raise AccessMeshError("mesh_namespace_transition_required")
            namespace = mesh_id or row["namespace_id"]
            db.execute(
                "UPDATE access_mesh_registry_scope SET namespace_id=?,mesh_id=?,authority_id=? "
                "WHERE singleton=1",
                (namespace, mesh_id, authority_id),
            )
        self._load_registry_scope()

    def local_active_work_summary(self, namespace_id=None, *, _connection=None):
        if namespace_id is not None and namespace_id != self.namespace_id:
            raise AccessMeshError("mesh_namespace_mismatch")
        with self._connect() if _connection is None else nullcontext(_connection) as db:
            slots = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE state='active'"
            ).fetchall()
            now = self.clock()
            active_slots = sum(
                self.merged_cycle(self._snapshot(row), now)["state"]
                in {"active", "warning", "draining"}
                for row in slots
            )
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            sessions = (
                db.execute(
                    "SELECT COUNT(*) FROM logical_agent_work_sessions "
                    "WHERE state='active' AND hard_expires_at>?",
                    (now.isoformat(),),
                ).fetchone()[0]
                if "logical_agent_work_sessions" in tables
                else 0
            )
            cleanup = db.execute(
                "SELECT COUNT(*) FROM access_mesh_cleanup_jobs WHERE completed_at IS NULL"
            ).fetchone()[0]
            commands = (
                db.execute(
                    "SELECT COUNT(*) FROM commands "
                    "WHERE status IN ('queued','running','cancelling')"
                ).fetchone()[0]
                if "commands" in tables
                else 0
            )
        return {
            "namespace_id": self.namespace_id,
            "active_slots": active_slots,
            "active_sessions": sessions,
            "active_commands": commands,
            "cleanup_pending": cleanup,
        }

    def begin_topology_transition(self, operation_id):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
            raise AccessMeshError("invalid_request")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT transition_id FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()[0]
            if current not in {None, operation_id}:
                raise AccessMeshError("mesh_transition_pending")
            if current == operation_id:
                return
            if self.registry_mesh_id and self.registry_upgrade_pending:
                raise AccessMeshError("mesh_registry_migrating")
            summary = self.local_active_work_summary(_connection=db)
            if any(
                summary[k]
                for k in ("active_slots", "active_sessions", "active_commands", "cleanup_pending")
            ):
                raise AccessMeshError("mesh_active_work")
            db.execute(
                "UPDATE access_mesh_registry_scope SET transition_id=? WHERE singleton=1",
                (operation_id,),
            )

    def abort_topology_transition(self, operation_id):
        with self._connect() as db:
            db.execute(
                "UPDATE access_mesh_registry_scope SET transition_id=NULL "
                "WHERE singleton=1 AND transition_id=?",
                (operation_id,),
            )

    def _retire_native_authority_tx(self, db, tables):
        if "logical_agents" not in tables:
            return
        namespace = db.execute(
            "SELECT namespace_id FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()[0]
        stamp = self.clock().isoformat()
        owned = "SELECT logical_agent_id FROM access_mesh_native_owners"
        db.execute("""CREATE TABLE IF NOT EXISTS mesh_identity_history (
            namespace_id TEXT NOT NULL, logical_agent_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
            PRIMARY KEY(namespace_id,logical_agent_id)
        )""")
        db.execute(
            "INSERT OR IGNORE INTO mesh_identity_history "
            "SELECT ?,logical_agent_id,issuer_id,slot_id FROM access_mesh_native_owners",
            (namespace,),
        )
        db.execute(
            "UPDATE logical_agents SET state='deleted',deleted_at=COALESCE(deleted_at,?),"
            "updated_at=?,tombstone_reason='mesh_namespace_departed' "
            f"WHERE logical_agent_id IN ({owned})",
            (stamp, stamp),
        )
        if "logical_agent_work_sessions" in tables:
            db.execute(
                "UPDATE logical_agent_work_sessions SET state='ended',"
                "ended_at=COALESCE(ended_at,?),"
                "end_reason='mesh_namespace_departed' "
                f"WHERE logical_agent_id IN ({owned}) AND state IN ('active','stopping')",
                (stamp,),
            )
        for name, column in (
            ("logical_agent_node_attachments", "revoked_at"),
            ("logical_agent_arms", "revoked_at"),
            ("logical_agent_rearms", "cancelled_at"),
            ("persistent_command_permits", "revoked_at"),
        ):
            if name in tables:
                db.execute(
                    f'UPDATE "{name}" SET {column}=COALESCE({column},?) '
                    f"WHERE logical_agent_id IN ({owned})",
                    (stamp,),
                )
        if "logical_agent_selectors" in tables:
            db.execute(
                "UPDATE logical_agent_selectors SET retired_at=COALESCE(retired_at,?),"
                f"tombstoned_at=COALESCE(tombstoned_at,?) WHERE logical_agent_id IN ({owned})",
                (stamp, stamp),
            )
        if "work_claims" in tables:
            db.execute(
                "UPDATE work_claims SET released_at=? WHERE released_at IS NULL "
                f"AND owner_kind='logical_agent' AND owner_id IN ({owned})",
                (stamp,),
            )
        for name in (
            "work_claim_leases",
            "persistent_attachment_presence",
            "mcp_event_local_attached",
        ):
            if name in tables:
                db.execute(f'DELETE FROM "{name}" WHERE logical_agent_id IN ({owned})')
        if "mcp_event_subscriptions" in tables:
            bindings = (
                f" OR id IN (SELECT subscription_id FROM mcp_event_message_bindings "
                f"WHERE recipient_agent_id IN ({owned}))"
                if "mcp_event_message_bindings" in tables
                else ""
            )
            subscriptions = (
                f"SELECT id FROM mcp_event_subscriptions WHERE subscriber_agent_id IN ({owned})"
                + bindings
            )
            db.execute("CREATE TEMP TABLE departing_subscriptions(id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO departing_subscriptions " + subscriptions)
            for name in ("mcp_event_deliveries", "mcp_event_message_bindings"):
                if name in tables:
                    db.execute(
                        f'DELETE FROM "{name}" WHERE subscription_id IN '
                        "(SELECT id FROM departing_subscriptions)"
                    )
            db.execute(
                "DELETE FROM mcp_event_subscriptions WHERE id IN "
                "(SELECT id FROM departing_subscriptions)"
            )

    def _clear_local_namespace_tx(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._retire_native_authority_tx(db, tables)
        if "access_mesh_messages" in tables:
            db.execute("""CREATE TABLE IF NOT EXISTS mesh_message_history (
                namespace_id TEXT NOT NULL, message_hash TEXT NOT NULL,
                content_json TEXT NOT NULL, PRIMARY KEY(namespace_id,message_hash)
            )""")
            namespace = db.execute(
                "SELECT namespace_id FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()[0]
            content_fields = {
                "sender_name",
                "target",
                "text",
                "created_at",
                "namespace",
                "task_id",
                "reply_to",
            }
            for row in db.execute(
                "SELECT message_hash,wire_json FROM access_mesh_messages"
            ).fetchall():
                wire = json.loads(row["wire_json"])
                content = {key: wire[key] for key in content_fields if key in wire}
                db.execute(
                    "INSERT OR IGNORE INTO mesh_message_history VALUES(?,?,?)",
                    (namespace, row["message_hash"], json.dumps(content, sort_keys=True)),
                )
                for name in ("coordination_message_recipients", "coordination_messages"):
                    if name in tables:
                        db.execute(
                            f'DELETE FROM "{name}" WHERE message_hash=?', (row["message_hash"],)
                        )
        # Delete FK dependants before the slot projection. This is a local
        # erasure, never SlotDeleted/SessionEnded or an outbound revoke event.
        removable = sorted(
            t for t in tables if t.startswith("access_mesh_") and t not in LOCAL_RETAINED_TABLES
        )
        if "access_mesh_attachments" in removable:
            removable.remove("access_mesh_attachments")
            removable.insert(0, "access_mesh_attachments")
        for table in removable:
            db.execute(f'DELETE FROM "{table}"')

    def finish_topology_transition(
        self, operation_id, *, namespace_id, authority_id, clear_local=True, mesh_id=None
    ):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()
            if (
                row["transition_id"] is None
                and row["namespace_id"] == namespace_id
                and row["authority_id"] == authority_id
            ):
                return
            if row["transition_id"] != operation_id:
                raise AccessMeshError("mesh_transition_pending")
            if clear_local:
                self._clear_local_namespace_tx(db)
            db.execute(
                "UPDATE access_mesh_registry_scope SET namespace_id=?,authority_id=?,"
                "mesh_id=?,transition_id=NULL,upgrade_pending=0 "
                "WHERE singleton=1",
                (namespace_id, authority_id, mesh_id),
            )
        if clear_local:
            self.clear_managed_proof()
            self._local_proof_history = ()
        self._load_registry_scope()

    def clear_local_namespace(self, namespace_id):
        """A topology-only local cleanup while the persisted fence is held."""
        if namespace_id != self.namespace_id:
            raise AccessMeshError("mesh_namespace_mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT transition_id FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()
            if not row[0]:
                raise AccessMeshError("mesh_transition_required")
            self._clear_local_namespace_tx(db)
        self.clear_managed_proof()
        self._local_proof_history = ()

    def registry_slot(self, slot_id):
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE slot_id=?", (slot_id,)
            ).fetchall()
        if len(rows) > 1:
            raise AccessMeshError("access_mesh_identity_conflict")
        return self._snapshot(rows[0]) if rows else None

    def registry_issuer_trusted(self, issuer_id):
        if issuer_id in self.trusted_issuers:
            return True
        if not self.registry_mesh_id:
            return False
        # Issuer is immutable provenance. Removing its former server from this
        # Mesh never revokes a slot kept in the current authoritative registry.
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? LIMIT 1",
                    (issuer_id,),
                ).fetchone()
                is not None
            )

    def registry_slot_wire(self, number):
        self.assert_registry_available()
        slot = self.registry_code_slot(number)
        if slot is None:
            raise AccessMeshError("invalid_session_number")
        issuer, slot_id = slot.issuer_id, slot.slot_id
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (issuer, slot_id),
            ).fetchone()
            if row is None:
                raise AccessMeshError("invalid_session_number")
            result = {
                "slot": self.snapshot_wire(row),
                "claim": dict(
                    db.execute(
                        "SELECT * FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                        (issuer, slot_id),
                    ).fetchone()
                ),
            }
            for key, table in (
                ("starts", "access_mesh_number_starts"),
                ("ends", "access_mesh_number_ends"),
            ):
                result[key] = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT * FROM {table} WHERE issuer_id=? AND slot_id=?", (issuer, slot_id)
                    )
                ]
        return {
            "ok": True,
            "namespace_id": self.namespace_id,
            **result,
            "verification_keys": self.registry_verification_keys(),
        }

    def install_registry_verification_keys(self, encoded):
        """Install current-namespace keys received from the authenticated primary."""
        try:
            keys = tuple(base64.b64decode(value, validate=True) for value in encoded)
            if not keys or any(len(key) < 16 for key in keys):
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise AccessMeshError("access_mesh_invalid_snapshot") from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            for key in keys:
                nonce = secrets.token_bytes(12)
                associated = f"{self.namespace_id}:{self.registry_authority_id}".encode()
                sealed = nonce + self._archive_cipher().encrypt(nonce, key, associated)
                db.execute(
                    "INSERT OR IGNORE INTO access_mesh_local_proof_history VALUES(?,?,?,?)",
                    (
                        self.namespace_id,
                        self.registry_authority_id,
                        hashlib.sha256(key).hexdigest(),
                        sealed,
                    ),
                )
        self._load_local_proof_history()

    def export_registry(self):
        """Exact registry snapshot for paired topology handoff; no local work."""
        with self._connect() as db:
            db.execute("BEGIN")
            tables = {
                name: [dict(r) for r in db.execute(f'SELECT * FROM "{name}"')]
                for name in REGISTRY_TABLES
            }
            for row in tables["access_mesh_issuer_receipts"]:
                sealed = base64.b64decode(row["result_json"])
                row["result_json"] = (
                    self._receipt_cipher()
                    .decrypt(sealed[:12], sealed[12:], row["request_key"].encode())
                    .decode()
                )
        return {
            "version": 1,
            "namespace_id": self.namespace_id,
            "mesh_id": self.registry_mesh_id,
            "tables": tables,
            "verification_keys": self.registry_verification_keys(),
        }

    def import_registry(self, snapshot, *, expected_namespace_id=None):
        """Restore a frozen primary's registry, re-sealing local-only receipts.

        The caller must authenticate the paired topology operation. Import is
        never reachable through an ordinary peer slot-replication endpoint.
        """
        namespace = expected_namespace_id or self.namespace_id
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("version") != 1
            or snapshot.get("namespace_id") != namespace
        ):
            raise AccessMeshError("mesh_namespace_mismatch")
        if set(snapshot.get("tables", {})) != set(REGISTRY_TABLES):
            raise AccessMeshError("access_mesh_invalid_snapshot")
        proofs = tuple(
            base64.b64decode(value, validate=True)
            for value in snapshot.get("verification_keys", [])
        )
        if not proofs or any(len(k) < 16 for k in proofs):
            raise AccessMeshError("access_mesh_invalid_snapshot")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attachments = [dict(row) for row in db.execute("SELECT * FROM access_mesh_attachments")]
            db.execute("DELETE FROM access_mesh_attachments")
            for table in reversed(REGISTRY_TABLES):
                db.execute(f'DELETE FROM "{table}"')
            for table in REGISTRY_TABLES:
                columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                for original in snapshot["tables"][table]:
                    row = dict(original)
                    if set(row) != set(columns):
                        raise AccessMeshError("access_mesh_invalid_snapshot")
                    if table == "access_mesh_issuer_receipts":
                        nonce = secrets.token_bytes(12)
                        sealed = nonce + self._receipt_cipher().encrypt(
                            nonce, row["result_json"].encode(), row["request_key"].encode()
                        )
                        row["result_json"] = base64.b64encode(sealed).decode()
                    verb = "INSERT OR REPLACE" if table == "access_mesh_slot_catalog" else "INSERT"
                    placeholders = ",".join("?" for _ in columns)
                    db.execute(
                        f'{verb} INTO "{table}" ({",".join(columns)}) VALUES ({placeholders})',
                        [row[k] for k in columns],
                    )
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='logical_agents'").fetchone():
                for row in db.execute("SELECT * FROM access_mesh_slot_replicas").fetchall():
                    self._native_slot(db, self._snapshot(row), self.clock().isoformat())
            for row in attachments:
                exists = db.execute(
                    "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                    (row["issuer_id"], row["slot_id"]),
                ).fetchone()
                if not exists:
                    raise AccessMeshError("access_mesh_registry_handoff_incomplete")
                db.execute(
                    "INSERT INTO access_mesh_attachments "
                    "(connection_key,issuer_id,slot_id,logical_agent_id) VALUES(?,?,?,?)",
                    tuple(
                        row[k]
                        for k in ("connection_key", "issuer_id", "slot_id", "logical_agent_id")
                    ),
                )
            # Preserve the old group's verification keys on every successor;
            # these keys belong only to this namespace and are erased on Move.
            for key in proofs:
                authority = self.registry_authority_id
                nonce = secrets.token_bytes(12)
                associated = f"{namespace}:{authority}".encode()
                sealed = nonce + self._archive_cipher().encrypt(nonce, key, associated)
                db.execute(
                    "INSERT OR IGNORE INTO access_mesh_local_proof_history VALUES(?,?,?,?)",
                    (namespace, authority, hashlib.sha256(key).hexdigest(), sealed),
                )
        self._load_local_proof_history()

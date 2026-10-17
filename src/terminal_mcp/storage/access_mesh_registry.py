"""The current Mesh registry, its local transition fence and primary handoff.

Topology owns authorization and the distributed commit. These storage ports own
one SQLite transaction: a fenced node cannot accept a new slot or attachment, and
leaving a namespace only clears that node's local copy of its Access state.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import nullcontext

from terminal_mcp.core.access_mesh_grants import AccessMeshError

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
LOCAL_RETAINED_TABLES = {"access_mesh_registry_scope", "access_mesh_command_journal"}


class AccessRegistryStore:
    def initialize_registry(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_registry_scope (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                namespace_id TEXT NOT NULL, authority_id TEXT NOT NULL,
                mesh_id TEXT, transition_id TEXT
            )""")
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_registry_scope VALUES(1,?,?,NULL,NULL)",
                ("standalone-" + secrets.token_hex(16), self.local_node_id),
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
            row = connection.execute(
                "SELECT transition_id FROM access_mesh_registry_scope WHERE singleton=1"
            ).fetchone()
            if row and row[0]:
                raise AccessMeshError("mesh_transition_pending")

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

    def _clear_local_namespace_tx(self, db):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
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
                "mesh_id=?,transition_id=NULL "
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
        self.assert_registry_ready()
        winner = self.numbers.winner(number)
        if winner is None:
            raise AccessMeshError("invalid_session_number")
        issuer, slot_id = winner["issuer_id"], winner["slot_id"]
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
        keys = tuple(
            dict.fromkeys((self._managed_proof_key or self._proof_key, *self._local_proof_history))
        )
        return {
            "ok": True,
            "namespace_id": self.namespace_id,
            **result,
            "verification_keys": [base64.b64encode(k).decode() for k in keys],
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
        proofs = list(
            dict.fromkeys((self._managed_proof_key or self._proof_key, *self._local_proof_history))
        )
        return {
            "version": 1,
            "namespace_id": self.namespace_id,
            "mesh_id": self.registry_mesh_id,
            "tables": tables,
            "verification_keys": [base64.b64encode(k).decode() for k in proofs],
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

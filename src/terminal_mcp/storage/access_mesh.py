"""Durable access replicas connected to native command/session storage.

Issuer identity is global. WorkSession authority and every command remain local.
Fencing/claim cleanup is a durable job committed with the triggering transition.
A new cycle waits for cleanup, preventing delayed release of a newer claim.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    LocalAccessMesh,
    SessionSnapshot,
    SlotPolicy,
    SlotSnapshot,
    _parse,
    _text,
    _utc,
)
from terminal_mcp.core.human_names import full_agent_name, number_agent_name
from terminal_mcp.core.session_end_reasons import session_end_reason
from terminal_mcp.storage.access_mesh_registry import AccessRegistryStore

_mutation_receipt: ContextVar[dict | None] = ContextVar("mesh_issuer_receipt", default=None)
_mutation_number: ContextVar[dict | None] = ContextVar("mesh_session_number", default=None)
_mutation_end: ContextVar[dict | None] = ContextVar("mesh_session_end", default=None)
_mutation_start: ContextVar[dict | None] = ContextVar("mesh_session_start", default=None)


@dataclass(frozen=True, slots=True)
class AgentOverviewPage:
    """Private scan positions, kept outside all public agent records."""

    agents: list[dict]
    row_offsets: list[int]
    scanned_until: int
    has_more: bool


def public_name(issuer_id: str, logical_agent_id: str) -> str:
    digest = hashlib.sha256(f"{issuer_id}:{logical_agent_id}".encode()).hexdigest()[:12]
    return f"{issuer_id[:40]}-{digest}"


def local_cycle(slot: SlotSnapshot, now: datetime) -> dict:
    from terminal_mcp.core.access_policy_windows import slot_cycle

    return slot_cycle(slot, now)


class AccessMeshStore(AccessRegistryStore, LocalAccessMesh):
    """Native storage adapter; construct after the parent directory exists."""

    def __init__(
        self,
        *args,
        clock=None,
        upgrade_defaults: SlotPolicy | None = None,
        **kwargs,
    ):
        database_path = args[0] if args else kwargs.get("database_path")
        self._pre_unified_registry = False
        if database_path is not None and Path(database_path).is_file():
            with sqlite3.connect(database_path) as previous:
                tables = {
                    r[0]
                    for r in previous.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                self._pre_unified_registry = (
                    bool(tables & {"access_mesh_slot_replicas", "logical_agents"})
                    and "access_mesh_registry_scope" not in tables
                )
        self._upgrade_defaults = upgrade_defaults
        self.clock = clock or (lambda: datetime.now(UTC))
        self._delivery_notifier = None
        super().__init__(*args, **kwargs)
        # History is encrypted with this node's immutable bootstrap proof. It
        # holds only derived group-verification keys, never Fleet signing keys.
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_local_proof_history(
                  mesh_id TEXT NOT NULL,
                  authority_id TEXT NOT NULL,
                  proof_fingerprint TEXT NOT NULL,
                  sealed BLOB NOT NULL,
                  PRIMARY KEY(mesh_id, authority_id, proof_fingerprint)
                ) WITHOUT ROWID
            """)
        self._load_local_proof_history()
        from terminal_mcp.storage.access_mesh_numbers import MeshSessionNumbers

        self.numbers = MeshSessionNumbers(self)
        from terminal_mcp.storage.access_mesh_recipient_holds import MeshRecipientHolds

        self.recipient_holds = MeshRecipientHolds(self)
        self.initialize_registry()
        from terminal_mcp.storage.access_mesh_registry_upgrade import migrate_receipt_kinds

        migrate_receipt_kinds(self)

    def _archive_cipher(self):
        key = hmac.new(
            self._proof_key,
            f"terminal-mcp-access-local-proof-history-v1:{self.local_node_id}".encode(),
            hashlib.sha256,
        ).digest()
        return AESGCM(key)

    def _load_local_proof_history(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT mesh_id,authority_id,proof_fingerprint,sealed "
                "FROM access_mesh_local_proof_history ORDER BY mesh_id,authority_id"
            ).fetchall()
        keys = []
        for row in rows:
            associated = f"{row['mesh_id']}:{row['authority_id']}".encode()
            blob = row["sealed"]
            try:
                key = self._archive_cipher().decrypt(blob[:12], blob[12:], associated)
            except Exception as exc:
                raise AccessMeshError("access_mesh_proof_history_unreadable") from exc
            if not hmac.compare_digest(hashlib.sha256(key).hexdigest(), row["proof_fingerprint"]):
                raise AccessMeshError("access_mesh_proof_history_unreadable")
            if key not in keys:
                keys.append(key)
        self._local_proof_history = tuple(keys)

    def install_managed_proof(self, *, mesh_id: str, authority_id: str, ingress_token: str) -> bool:
        """Derive group proof from the authenticated managed authority credential.

        Every authorized member already receives this credential through signed
        Fleet Control replication. HKDF-style domain separation prevents using
        the resulting proof as a bearer token. Protect historical local slots
        with an encrypted archive before switching the live verifier.
        """
        import re

        if (
            not isinstance(mesh_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", mesh_id) is None
            or not isinstance(authority_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", authority_id) is None
            or not isinstance(ingress_token, str)
            or len(ingress_token.encode()) < 32
            or authority_id not in self.trusted_issuers
        ):
            raise AccessMeshError("access_mesh_proof_enrollment_invalid")
        key = hmac.new(
            ingress_token.encode(),
            f"terminal-mcp-managed-native-access-proof-v1:{mesh_id}:{authority_id}".encode(),
            hashlib.sha256,
        ).digest()
        if self._managed_proof_key == key and getattr(self, "_managed_proof_scope", None) == (
            mesh_id,
            authority_id,
        ):
            return False
        nonce = secrets.token_bytes(12)
        associated = f"{mesh_id}:{authority_id}".encode()
        sealed = nonce + self._archive_cipher().encrypt(nonce, key, associated)
        fingerprint = hashlib.sha256(key).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_local_proof_history("
                "mesh_id,authority_id,proof_fingerprint,sealed) VALUES(?,?,?,?)",
                (mesh_id, authority_id, fingerprint, sealed),
            )
        self._load_local_proof_history()
        self._managed_proof_key = key
        self._managed_proof_scope = (mesh_id, authority_id)
        return True

    def clear_managed_proof(self) -> bool:
        changed = self._managed_proof_key is not None
        self._managed_proof_key = None
        self._managed_proof_scope = None
        return changed

    @property
    def managed_proof_ready(self) -> bool:
        return self._managed_proof_key is not None

    def set_delivery_notifier(self, callback) -> None:
        """Register an in-process wakeup, never a network call or durability gate."""
        self._delivery_notifier = callback

    def _prepare_schema_upgrade(self, db, *, from_version: int) -> None:
        super()._prepare_schema_upgrade(db, from_version=from_version)
        columns = {row[1] for row in db.execute("PRAGMA table_info(access_mesh_operator_defaults)")}
        ledger_exists = (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration_state'"
            ).fetchone()
            is not None
        )
        snapshot_required = ledger_exists and (
            db.execute(
                "SELECT 1 FROM schema_migration_state "
                "WHERE name='access_defaults_snapshot_required'"
            ).fetchone()
            is not None
        )
        # No native table means a fresh installation, whose Mesh inheritance
        # remains unchanged. An existing absent row meant configured native
        # defaults before v25; snapshot that effective policy, never Fleet policy.
        if not columns:
            return
        actual_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        # Core omits the ledger at version zero only for empty Access-only
        # metadata with no sequence history. Preserve that partial first
        # install's inheritance without manufacturing a persisted defaults row.
        if actual_version == 0 and not ledger_exists:
            return
        if (
            not snapshot_required
            and actual_version >= 26
            and {"inherit_policy", "parent_json"} <= columns
        ):
            return
        if db.execute("SELECT 1 FROM access_mesh_operator_defaults WHERE singleton=1").fetchone():
            if snapshot_required:
                db.execute(
                    "DELETE FROM schema_migration_state "
                    "WHERE name='access_defaults_snapshot_required'"
                )
            return
        if not isinstance(self._upgrade_defaults, SlotPolicy):
            raise RuntimeError("native Access upgrade requires its configured effective defaults")
        names = ["singleton", "revision", "policy_json"]
        values = [
            1,
            1,
            json.dumps(asdict(self._upgrade_defaults), sort_keys=True),
        ]
        if "legacy_enabled" in columns:
            # Old installations stored this required flag. It is retired in
            # the same initialization after preserving their effective policy.
            names.append("legacy_enabled")
            values.append(1)
        if "inherit_policy" in columns:
            names.append("inherit_policy")
            values.append(0)
        if "parent_json" in columns:
            names.append("parent_json")
            values.append(None)
        db.execute(
            "INSERT INTO access_mesh_operator_defaults (" + ",".join(names) + ") "
            "VALUES(" + ",".join("?" for _ in values) + ")",
            values,
        )
        if snapshot_required:
            db.execute(
                "DELETE FROM schema_migration_state WHERE name='access_defaults_snapshot_required'"
            )

    def _initialize(self) -> None:
        super()._initialize()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_local_sessions (
                issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                work_session_id TEXT NOT NULL, cycle_started_at TEXT NOT NULL,
                session_epoch INTEGER NOT NULL,
                PRIMARY KEY(issuer_id,slot_id)
            ) WITHOUT ROWID""")
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(access_mesh_local_sessions)")
            }
            for name in ("current_intent", "intent_updated_at", "intent_warned_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE access_mesh_local_sessions ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_cleanup_jobs (
                job_id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                logical_agent_id TEXT NOT NULL, work_session_id TEXT,
                session_epoch INTEGER, release_claims INTEGER NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS access_mesh_cleanup_pending
                ON access_mesh_cleanup_jobs(completed_at,logical_agent_id)""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_activity (
                connection_key TEXT PRIMARY KEY, attached_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            ) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_issuer_receipts (
                request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL
            ) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_issuer_bindings (
                connection_key TEXT PRIMARY KEY, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL
            ) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_native_owners (
                logical_agent_id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL
            ) WITHOUT ROWID""")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_operator_defaults (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
                policy_json TEXT NOT NULL,
                legacy_admission_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(legacy_admission_enabled IN (0,1))
            )""")
            default_columns = {
                row[1] for row in db.execute("PRAGMA table_info(access_mesh_operator_defaults)")
            }
            if "legacy_admission_enabled" not in default_columns:
                db.execute(
                    "ALTER TABLE access_mesh_operator_defaults ADD COLUMN "
                    "legacy_admission_enabled INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(legacy_admission_enabled IN (0,1))"
                )
            if "inherit_policy" not in default_columns:
                db.execute(
                    "ALTER TABLE access_mesh_operator_defaults "
                    "ADD COLUMN inherit_policy INTEGER NOT NULL DEFAULT 0"
                )
            if "parent_json" not in default_columns:
                db.execute("ALTER TABLE access_mesh_operator_defaults ADD COLUMN parent_json TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_slot_catalog (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, issuer_id TEXT NOT NULL,
                slot_id TEXT NOT NULL, UNIQUE(issuer_id,slot_id)
            )""")
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_slot_catalog(issuer_id,slot_id) "
                "SELECT issuer_id,slot_id FROM access_mesh_slot_replicas ORDER BY issuer_id,slot_id"
            )
            db.execute("""CREATE TRIGGER IF NOT EXISTS access_mesh_slot_catalog_insert
                AFTER INSERT ON access_mesh_slot_replicas BEGIN
                INSERT OR IGNORE INTO access_mesh_slot_catalog(issuer_id,slot_id)
                VALUES(NEW.issuer_id,NEW.slot_id); END""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS access_mesh_live_code_unique
                ON access_mesh_slot_replicas(issuer_id,code_tag)""")

    def human_name(self, slot: SlotSnapshot, *, short=False) -> str:
        name = slot.agent_name
        if not name:
            number = self.numbers.number_for_slot(slot.issuer_id, slot.slot_id)
            if number is None:
                raise AccessMeshError("agent_name_unavailable")
            name = number_agent_name(number)
        return name if short else full_agent_name(name, slot.display_suffix)

    @staticmethod
    def _release_at_end(slot: SlotSnapshot) -> bool:
        return slot.kind == "temporary" or slot.policy.release_on_end is True

    def _native_slot(self, db, slot: SlotSnapshot, stamp: str) -> None:
        if slot.session is None:
            return
        existing = db.execute(
            "SELECT authority_node_id FROM logical_agents WHERE logical_agent_id=?",
            (slot.session.logical_agent_id,),
        ).fetchone()
        conflicting = db.execute(
            "SELECT 1 FROM access_mesh_sessions WHERE logical_agent_id=? "
            "AND (issuer_id!=? OR slot_id!=?)",
            (slot.session.logical_agent_id, slot.issuer_id, slot.slot_id),
        ).fetchone()
        owner = db.execute(
            "SELECT issuer_id,slot_id FROM access_mesh_native_owners WHERE logical_agent_id=?",
            (slot.session.logical_agent_id,),
        ).fetchone()
        returning = False
        if (
            existing
            and owner is None
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='mesh_identity_history'"
            ).fetchone()
        ):
            returning = (
                db.execute(
                    "SELECT 1 FROM mesh_identity_history WHERE namespace_id=? "
                    "AND logical_agent_id=? "
                    "AND issuer_id=? AND slot_id=?",
                    (
                        self.namespace_id,
                        slot.session.logical_agent_id,
                        slot.issuer_id,
                        slot.slot_id,
                    ),
                ).fetchone()
                is not None
            )
        if (
            conflicting
            or (existing and existing[0] != self.local_node_id)
            or (existing and owner is None and not returning)
            or (owner and tuple(owner) != (slot.issuer_id, slot.slot_id))
        ):
            raise AccessMeshError("access_mesh_identity_conflict")
        db.execute(
            "INSERT INTO access_mesh_native_owners VALUES(?,?,?) "
            "ON CONFLICT(logical_agent_id) DO NOTHING",
            (slot.session.logical_agent_id, slot.issuer_id, slot.slot_id),
        )
        db.execute(
            """INSERT INTO logical_agents (
            logical_agent_id,display_name,state,authority_node_id,authority_epoch,
            slot_revision,selector_generation,auth_generation,created_at,updated_at)
            VALUES(?,?,'armed',?,1,1,1,1,?,?) ON CONFLICT(logical_agent_id) DO NOTHING""",
            (
                slot.session.logical_agent_id,
                full_agent_name(slot.agent_name, slot.display_suffix)
                if slot.agent_name
                else "Agent name unavailable",
                self.local_node_id,
                stamp,
                stamp,
            ),
        )
        if returning and slot.state == "active":
            db.execute(
                "UPDATE logical_agents SET state='armed',deleted_at=NULL,"
                "tombstone_reason=NULL,updated_at=? "
                "WHERE logical_agent_id=?",
                (stamp, slot.session.logical_agent_id),
            )
        if slot.state == "suspended":
            db.execute(
                "UPDATE logical_agents SET state=?,updated_at=?,deleted_at=NULL "
                "WHERE logical_agent_id=?",
                (
                    slot.state,
                    stamp,
                    slot.session.logical_agent_id,
                ),
            )

    def _queue_cleanup(
        self, db, slot: SlotSnapshot, *, job_id: str, reason: str, stamp: str, release_claims: bool
    ) -> None:
        if slot.session is None:
            return
        current = db.execute(
            "SELECT l.*,s.hard_expires_at FROM access_mesh_local_sessions l "
            "LEFT JOIN logical_agent_work_sessions s USING(work_session_id) "
            "WHERE l.issuer_id=? AND l.slot_id=?",
            (slot.issuer_id, slot.slot_id),
        ).fetchone()
        session_id = current["work_session_id"] if current else None
        epoch = current["session_epoch"] if current else None
        ended_at = stamp
        if reason == "local_cycle_ended" and current is not None and current["hard_expires_at"]:
            ended_at = min(stamp, current["hard_expires_at"])
        if session_id:
            db.execute(
                "UPDATE logical_agent_work_sessions SET state='ended',ended_at=?,"
                "end_reason=? WHERE work_session_id=? AND state IN ('active','stopping')",
                (
                    ended_at,
                    session_end_reason(
                        reason, ended_at=ended_at, hard_expires_at=current["hard_expires_at"]
                    ),
                    session_id,
                ),
            )
            db.execute(
                "UPDATE access_mesh_slot_replicas SET last_used_at=MAX(COALESCE(last_used_a"
                "t,''),?) "
                "WHERE issuer_id=? AND slot_id=?",
                (ended_at, slot.issuer_id, slot.slot_id),
            )
        db.execute(
            """INSERT INTO access_mesh_cleanup_jobs
            (job_id,issuer_id,slot_id,logical_agent_id,work_session_id,session_epoch,
             release_claims,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO NOTHING""",
            (
                job_id,
                slot.issuer_id,
                slot.slot_id,
                slot.session.logical_agent_id,
                session_id,
                epoch,
                int(release_claims),
                reason,
                stamp,
            ),
        )

    def _after_apply(self, db, event: AccessSlotEvent, *, release_claims: bool = False) -> None:
        row = db.execute(
            "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
            (event.issuer_id, event.slot_id),
        ).fetchone()
        slot = self._snapshot(row, _connection=db)
        stamp = _text(
            event.effective_at
            if event.kind in {"SessionEnded", "SlotSuspended"} and event.effective_at is not None
            else self.clock()
        )
        self._native_slot(db, slot, stamp)
        number = _mutation_number.get()
        if event.kind == "SlotIssued" and number is not None:
            started = number.get("started_at") or event.effective_at or self.clock()
            expires = started + timedelta(seconds=slot.policy.duration_seconds)
            if db.execute(
                "SELECT 1 FROM access_mesh_recipient_holds WHERE number=? "
                "AND state='held' AND (issuer_id!=? OR slot_id!=?) LIMIT 1",
                (number["number"], event.issuer_id, event.slot_id),
            ).fetchone():
                raise AccessMeshError("access_mesh_subscription_hold")
            db.execute(
                "INSERT INTO access_mesh_number_claims "
                "(issuer_id,slot_id,number,started_at,hard_expires_at) "
                "VALUES(?,?,?,?,?)",
                (
                    event.issuer_id,
                    event.slot_id,
                    number["number"],
                    _text(started),
                    _text(expires),
                ),
            )
            db.execute(
                "DELETE FROM access_mesh_number_reservations WHERE attempt_id=?",
                (number["attempt_id"],),
            )
        ending = _mutation_end.get()
        if event.kind == "SessionEnded" and ending is not None:
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_number_ends VALUES(?,?,?,?,?,?,?)",
                (
                    ending["number"],
                    ending["cycle_key"],
                    event.issuer_id,
                    event.slot_id,
                    event.event_id,
                    _text(event.effective_at),
                    event.revision,
                ),
            )
        if event.kind == "SessionStarted":
            previous = db.execute(
                "SELECT number FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (event.issuer_id, event.slot_id),
            ).fetchone()
            start_data = _mutation_start.get()
            # Replicas use the activation time and revision authored by the
            # issuer, including an early resume of the existing window.
            if previous:
                active_from = (
                    event.active_from or (start_data or {}).get("active_from") or event.effective_at
                )
                started_at = event.effective_at or active_from
                expires = event.deadline_at or (
                    started_at + timedelta(seconds=slot.policy.duration_seconds)
                )
                db.execute(
                    "INSERT OR IGNORE INTO access_mesh_number_starts "
                    "(event_id,number,issuer_id,slot_id,active_from,"
                    "started_at,hard_expires_at,revision) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        previous["number"],
                        event.issuer_id,
                        event.slot_id,
                        _text(active_from),
                        _text(started_at),
                        _text(expires),
                        event.revision,
                    ),
                )
        receipt = _mutation_receipt.get()
        if receipt is not None and receipt["event_id"] == event.event_id:
            self._save_receipt_tx(db, receipt)
        if event.kind in {"SessionStarted", "SessionUpdated"} and slot.anchor:
            active_deadline = slot.deadline_at or (
                slot.anchor + timedelta(seconds=slot.policy.duration_seconds)
            )
            db.execute(
                "UPDATE access_mesh_number_claims "
                "SET hard_expires_at=MAX(hard_expires_at, ?) "
                "WHERE issuer_id=? AND slot_id=?",
                (_text(active_deadline), slot.issuer_id, slot.slot_id),
            )
        if event.kind in {"SessionStarted", "SessionUpdated", "SlotPolicyChanged"}:
            self._reconcile_local_tx(
                db, slot, stamp=stamp, job_id=f"event:{event.issuer_id}:{event.event_id}"
            )
        if event.kind in {"SessionEnded", "SlotSuspended", "SlotDeleted"}:
            self._queue_cleanup(
                db,
                slot,
                job_id=f"event:{event.issuer_id}:{event.event_id}",
                reason=event.kind,
                stamp=stamp,
                release_claims=release_claims,
            )

    def _reconcile_local_tx(self, db, slot, *, stamp, job_id, force_fence=False):
        current = db.execute(
            "SELECT l.*,s.state FROM access_mesh_local_sessions l "
            "JOIN logical_agent_work_sessions s USING(work_session_id) "
            "WHERE l.issuer_id=? AND l.slot_id=?",
            (slot.issuer_id, slot.slot_id),
        ).fetchone()
        if current is None or current["state"] != "active":
            return
        cycle = local_cycle(slot, _parse(stamp))
        same = (
            not force_fence
            and cycle["state"] in {"active", "warning", "draining"}
            and current["cycle_started_at"] == cycle["started_at"]
        )
        if same:
            db.execute(
                "UPDATE logical_agent_work_sessions SET hard_expires_at=? WHERE work_session_id=?",
                (cycle["hard_expires_at"], current["work_session_id"]),
            )
        else:
            self._queue_cleanup(
                db,
                slot,
                job_id=job_id,
                reason="replicated_cycle_fence",
                stamp=stamp,
                release_claims=self._release_at_end(slot),
            )

    def _prepare_new_slot(self, db, event: AccessSlotEvent) -> None:
        """A slot keeps its number until an explicit physical deletion."""
        if not event.code_tag:
            return
        if (
            db.execute(
                "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? "
                "AND code_tag=? AND slot_id!=?",
                (event.issuer_id, event.code_tag, event.slot_id),
            ).fetchone()
            is not None
        ):
            raise AccessMeshError("access_mesh_code_in_use")

    def _before_slot_update(self, db, old, *, state, code_tag):
        from terminal_mcp.storage.access_mesh_recipient_holds import slot_held

        if code_tag != old["code_tag"] and slot_held(db, old["issuer_id"], old["slot_id"]):
            raise AccessMeshError("access_mesh_subscription_hold")

    def _purge_slot(self, db, slot: SlotSnapshot) -> None:
        """Delete the slot's current storage while retaining command and event history."""
        key = (slot.issuer_id, slot.slot_id)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "access_mesh_activity" in tables:
            db.execute(
                "DELETE FROM access_mesh_activity WHERE connection_key IN "
                "(SELECT connection_key FROM access_mesh_attachments WHERE issuer_id=? AND "
                "slot_id=?)",
                key,
            )
        if slot.session is not None:
            agent = (slot.session.logical_agent_id,)
            for table in (
                "logical_agent_rearms",
                "logical_agent_node_attachments",
                "persistent_command_permits",
                "logical_agent_work_windows",
                "logical_agent_work_sessions",
            ):
                if table in tables:
                    db.execute(f"DELETE FROM {table} WHERE logical_agent_id=?", agent)
            if "logical_agents" in tables:
                db.execute(
                    "UPDATE logical_agents SET state='suspended',updated_at=? WHERE "
                    "logical_agent_id=?",
                    (_text(self.clock()), *agent),
                )
        for table in (
            "access_mesh_local_sessions",
            "access_mesh_issuer_bindings",
            "access_mesh_native_owners",
            "access_mesh_number_claims",
            "access_mesh_number_starts",
            "access_mesh_number_ends",
            "access_mesh_recipient_holds",
            "access_mesh_slot_catalog",
        ):
            if table in tables:
                db.execute(f"DELETE FROM {table} WHERE issuer_id=? AND slot_id=?", key)
        super()._purge_slot(db, slot)

    def _slot_unused(self, db, slot: SlotSnapshot, *, before: datetime, now: datetime) -> bool:
        if not super()._slot_unused(db, slot, before=before, now=now):
            return False
        return (
            db.execute(
                "SELECT 1 FROM access_mesh_local_sessions l JOIN logical_agent_work_sessions s "
                "USING(work_session_id) WHERE l.issuer_id=? AND l.slot_id=? "
                "AND s.state IN ('active','stopping') AND s.hard_expires_at>? LIMIT 1",
                (slot.issuer_id, slot.slot_id, _text(now)),
            ).fetchone()
            is None
        )

    def expired_unused_slots(
        self, *, now: datetime, after: str = "", limit: int = 50
    ) -> list[SlotSnapshot]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessMeshError("access_mesh_invalid_limit")
        before = _utc(now) - timedelta(hours=72)
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.* FROM access_mesh_slot_replicas r "
                "LEFT JOIN access_mesh_sessions identity USING(issuer_id,slot_id) WHERE "
                "r.slot_id>? "
                "AND MAX(julianday(COALESCE(r.last_used_at,r.created_at)),COALESCE(julianday("
                "CASE WHEN identity.active=0 THEN identity.ended_at "
                "WHEN identity.active=1 AND r.state='active' THEN "
                "COALESCE(json_extract(r.policy_window_json,'$.hard_expires_at'),r.deadline_at,"
                "datetime(r.anchor,'+'||json_extract(r.policy_json,'$.duration_seconds')||'"
                " seconds')) END),0))<=julianday(?) "
                "AND NOT EXISTS(SELECT 1 FROM access_mesh_sessions ss WHERE "
                "ss.issuer_id=r.issuer_id "
                "AND ss.slot_id=r.slot_id AND ss.active=1 AND r.state='active' "
                "AND julianday(COALESCE(json_extract(r.policy_window_json,'$.hard_expires_a"
                "t'),r.deadline_at,"
                "datetime(r.anchor,'+'||json_extract(r.policy_json,'$.duration_seconds')||'"
                " seconds')))>julianday(?)) "
                "AND NOT EXISTS(SELECT 1 FROM access_mesh_local_sessions l "
                "JOIN logical_agent_work_sessions s USING(work_session_id) "
                "WHERE l.issuer_id=r.issuer_id AND l.slot_id=r.slot_id "
                "AND s.state IN ('active','stopping') AND s.hard_expires_at>?) "
                "ORDER BY r.slot_id LIMIT ?",
                (after, _text(before), _text(now), _text(now), limit),
            ).fetchall()
            return [
                slot
                for row in rows
                if self._slot_unused(
                    db, slot := self._snapshot(row, _connection=db), before=before, now=now
                )
            ]

    def apply_event(
        self,
        event,
        *,
        mutation_receipt=None,
        mutation_number=None,
        mutation_end=None,
        mutation_start=None,
        **kwargs,
    ):
        token = _mutation_receipt.set(mutation_receipt)
        number_token = _mutation_number.set(mutation_number)
        end_token = _mutation_end.set(mutation_end)
        start_token = _mutation_start.set(mutation_start)
        try:
            try:
                result = super().apply_event(event, **kwargs)
                # The parent transaction has committed before this hook runs.
                # It is safe to wake the transport; failure to notify never
                # invalidates the durable outbox committed with the event.
                if (
                    result.outcome == "applied"
                    and self._delivery_notifier is not None
                    and kwargs.get("_connection") is None
                ):
                    self._delivery_notifier()
                return result
            except sqlite3.IntegrityError as exc:
                if event.code_tag is not None:
                    with self._connect() as db:
                        duplicate = db.execute(
                            "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? "
                            "AND code_tag=?",
                            (event.issuer_id, event.code_tag),
                        ).fetchone()
                    if duplicate is not None:
                        raise AccessMeshError("access_mesh_code_in_use") from exc
                raise
        finally:
            _mutation_start.reset(start_token)
            _mutation_end.reset(end_token)
            _mutation_number.reset(number_token)
            _mutation_receipt.reset(token)

    def _receipt_cipher(self):
        key = hmac.new(
            self._proof_key,
            f"access-mesh-issuer-receipt-v2:{self.local_node_id}".encode(),
            hashlib.sha256,
        ).digest()
        return AESGCM(key)

    def prepare_receipt(self, *, event_id, request_key, fingerprint, connection_key, result):
        nonce = secrets.token_bytes(12)
        encoded = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
        sealed = nonce + self._receipt_cipher().encrypt(nonce, encoded, request_key.encode())
        return {
            "event_id": event_id,
            "request_key": request_key,
            "fingerprint": fingerprint,
            "connection_key": connection_key,
            "issuer_id": result["issuer_node_id"],
            "slot_id": result["slot_id"],
            "sealed": base64.b64encode(sealed).decode(),
        }

    @staticmethod
    def _save_receipt_tx(db, receipt):
        db.execute(
            "INSERT INTO access_mesh_issuer_receipts VALUES(?,?,?,?)",
            (
                receipt["request_key"],
                receipt["fingerprint"],
                receipt["sealed"],
                _text(datetime.now(UTC)),
            ),
        )
        if receipt.get("connection_key"):
            db.execute(
                "INSERT INTO access_mesh_issuer_bindings VALUES(?,?,?) "
                "ON CONFLICT(connection_key) DO UPDATE SET issuer_id=excluded.issuer_id,"
                "slot_id=excluded.slot_id",
                (receipt["connection_key"], receipt["issuer_id"], receipt["slot_id"]),
            )

    def save_receipt(self, receipt):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            self._save_receipt_tx(db, receipt)

    def receipt(self, request_key: str, fingerprint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT fingerprint,result_json FROM access_mesh_issuer_receipts "
                "WHERE request_key=?",
                (request_key,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise AccessMeshError("idempotency_conflict")
        sealed = base64.b64decode(row[1])
        clear = self._receipt_cipher().decrypt(sealed[:12], sealed[12:], request_key.encode())
        return json.loads(clear)

    def issuer_bound_slot(self, connection_key: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT s.* FROM access_mesh_issuer_bindings b "
                "JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id) "
                "WHERE b.connection_key=?",
                (connection_key,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def code_slot(self, issuer_id: str, code: str) -> SlotSnapshot | None:
        tags = self.code_tags(issuer_id, code)
        with self._connect() as db:
            placeholders = ",".join("?" for _ in tags)
            rows = db.execute(
                "SELECT * FROM access_mesh_slot_replicas "
                f"WHERE issuer_id=? AND code_tag IN ({placeholders})",
                (issuer_id, *tags),
            ).fetchall()
        if len(rows) > 1:
            raise AccessMeshError("access_mesh_ambiguous_code")
        return self._snapshot(rows[0]) if rows else None

    def slot_for_agent(self, logical_agent_id: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.* FROM access_mesh_sessions s JOIN access_mesh_slot_replicas r "
                "USING(issuer_id,slot_id) WHERE s.logical_agent_id=?",
                (logical_agent_id,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def canonical_slot(self, slot: SlotSnapshot) -> SlotSnapshot:
        """Translate a historical Mesh alias to the deterministic winner."""
        winner = self.numbers.group_winner(slot.issuer_id, slot.slot_id)
        if winner is None:
            return slot
        selected = self.slot(winner["issuer_id"], winner["slot_id"])
        return selected if selected is not None and selected.state == "active" else slot

    def attached_slot(self, connection_key: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT s.* FROM access_mesh_attachments a
                JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id)
                WHERE a.connection_key=?""",
                (connection_key,),
            ).fetchone()
        if not row:
            return None
        return self.canonical_slot(self._snapshot(row))

    def attach(self, *, issuer_id: str, code: str, connection_key: str) -> SlotSnapshot:
        self.assert_registry_ready()
        slot = super().attach(issuer_id=issuer_id, code=code, connection_key=connection_key)
        self.touch(connection_key)
        return slot

    def touch(self, connection_key: str, *, now: datetime | None = None) -> None:
        stamp = _text(now or self.clock())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO access_mesh_activity
                (connection_key,attached_at,last_active_at)
                SELECT connection_key,?,? FROM access_mesh_attachments WHERE connection_key=?
                ON CONFLICT(connection_key) DO UPDATE SET last_active_at=excluded.last_active_at""",
                (stamp, stamp, connection_key),
            )
            db.execute(
                "UPDATE access_mesh_slot_replicas SET last_used_at=MAX(COALESCE(last_used_a"
                "t,''),?) "
                "WHERE (issuer_id,slot_id) IN (SELECT issuer_id,slot_id FROM "
                "access_mesh_attachments "
                "WHERE connection_key=?)",
                (stamp, connection_key),
            )

    def touch_slot(self, issuer_id: str, slot_id: str, *, now: datetime | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE access_mesh_slot_replicas SET last_used_at=MAX(COALESCE(last_used_a"
                "t,''),?) "
                "WHERE issuer_id=? AND slot_id=?",
                (_text(now or self.clock()), issuer_id, slot_id),
            )

    def intent_status(
        self, slot: SlotSnapshot, *, now: datetime, observe=False, require_intent=False
    ) -> dict:
        """Atomically observe one session's due warning; reads never refresh intent.

        The first call at >=180 seconds receives grace. A serialized later write
        cannot share that grace, including concurrent calls and process restarts.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            row = db.execute(
                "SELECT current_intent,intent_updated_at,intent_warned_at,work_session_id "
                "FROM access_mesh_local_sessions WHERE issuer_id=? AND slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            if row is None or not row[0] or not row[1]:
                return {"state": "missing", "blocked": require_intent, "warn": False}
            due = _parse(row[1]) + timedelta(seconds=180)
            stale = _utc(now) >= due
            first = stale and not row[2] and observe
            if first:
                db.execute(
                    "UPDATE access_mesh_local_sessions SET intent_warned_at=? "
                    "WHERE work_session_id=? AND intent_warned_at IS NULL",
                    (_text(now), row[3]),
                )
            return {
                "state": "stale" if stale else "fresh",
                "intent_updated_at": row[1],
                "refresh_due_at": _text(due),
                "warn": bool(first),
                "blocked": bool(require_intent and stale and row[2]),
            }

    def update_intent(
        self,
        slot: SlotSnapshot,
        work_session_id: str,
        intent: str,
        *,
        now: datetime,
        initial_only=False,
    ) -> dict:
        stamp = _text(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            if initial_only:
                existing = db.execute(
                    "SELECT intent_updated_at FROM access_mesh_local_sessions "
                    "WHERE issuer_id=? AND slot_id=? AND work_session_id=? "
                    "AND current_intent IS NOT NULL",
                    (slot.issuer_id, slot.slot_id, work_session_id),
                ).fetchone()
                if existing and existing[0]:
                    return {
                        "ok": True,
                        "intent_updated_at": existing[0],
                        "refresh_due_at": _text(_parse(existing[0]) + timedelta(seconds=180)),
                    }
            updated = db.execute(
                "UPDATE access_mesh_local_sessions SET current_intent=?,intent_updated_at=?,"
                "intent_warned_at=NULL WHERE issuer_id=? AND slot_id=? AND work_session_id=? "
                "AND EXISTS(SELECT 1 FROM logical_agent_work_sessions s WHERE "
                "s.work_session_id=access_mesh_local_sessions.work_session_id AND s.state='active' "
                "AND s.hard_expires_at>?)",
                (intent, stamp, slot.issuer_id, slot.slot_id, work_session_id, stamp),
            ).rowcount
            if not updated:
                raise AccessMeshError("session_expired")
        return {
            "ok": True,
            "intent_updated_at": stamp,
            "refresh_due_at": _text(_utc(now) + timedelta(seconds=180)),
        }

    def agent_overview(
        self,
        *,
        now: datetime,
        after="",
        limit=20,
        include_inactive=False,
        logical_agent_id=None,
        offset=0,
    ) -> AgentOverviewPage:
        """Bounded raw scan with continuation independent of merged-cycle filtering."""
        scan_limit = max(1, min(limit, 100))
        with self._connect() as db:
            rows = db.execute(
                # Collapse historical number aliases before paging. The running
                # maximum partitions touching/transitive lifetimes exactly as
                # MeshSessionNumbers._groups; an inactive/missing winner keeps
                # the original slot, matching canonical_slot's fallback.
                "WITH ordered_claims AS ("
                "SELECT c.*,MAX(hard_expires_at) OVER (PARTITION BY number "
                "ORDER BY started_at,issuer_id,slot_id ROWS BETWEEN UNBOUNDED "
                "PRECEDING AND 1 PRECEDING) AS previous_end "
                "FROM access_mesh_number_claims c),"
                "grouped_claims AS (SELECT *,SUM(CASE WHEN previous_end IS NULL "
                "OR started_at>previous_end THEN 1 ELSE 0 END) OVER ("
                "PARTITION BY number ORDER BY started_at,issuer_id,slot_id "
                "ROWS UNBOUNDED PRECEDING) AS group_id FROM ordered_claims),"
                "claim_winners AS (SELECT issuer_id,slot_id,"
                "FIRST_VALUE(issuer_id) OVER (PARTITION BY number,group_id "
                "ORDER BY started_at DESC,issuer_id DESC,slot_id DESC) AS winner_issuer,"
                "FIRST_VALUE(slot_id) OVER (PARTITION BY number,group_id "
                "ORDER BY started_at DESC,issuer_id DESC,slot_id DESC) AS winner_slot "
                "FROM grouped_claims),"
                "attachment_map AS (SELECT a.connection_key,"
                "COALESCE(w.issuer_id,r.issuer_id) AS issuer_id,"
                "COALESCE(w.slot_id,r.slot_id) AS slot_id "
                "FROM access_mesh_attachments a "
                "JOIN access_mesh_slot_replicas r USING(issuer_id,slot_id) "
                "LEFT JOIN claim_winners c USING(issuer_id,slot_id) "
                "LEFT JOIN access_mesh_slot_replicas w ON w.issuer_id=c.winner_issuer "
                "AND w.slot_id=c.winner_slot AND w.state='active'),"
                "local_presence AS (SELECT a.issuer_id,a.slot_id,"
                "MAX(y.last_active_at) AS last_activity FROM attachment_map a "
                "LEFT JOIN access_mesh_activity y USING(connection_key) "
                "GROUP BY a.issuer_id,a.slot_id) "
                "SELECT r.*,i.logical_agent_id AS session_agent_id,l.work_session_id,l.sess"
                "ion_epoch,l.current_intent,"
                "l.intent_updated_at,s.state AS work_state,s.started_at AS work_started,"
                "s.hard_expires_at AS work_expiry,g.display_name,p.last_activity "
                "FROM access_mesh_slot_replicas r "
                "JOIN access_mesh_sessions i USING(issuer_id,slot_id) "
                "JOIN local_presence p USING(issuer_id,slot_id) "
                "LEFT JOIN access_mesh_local_sessions l USING(issuer_id,slot_id) "
                "LEFT JOIN logical_agent_work_sessions s USING(work_session_id) "
                "LEFT JOIN logical_agents g ON g.logical_agent_id=i.logical_agent_id "
                "WHERE i.logical_agent_id>? AND (? IS NULL OR i.logical_agent_id=?) "
                "AND (? OR (r.state='active' AND s.state='active' AND s.hard_expires_at>?)) "
                "ORDER BY i.logical_agent_id LIMIT ? OFFSET ?",
                (
                    after,
                    logical_agent_id,
                    logical_agent_id,
                    include_inactive,
                    _text(now),
                    scan_limit + 1,
                    offset,
                ),
            ).fetchall()
            scanned = rows[:scan_limit]
            result = []
            row_offsets = []
            for index, row in enumerate(scanned, start=offset + 1):
                slot = self._snapshot(row, _connection=db)
                cycle = self.merged_cycle(slot, now)
                live = cycle["state"] in {"active", "warning", "draining"}
                if not include_inactive and not live:
                    continue
                updated = row["intent_updated_at"]
                last = row["last_activity"]
                started = row["work_started"] or cycle["started_at"]
                expiry = row["work_expiry"] or cycle["hard_expires_at"]
                result.append(
                    {
                        "logical_agent_id": slot.session.logical_agent_id,
                        "public_name": self.human_name(slot),
                        "display_name": self.human_name(slot),
                        "last_server": self.local_node_id,
                        "session_duration": max(
                            0, int((_parse(expiry) - _parse(started)).total_seconds())
                        )
                        if started and expiry
                        else 0,
                        "last_activity": last,
                        "activity_stale": last is None
                        or (_utc(now) - _parse(last)).total_seconds() >= 180,
                        "current_intent": row["current_intent"],
                        "intent_updated_at": updated,
                        "intent_state": "missing"
                        if not updated
                        else "stale"
                        if _utc(now) >= _parse(updated) + timedelta(seconds=180)
                        else "fresh",
                        "work_session_id": row["work_session_id"],
                        "session_epoch": row["session_epoch"],
                        "session_state": row["work_state"]
                        if row["work_state"] == "ended"
                        else cycle["state"],
                        "hard_expires_at": expiry,
                        "session_started_at": started,
                    }
                )
                row_offsets.append(index)
            return AgentOverviewPage(
                agents=result,
                row_offsets=row_offsets,
                scanned_until=offset + len(scanned),
                has_more=len(rows) > scan_limit,
            )

    def last_activity(self, issuer_id: str, slot_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(a.last_active_at) FROM access_mesh_activity a "
                "JOIN access_mesh_attachments x USING(connection_key) "
                "WHERE x.issuer_id=? AND x.slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            return row[0] if row else None

    def lifecycle_signature(self) -> tuple:
        """Read the inputs to local maintenance in one consistent snapshot.

        Watch attached identities and all their possible number-group winners,
        including cross-process start/end/policy changes. Command/output history,
        activity timestamps and unrelated remote slots cannot change a cycle.
        This is a private change detector, never cached authorization state.
        """
        watched = """WITH attached AS (
            SELECT DISTINCT issuer_id,slot_id FROM access_mesh_attachments
        ), watched_numbers AS (
            SELECT DISTINCT c.number FROM access_mesh_number_claims c
            JOIN attached a USING(issuer_id,slot_id)
        ), watched_slots AS (
            SELECT issuer_id,slot_id FROM attached
            UNION SELECT issuer_id,slot_id FROM access_mesh_number_claims
              WHERE number IN (SELECT number FROM watched_numbers)
        ) """
        queries = (
            "SELECT DISTINCT issuer_id,slot_id,logical_agent_id "
            "FROM access_mesh_attachments ORDER BY issuer_id,slot_id,logical_agent_id",
            "SELECT s.* FROM access_mesh_slot_replicas s "
            "JOIN watched_slots w USING(issuer_id,slot_id) ORDER BY issuer_id,slot_id",
            "SELECT s.* FROM access_mesh_sessions s "
            "JOIN watched_slots w USING(issuer_id,slot_id) ORDER BY issuer_id,slot_id",
            "SELECT * FROM access_mesh_number_claims "
            "WHERE number IN (SELECT number FROM watched_numbers) ORDER BY issuer_id,slot_id",
            "SELECT * FROM access_mesh_number_starts "
            "WHERE number IN (SELECT number FROM watched_numbers) ORDER BY event_id",
            "SELECT * FROM access_mesh_number_ends "
            "WHERE number IN (SELECT number FROM watched_numbers) ORDER BY event_id",
            "SELECT l.* FROM access_mesh_local_sessions l "
            "JOIN watched_slots w USING(issuer_id,slot_id) ORDER BY issuer_id,slot_id",
            "SELECT s.work_session_id,s.state,s.hard_expires_at "
            "FROM logical_agent_work_sessions s "
            "JOIN access_mesh_local_sessions l USING(work_session_id) "
            "JOIN watched_slots w USING(issuer_id,slot_id) ORDER BY s.work_session_id",
            "SELECT a.logical_agent_id,a.state FROM logical_agents a "
            "JOIN logical_agent_work_sessions s USING(logical_agent_id) "
            "JOIN access_mesh_local_sessions l USING(work_session_id) "
            "JOIN watched_slots w USING(issuer_id,slot_id) ORDER BY s.work_session_id",
            "SELECT job_id,logical_agent_id,work_session_id,session_epoch,reason,release_claims "
            "FROM access_mesh_cleanup_jobs WHERE completed_at IS NULL ORDER BY job_id",
        )
        with self._connect() as db:
            db.execute("BEGIN")
            rows = tuple(
                tuple(tuple(row) for row in db.execute(watched + query)) for query in queries
            )
        return tuple(sorted(self.trusted_issuers)), rows

    def local_slots(self, *, after: str = "", limit: int = 50) -> list[SlotSnapshot]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessMeshError("access_mesh_invalid_limit")
        with self._connect() as db:
            rows = db.execute(
                """SELECT DISTINCT s.* FROM access_mesh_slot_replicas s
                JOIN access_mesh_attachments a USING(issuer_id,slot_id)
                JOIN access_mesh_sessions i USING(issuer_id,slot_id)
                WHERE i.logical_agent_id>? ORDER BY i.logical_agent_id LIMIT ?""",
                (after, limit),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def capture_policy_window(self, slot: SlotSnapshot, now: datetime):
        """Capture the same number-backed window that public MCP actually authorizes.

        The stable number claim is an identity, not the rotating connection code.
        Existing direct/non-number slots retain their original domain behavior.
        """
        from terminal_mcp.core.access_mesh_grants import CapturedAccessWindow
        from terminal_mcp.core.access_policy_windows import capture_window

        readable = replace(slot, state="active") if slot.state == "suspended" else slot
        winner = self.numbers.group_winner(slot.issuer_id, slot.slot_id)
        if winner is None:
            return capture_window(readable, now)
        current = readable
        if (winner["issuer_id"], winner["slot_id"]) != (slot.issuer_id, slot.slot_id):
            current = self.slot(winner["issuer_id"], winner["slot_id"]) or readable
        cycle = self.merged_cycle(readable, now)
        start, end = _parse(cycle["started_at"]), _parse(cycle["hard_expires_at"])
        if start is None or end is None:
            return capture_window(readable, now)
        policy = current.effective(start).policy
        rearm = end + timedelta(seconds=policy.cooldown_seconds)
        ended = self.numbers.end_for_slot(slot.issuer_id, slot.slot_id)
        closed = _parse(ended["ended_at"]) if ended else None
        if closed is not None and closed < start:
            closed = None
        if closed is not None:
            rearm = closed + timedelta(seconds=policy.cooldown_seconds)
        return CapturedAccessWindow(policy, start, end, rearm, closed)

    def rotated_code_slot(self, code: str):
        """Resolve a rotated bearer tag without changing a stable recipient identity.

        Only issuer-authenticated replicated slot tags with an existing number
        claim qualify. No new grant, alias table or implicit window is created.
        Ambiguous claims stay closed. Each issuer lookup uses its code-tag index.
        """
        self.numbers.validate(code)
        found = []
        with self._connect() as db:
            for issuer in sorted(self.registry_issuers()):
                # Historic LOCAL slots may have been issued before the Mesh
                # authority key rotated. Never restore remote issuer proofs.
                if (
                    issuer != self.local_node_id
                    and getattr(self, "managed_trust_required", False)
                    and self._managed_proof_key is None
                ):
                    continue
                tags = self.code_tags(issuer, code)
                marks = ",".join("?" for _ in tags)
                rows = db.execute(
                    "SELECT s.* FROM access_mesh_slot_replicas s "
                    "JOIN access_mesh_number_claims c USING(issuer_id,slot_id) "
                    f"WHERE s.issuer_id=? AND s.code_tag IN ({marks}) AND s.state='active' "
                    "AND c.number!=?",
                    (issuer, *tags, code),
                ).fetchall()
                found.extend(self._snapshot(row, _connection=db) for row in rows)
        if len(found) > 1:
            raise AccessMeshError("access_mesh_ambiguous_code")
        if found:
            selected = found[0]
            winner = self.numbers.group_winner(selected.issuer_id, selected.slot_id)
            if winner is None or (winner["issuer_id"], winner["slot_id"]) != (
                selected.issuer_id,
                selected.slot_id,
            ):
                raise AccessMeshError("access_mesh_identity_conflict")
            return selected
        return None

    def merged_cycle(self, slot: SlotSnapshot, now: datetime) -> dict:
        """Project explicit, shared Mesh start/end transitions for this epoch.

        No new WorkSession is silently materialized by policy rearm: after a
        deadline or explicit end, another Access.start must authorize it.
        """
        number = self.numbers.number_for_slot(slot.issuer_id, slot.slot_id)
        winner = self.numbers.group_winner(slot.issuer_id, slot.slot_id)
        if number is None or winner is None:
            return local_cycle(slot, now)
        if (slot.issuer_id, slot.slot_id) != (winner["issuer_id"], winner["slot_id"]):
            effective = self.slot(winner["issuer_id"], winner["slot_id"])
            if effective is not None:
                slot = effective
        from terminal_mcp.core.access_policy_windows import merged_slot_cycle

        return merged_slot_cycle(
            slot,
            now,
            winner=winner,
            latest_start=self.numbers.start_for_slot(slot.issuer_id, slot.slot_id),
            latest_end=self.numbers.end_for_slot(slot.issuer_id, slot.slot_id),
        )

    def number_cycle(self, slot: SlotSnapshot, now: datetime) -> dict | None:
        """Stable per-cycle key prevents a late end fencing a newer cycle."""
        number = self.numbers.number_for_slot(slot.issuer_id, slot.slot_id)
        winner = self.numbers.group_winner(slot.issuer_id, slot.slot_id)
        if winner is None:
            return None
        effective = self.slot(winner["issuer_id"], winner["slot_id"])
        if effective is None:
            return None
        cycle = self.merged_cycle(effective, now)
        if cycle["state"] not in {"active", "warning", "draining"}:
            return None
        return {
            "number": number,
            "cycle_key": f"{winner['issuer_id']}:{winner['slot_id']}:{cycle['started_at']}",
        }

    def observed_identity(self, slot: SlotSnapshot, *, now: datetime | None = None) -> dict:
        """Pure status read. Lifecycle materialization belongs to tick/attach/write admission."""
        now = _utc(now or datetime.now(UTC))
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            if row is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            slot = self._snapshot(row, _connection=db)
            agent_id = slot.session.logical_agent_id if slot.session is not None else None
            cycle = self.merged_cycle(slot, now)
            current = db.execute(
                "SELECT l.*,s.state FROM access_mesh_local_sessions l "
                "JOIN logical_agent_work_sessions s USING(work_session_id) "
                "WHERE l.issuer_id=? AND l.slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            pending = db.execute(
                "SELECT 1 FROM access_mesh_cleanup_jobs WHERE logical_agent_id=? "
                "AND completed_at IS NULL LIMIT 1",
                (agent_id,),
            ).fetchone()
        identity = {
            "ok": True,
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "logical_agent_id": agent_id,
            "authority_node_id": self.local_node_id,
            "public_name": self.human_name(slot),
            "mode": slot.kind,
            "slot_kind": slot.kind,
            "slot_state": slot.state,
            "session_lifecycle": cycle,
            "cleanup_pending": bool(pending),
            "hard_expires_at": cycle["hard_expires_at"],
        }
        if (
            not pending
            and current
            and current["state"] == "active"
            and current["cycle_started_at"] == cycle["started_at"]
            and cycle["state"] in {"active", "warning", "draining"}
        ):
            identity.update(
                work_session_id=current["work_session_id"], session_epoch=current["session_epoch"]
            )
        return identity

    def local_identity(self, slot: SlotSnapshot, *, now: datetime | None = None) -> dict:
        """Materialize one bounded local cycle. No network, grants, or raw metadata."""
        now = _utc(now or datetime.now(UTC))
        stamp = _text(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            if row is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            slot = self._snapshot(row, _connection=db)
            agent_id = slot.session.logical_agent_id if slot.session is not None else None
            cycle = self.merged_cycle(slot, now)
            current = db.execute(
                """SELECT l.*,s.state FROM access_mesh_local_sessions l
                JOIN logical_agent_work_sessions s USING(work_session_id)
                WHERE l.issuer_id=? AND l.slot_id=?""",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            live = cycle["state"] in {"active", "warning", "draining"}
            same = (
                current
                and current["state"] == "active"
                and live
                and current["cycle_started_at"] == cycle["started_at"]
            )
            if current and current["state"] == "active" and not same:
                self._queue_cleanup(
                    db,
                    slot,
                    job_id=f"expiry:{current['work_session_id']}",
                    reason="local_cycle_ended",
                    stamp=stamp,
                    release_claims=self._release_at_end(slot),
                )
            pending = db.execute(
                "SELECT 1 FROM access_mesh_cleanup_jobs WHERE logical_agent_id=? "
                "AND completed_at IS NULL LIMIT 1",
                (agent_id,),
            ).fetchone()
            identity = {
                "ok": True,
                "issuer_node_id": slot.issuer_id,
                "slot_id": slot.slot_id,
                "logical_agent_id": agent_id,
                "authority_node_id": self.local_node_id,
                "public_name": self.human_name(slot),
                "mode": slot.kind,
                "slot_kind": slot.kind,
                "slot_state": slot.state,
                "session_lifecycle": cycle,
                "cleanup_pending": bool(pending),
                "hard_expires_at": cycle["hard_expires_at"],
            }
            if pending or not live:
                return identity
            if same:
                session_id, epoch = current["work_session_id"], current["session_epoch"]
                db.execute(
                    # SQLite fires AFTER UPDATE triggers even for identical values.
                    # Avoid unbounded duplicate instance_events on each Mesh read.
                    "UPDATE logical_agent_work_sessions SET hard_expires_at=? "
                    "WHERE work_session_id=? AND hard_expires_at IS NOT ?",
                    (cycle["hard_expires_at"], session_id, cycle["hard_expires_at"]),
                )
            else:
                epoch = db.execute(
                    "SELECT COALESCE(MAX(session_epoch),0)+1 "
                    "FROM logical_agent_work_sessions WHERE logical_agent_id=?",
                    (slot.session.logical_agent_id,),
                ).fetchone()[0]
                session_id = "wm_" + secrets.token_urlsafe(24)
                db.execute(
                    """INSERT INTO logical_agent_work_sessions
                    (work_session_id,logical_agent_id,session_epoch,authority_node_id,
                     authority_epoch,started_at,hard_expires_at,auth_principal_id,
                     auth_generation,state,origin_instance_id)
                    VALUES(?,?,?,?,1,?,?,NULL,1,'active',?)""",
                    (
                        session_id,
                        slot.session.logical_agent_id,
                        epoch,
                        self.local_node_id,
                        cycle["started_at"],
                        cycle["hard_expires_at"],
                        slot.issuer_id,
                    ),
                )
                db.execute(
                    """INSERT INTO access_mesh_local_sessions
                    (issuer_id,slot_id,work_session_id,cycle_started_at,session_epoch)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(issuer_id,slot_id) DO UPDATE SET
                    work_session_id=excluded.work_session_id,cycle_started_at=excluded.cycle_started_at,
                    session_epoch=excluded.session_epoch,current_intent=NULL,
                    intent_updated_at=NULL,intent_warned_at=NULL""",
                    (slot.issuer_id, slot.slot_id, session_id, cycle["started_at"], epoch),
                )
            db.execute(
                # Materialize only a real state change; the prior unconditional
                # timestamp refresh triggered redundant logical_agent.changed
                # events on every read of an already-active cycle.
                "UPDATE logical_agents SET state='active',updated_at=? "
                "WHERE logical_agent_id=? AND state IS NOT 'active'",
                (stamp, slot.session.logical_agent_id),
            )
            return {**identity, "work_session_id": session_id, "session_epoch": epoch}

    def issuer_slots(self, *, after: int = 0, limit: int = 21) -> list[tuple[int, SlotSnapshot]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.sequence,s.* FROM access_mesh_slot_catalog c "
                "JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id) "
                "WHERE c.sequence>? ORDER BY c.sequence LIMIT ?",
                (after, limit),
            ).fetchall()
        return [(row["sequence"], self._snapshot(row)) for row in rows]

    def defaults(self) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_operator_defaults WHERE singleton=1"
            ).fetchone()
        return (
            {
                "revision": row["revision"],
                "policy": json.loads(row["policy_json"]),
                "legacy_admission_enabled": bool(row["legacy_admission_enabled"]),
                "inherit_policy": bool(row["inherit_policy"]),
                "parent": json.loads(row["parent_json"]) if row["parent_json"] else None,
            }
            if row
            else None
        )

    def change_defaults(
        self,
        *,
        expected_revision,
        policy,
        receipt,
        inherit_policy=False,
        parent=None,
        legacy_admission_enabled=False,
    ):
        from terminal_mcp.storage.access_policy_inheritance import change_defaults

        return change_defaults(
            self,
            expected_revision=expected_revision,
            policy=policy,
            receipt=receipt,
            inherit_policy=inherit_policy,
            parent=parent,
            legacy_admission_enabled=legacy_admission_enabled,
        )

    def sync_parent_defaults(self, parent):
        from terminal_mcp.storage.access_policy_inheritance import sync_parent_defaults

        return sync_parent_defaults(self, parent)

    def latest_session_window(self, slot_id: str):
        """Return the latest explicit window anchor/deadline and its event revision.

        Consult the append-only event log: SessionEnded changes the slot anchor
        to a cooldown marker, so the original hard deadline is no longer on the
        live slot projection.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT revision,event_json FROM access_mesh_event_log "
                "WHERE slot_id=? AND "
                "json_extract(event_json,'$.kind') IN "
                "('SlotIssued','SessionStarted','SessionUpdated') "
                "ORDER BY revision DESC LIMIT 1",
                (slot_id,),
            ).fetchone()
        if row is None:
            return None
        event = json.loads(row[1])
        start = _parse(event.get("effective_at"))
        if start is None:
            return None
        end = _parse(event.get("deadline_at"))
        return {"revision": int(row[0]), "start": start, "deadline": end}

    def latest_session_end(self, slot_id: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT revision,event_json FROM access_mesh_event_log "
                "WHERE slot_id=? AND "
                "json_extract(event_json,'$.kind')='SessionEnded' "
                "ORDER BY revision DESC LIMIT 1",
                (slot_id,),
            ).fetchone()
        if row is None:
            return None
        return {"revision": int(row[0]), "ended_at": _parse(json.loads(row[1])["effective_at"])}

    def last_session_end(self, slot_id: str) -> datetime | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT event_json FROM access_mesh_event_log WHERE issuer_id=? "
                "AND slot_id=? AND json_extract(event_json,'$.kind')='SessionEnded' "
                "ORDER BY revision DESC LIMIT 1",
                (self.local_node_id, slot_id),
            ).fetchone()
        return _parse(json.loads(row[0])["effective_at"]) if row else None

    def initialize_command_journal(self) -> None:
        # A monotonic side index gives stable local cursors across VACUUM and
        # command retention. The command hash is the durable journal identity.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_command_journal "
                "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, command_hash TEXT UNIQUE NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_command_journal(command_hash) "
                "SELECT hash FROM commands ORDER BY COALESCE(enqueued_at,started_at,''),hash"
            )
            db.execute("""CREATE TRIGGER IF NOT EXISTS access_mesh_command_journal_insert
                AFTER INSERT ON commands BEGIN
                INSERT OR IGNORE INTO access_mesh_command_journal(command_hash) VALUES(NEW.hash);
                END""")

    def command_journal(self, *, after: int = 0, limit: int = 20) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 101:
            raise AccessMeshError("access_mesh_invalid_limit")
        with self._connect() as db:
            rows = db.execute(
                """SELECT j.sequence,c.hash AS cmd_hash,
                COALESCE(c.enqueued_at,a.created_at,c.started_at) AS created_at,
                COALESCE(l.display_name,a.agent_id) AS actor,a.logical_agent_id,
                m.issuer_id AS issuer_node_id,c.status,c.queue_id,
                c.started_at,c.finished_at,c.exit_code
                FROM access_mesh_command_journal j JOIN commands c ON c.hash=j.command_hash
                LEFT JOIN command_agent_attribution a ON a.command_hash=c.hash
                LEFT JOIN logical_agents l ON l.logical_agent_id=a.logical_agent_id
                LEFT JOIN access_mesh_sessions m ON m.logical_agent_id=a.logical_agent_id
                WHERE j.sequence>? ORDER BY j.sequence LIMIT ?""",
                (after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_cleanup(
        self, *, limit: int = 50, logical_agent_id: str | None = None
    ) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessMeshError("access_mesh_invalid_limit")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM access_mesh_cleanup_jobs WHERE completed_at IS NULL "
                + ("AND logical_agent_id=? " if logical_agent_id else "")
                + "ORDER BY created_at,job_id LIMIT ?",
                (logical_agent_id, limit) if logical_agent_id else (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_cleanup(self, job_id: str, *, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE access_mesh_cleanup_jobs SET attempts=attempts+1,last_error=?,"
                "completed_at=? WHERE job_id=? AND completed_at IS NULL",
                (
                    error[:2000] if error else None,
                    None if error else _text(datetime.now(UTC)),
                    job_id,
                ),
            )

    def snapshot_page(self, *, issuer_id: str, after: str = "", limit: int = 20) -> list[dict]:
        if issuer_id != self.local_node_id or type(limit) is not int or not 1 <= limit <= 50:
            raise AccessMeshError("access_mesh_invalid_snapshot_request")
        with self._connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE (? OR issuer_id=?) "
                "AND slot_id>? ORDER BY slot_id LIMIT ?",
                (
                    bool(
                        self.registry_mesh_id and self.registry_authority_id == self.local_node_id
                    ),
                    issuer_id,
                    after,
                    limit,
                ),
            ).fetchall()
            return [self.snapshot_wire(row, _connection=db) for row in rows]

    def snapshot_wire(self, row, *, _connection=None) -> dict:
        """Project a stored slot through the shared replication wire contract."""
        item = dict(row)
        session = self._snapshot(row, _connection=_connection).session
        item["session"] = session.to_wire() if session is not None else None
        # Generation is issuer-local public vault metadata, not a bearer proof.
        item.pop("code_generation", None)
        if not item.get("inherit_policy"):
            item.pop("inherit_policy", None)
        if item.get("policy_window_json") is None:
            item.pop("policy_window_json", None)
        return item

    def apply_snapshot(self, raw: dict, *, authenticated_peer_id: str) -> str:
        fields = {
            "issuer_id",
            "slot_id",
            "session",
            "created_at",
            "last_used_at",
            "kind",
            "state",
            "revision",
            "event_id",
            "policy_json",
            "code_tag",
            "anchor",
        }
        optional = {
            "deadline_at",
            "fence_revision",
            "agent_name",
            "display_suffix",
            "policy_window_json",
            "inherit_policy",
        }
        if not isinstance(raw, dict) or not fields <= set(raw) or set(raw) - fields - optional:
            raise AccessMeshError("access_mesh_invalid_snapshot")
        raw = {
            "deadline_at": None,
            "fence_revision": raw["revision"],
            "agent_name": None,
            "display_suffix": None,
            "policy_window_json": None,
            "inherit_policy": 0,
            **raw,
        }
        fields |= optional
        if (
            authenticated_peer_id != raw["issuer_id"]
            and authenticated_peer_id != self.registry_authority_id
        ) or authenticated_peer_id not in self.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_issuer")
        try:
            from terminal_mcp.core.access_mesh_grants import SlotPolicy

            policy = SlotPolicy(**json.loads(raw["policy_json"]))
            session = (
                SessionSnapshot.from_wire(raw["session"]) if raw["session"] is not None else None
            )
            _utc(_parse(raw["created_at"]))
            if raw["last_used_at"] is not None:
                _utc(_parse(raw["last_used_at"]))
            if raw["anchor"] is not None and session is None:
                raise ValueError("a started window requires a session")
            if type(raw["inherit_policy"]) is not int or raw["inherit_policy"] not in {0, 1}:
                raise ValueError("invalid inheritance flag")
            if raw["policy_window_json"] is not None:
                from terminal_mcp.core.access_mesh_grants import CapturedAccessWindow

                CapturedAccessWindow.from_wire(json.loads(raw["policy_window_json"]))
            if (
                type(raw["fence_revision"]) is not int
                or not 1 <= raw["fence_revision"] <= raw["revision"]
            ):
                raise ValueError("invalid fence revision")
            if raw["deadline_at"] is not None and (
                raw["anchor"] is None
                or _utc(_parse(raw["deadline_at"])) <= _utc(_parse(raw["anchor"]))
            ):
                raise ValueError("invalid deadline override")
            event = AccessSlotEvent(
                issuer_id=raw["issuer_id"],
                slot_id=raw["slot_id"],
                logical_agent_id=session.logical_agent_id if session is not None else None,
                revision=raw["revision"],
                event_id=raw["event_id"],
                kind="SlotIssued",
                policy=policy,
                code_tag=raw["code_tag"],
                effective_at=_parse(raw["anchor"]),
                agent_name=raw["agent_name"],
                display_suffix=raw["display_suffix"],
                created_at=_parse(raw["created_at"]),
            )
            if raw["state"] not in {"active", "suspended"} or raw["kind"] not in {
                "temporary",
                "persistent",
            }:
                raise ValueError("invalid state or slot kind")
        except (ValueError, TypeError, KeyError) as exc:
            raise AccessMeshError("access_mesh_invalid_snapshot") from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_registry_ready(db)
            self.assert_registry_writer(db, authenticated_peer_id)
            old = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (event.issuer_id, event.slot_id),
            ).fetchone()
            if old is None:
                deleted = db.execute(
                    "SELECT revision FROM access_mesh_event_log WHERE issuer_id=? AND slot_id=? "
                    "AND json_extract(event_json,'$.kind')='SlotDeleted' ORDER BY revision "
                    "DESC LIMIT 1",
                    (event.issuer_id, event.slot_id),
                ).fetchone()
                if deleted is not None:
                    if event.revision <= deleted[0]:
                        return "stale"
                    raise AccessMeshError("access_mesh_unknown_slot")
            if old:
                previous = self._snapshot(old, _connection=db)
                if old["kind"] != raw["kind"] or (
                    previous.session is not None
                    and (
                        session is None
                        or previous.session.logical_agent_id != session.logical_agent_id
                    )
                ):
                    raise AccessMeshError("access_mesh_identity_conflict")
                if old["revision"] > event.revision:
                    return "stale"
                if old["revision"] == event.revision:
                    comparable = fields - {"session", "last_used_at"}
                    if (
                        any(old[key] != raw[key] for key in comparable)
                        or previous.session != session
                    ):
                        raise AccessMeshError("access_mesh_event_conflict")
                    if raw["last_used_at"] is not None:
                        db.execute(
                            "UPDATE access_mesh_slot_replicas SET "
                            "last_used_at=MAX(COALESCE(last_used_at,''),?) "
                            "WHERE issuer_id=? AND slot_id=?",
                            (raw["last_used_at"], event.issuer_id, event.slot_id),
                        )
                    return "duplicate"
                if old["last_used_at"] is not None:
                    raw["last_used_at"] = max(old["last_used_at"], raw["last_used_at"] or "")
            if old:
                self._before_slot_update(db, old, state=raw["state"], code_tag=raw["code_tag"])
            columns = tuple(sorted(fields - {"session"}))
            db.execute(
                f"INSERT INTO access_mesh_slot_replicas ({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)}) ON CONFLICT(issuer_id,slot_id) "
                "DO UPDATE SET "
                + ",".join(
                    f"{key}=excluded.{key}"
                    for key in columns
                    if key not in {"issuer_id", "slot_id"}
                ),
                tuple(raw[key] for key in columns),
            )
            if session is not None:
                db.execute(
                    "INSERT INTO access_mesh_sessions VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(issuer_id,slot_id) DO UPDATE SET "
                    "active=excluded.active,ended_at=excluded.ended_at,active_from=excluded.active_from",
                    (
                        event.issuer_id,
                        event.slot_id,
                        session.logical_agent_id,
                        int(session.active),
                        _text(session.ended_at) if session.ended_at else None,
                        _text(session.active_from) if session.active_from else None,
                    ),
                )
            slot = self._snapshot(raw, _connection=db)
            stamp = _text(self.clock())
            self._native_slot(db, slot, stamp)
            if old:
                self._reconcile_local_tx(
                    db,
                    slot,
                    stamp=stamp,
                    job_id=f"snapshot:{slot.issuer_id}:{slot.slot_id}:{slot.revision}",
                    force_fence=old["fence_revision"] != slot.fence_revision,
                )
            if slot.state != "active":
                self._queue_cleanup(
                    db,
                    slot,
                    job_id=f"snapshot:{slot.issuer_id}:{slot.slot_id}:{slot.revision}",
                    reason="snapshot_catchup",
                    stamp=stamp,
                    release_claims=self._release_at_end(slot),
                )
        return "applied"

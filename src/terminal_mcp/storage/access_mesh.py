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
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    LocalAccessMesh,
    SlotSnapshot,
    _parse,
    _text,
    _utc,
)

_mutation_receipt: ContextVar[dict | None] = ContextVar("mesh_issuer_receipt", default=None)


def public_name(issuer_id: str, logical_agent_id: str) -> str:
    digest = hashlib.sha256(f"{issuer_id}:{logical_agent_id}".encode()).hexdigest()[:12]
    return f"{issuer_id[:40]}-{digest}"


def local_cycle(slot: SlotSnapshot, now: datetime) -> dict:
    now = _utc(now)
    base = {
        "state": slot.state,
        "started_at": None,
        "hard_expires_at": None,
        "remaining_seconds": 0,
        "rearm_at": None,
    }
    if slot.state != "active" or slot.anchor is None:
        return {**base, "state": slot.state if slot.state != "active" else "idle"}
    anchor = _utc(slot.anchor)
    if now < anchor:
        return {**base, "state": "cooldown", "rearm_at": _text(anchor)}
    policy = slot.policy
    start = anchor
    end = (
        _utc(slot.deadline_at)
        if slot.deadline_at
        else anchor + timedelta(seconds=policy.duration_seconds)
    )
    if now >= end:
        if not policy.rearm_enabled:
            return {
                **base,
                "state": "expired",
                "started_at": _text(start),
                "hard_expires_at": _text(end),
            }
        next_start = end + timedelta(seconds=policy.cooldown_seconds)
        if now < next_start:
            return {
                **base,
                "state": "cooldown",
                "started_at": _text(start),
                "hard_expires_at": _text(end),
                "rearm_at": _text(next_start),
            }
        period = policy.duration_seconds + policy.cooldown_seconds
        index = int((now - next_start).total_seconds() // period)
        start = next_start + timedelta(seconds=index * period)
        end = start + timedelta(seconds=policy.duration_seconds)
    base.update(started_at=_text(start), hard_expires_at=_text(end))
    if now >= end:
        return {
            **base,
            "state": "cooldown",
            "rearm_at": _text(end + timedelta(seconds=policy.cooldown_seconds)),
        }
    remaining = (end - now).total_seconds()
    phase = "active"
    if policy.draining_seconds and remaining <= policy.draining_seconds:
        phase = "draining"
    elif policy.warning_seconds and remaining <= policy.warning_seconds:
        phase = "warning"
    return {**base, "state": phase, "remaining_seconds": max(0, int(remaining))}


class AccessMeshStore(LocalAccessMesh):
    """Native storage adapter; construct after the parent directory exists."""

    def __init__(self, *args, clock=None, **kwargs):
        self.clock = clock or (lambda: datetime.now(UTC))
        super().__init__(*args, **kwargs)

    def _initialize(self) -> None:
        super()._initialize()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS access_mesh_local_sessions (
                issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                work_session_id TEXT NOT NULL, cycle_started_at TEXT NOT NULL,
                session_epoch INTEGER NOT NULL,
                PRIMARY KEY(issuer_id,slot_id)
            ) WITHOUT ROWID""")
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
                policy_json TEXT NOT NULL, legacy_enabled INTEGER NOT NULL
            )""")
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
                ON access_mesh_slot_replicas(issuer_id,code_tag) WHERE state!='deleted'""")

    @staticmethod
    def _release_at_end(slot: SlotSnapshot) -> bool:
        return slot.kind == "legacy" or slot.policy.release_on_end is True

    def _native_slot(self, db, slot: SlotSnapshot, stamp: str) -> None:
        existing = db.execute(
            "SELECT authority_node_id FROM logical_agents WHERE logical_agent_id=?",
            (slot.logical_agent_id,),
        ).fetchone()
        conflicting = db.execute(
            "SELECT 1 FROM access_mesh_slot_replicas WHERE logical_agent_id=? "
            "AND (issuer_id!=? OR slot_id!=?)",
            (slot.logical_agent_id, slot.issuer_id, slot.slot_id),
        ).fetchone()
        owner = db.execute(
            "SELECT issuer_id,slot_id FROM access_mesh_native_owners WHERE logical_agent_id=?",
            (slot.logical_agent_id,),
        ).fetchone()
        if (
            conflicting
            or (existing and existing[0] != self.local_node_id)
            or (existing and owner is None)
            or (owner and tuple(owner) != (slot.issuer_id, slot.slot_id))
        ):
            raise AccessMeshError("access_mesh_identity_conflict")
        db.execute(
            "INSERT INTO access_mesh_native_owners VALUES(?,?,?) "
            "ON CONFLICT(logical_agent_id) DO NOTHING",
            (slot.logical_agent_id, slot.issuer_id, slot.slot_id),
        )
        db.execute(
            """INSERT INTO logical_agents (
            logical_agent_id,display_name,state,authority_node_id,authority_epoch,
            slot_revision,selector_generation,auth_generation,created_at,updated_at)
            VALUES(?,?,'armed',?,1,1,1,1,?,?) ON CONFLICT(logical_agent_id) DO NOTHING""",
            (
                slot.logical_agent_id,
                public_name(slot.issuer_id, slot.logical_agent_id),
                self.local_node_id,
                stamp,
                stamp,
            ),
        )
        if slot.state in {"deleted", "suspended"}:
            db.execute(
                "UPDATE logical_agents SET state=?,updated_at=?,deleted_at=? "
                "WHERE logical_agent_id=?",
                (
                    slot.state,
                    stamp,
                    stamp if slot.state == "deleted" else None,
                    slot.logical_agent_id,
                ),
            )

    def _queue_cleanup(
        self, db, slot: SlotSnapshot, *, job_id: str, reason: str, stamp: str, release_claims: bool
    ) -> None:
        current = db.execute(
            "SELECT * FROM access_mesh_local_sessions WHERE issuer_id=? AND slot_id=?",
            (slot.issuer_id, slot.slot_id),
        ).fetchone()
        session_id = current["work_session_id"] if current else None
        epoch = current["session_epoch"] if current else None
        if session_id:
            db.execute(
                "UPDATE logical_agent_work_sessions SET state='ended',ended_at=?,"
                "end_reason=? WHERE work_session_id=? AND state IN ('active','stopping')",
                (stamp, reason, session_id),
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
                slot.logical_agent_id,
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
        slot = self._snapshot(row)
        stamp = _text(self.clock())
        self._native_slot(db, slot, stamp)
        receipt = _mutation_receipt.get()
        if receipt is not None and receipt["event_id"] == event.event_id:
            self._save_receipt_tx(db, receipt)
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
                release_claims=slot.state == "deleted" or self._release_at_end(slot),
            )

    def apply_event(self, event, *, mutation_receipt=None, **kwargs):
        token = _mutation_receipt.set(mutation_receipt)
        try:
            try:
                return super().apply_event(event, **kwargs)
            except sqlite3.IntegrityError as exc:
                if event.code_tag is not None:
                    with self._connect() as db:
                        duplicate = db.execute(
                            "SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? "
                            "AND code_tag=? AND state!='deleted'",
                            (event.issuer_id, event.code_tag),
                        ).fetchone()
                    if duplicate is not None:
                        raise AccessMeshError("access_mesh_code_in_use") from exc
                raise
        finally:
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
        tag = self.code_tag(issuer_id, code)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas "
                "WHERE issuer_id=? AND code_tag=? AND state!='deleted'",
                (issuer_id, tag),
            ).fetchone()
        return self._snapshot(row) if row else None

    def slot_for_agent(self, logical_agent_id: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE logical_agent_id=?",
                (logical_agent_id,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def attached_slot(self, connection_key: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT s.* FROM access_mesh_attachments a
                JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id)
                WHERE a.connection_key=?""",
                (connection_key,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def attach(self, *, issuer_id: str, code: str, connection_key: str) -> SlotSnapshot:
        slot = super().attach(issuer_id=issuer_id, code=code, connection_key=connection_key)
        self.touch(connection_key)
        return slot

    def touch(self, connection_key: str, *, now: datetime | None = None) -> None:
        stamp = _text(now or datetime.now(UTC))
        with self._connect() as db:
            db.execute(
                """INSERT INTO access_mesh_activity
                (connection_key,attached_at,last_active_at)
                SELECT connection_key,?,? FROM access_mesh_attachments WHERE connection_key=?
                ON CONFLICT(connection_key) DO UPDATE SET last_active_at=excluded.last_active_at""",
                (stamp, stamp, connection_key),
            )

    def local_slots(self, *, after: str = "", limit: int = 50) -> list[SlotSnapshot]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessMeshError("access_mesh_invalid_limit")
        with self._connect() as db:
            rows = db.execute(
                """SELECT DISTINCT s.* FROM access_mesh_slot_replicas s
                JOIN access_mesh_attachments a USING(issuer_id,slot_id)
                WHERE s.logical_agent_id>? ORDER BY s.logical_agent_id LIMIT ?""",
                (after, limit),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

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
            slot = self._snapshot(row)
            cycle = local_cycle(slot, now)
            current = db.execute(
                "SELECT l.*,s.state FROM access_mesh_local_sessions l "
                "JOIN logical_agent_work_sessions s USING(work_session_id) "
                "WHERE l.issuer_id=? AND l.slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            pending = db.execute(
                "SELECT 1 FROM access_mesh_cleanup_jobs WHERE logical_agent_id=? "
                "AND completed_at IS NULL LIMIT 1",
                (slot.logical_agent_id,),
            ).fetchone()
        identity = {
            "ok": True,
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "logical_agent_id": slot.logical_agent_id,
            "authority_node_id": self.local_node_id,
            "public_name": public_name(slot.issuer_id, slot.logical_agent_id),
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
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (slot.issuer_id, slot.slot_id),
            ).fetchone()
            if row is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            slot = self._snapshot(row)
            cycle = local_cycle(slot, now)
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
                (slot.logical_agent_id,),
            ).fetchone()
            identity = {
                "ok": True,
                "issuer_node_id": slot.issuer_id,
                "slot_id": slot.slot_id,
                "logical_agent_id": slot.logical_agent_id,
                "authority_node_id": self.local_node_id,
                "public_name": public_name(slot.issuer_id, slot.logical_agent_id),
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
                    "UPDATE logical_agent_work_sessions SET hard_expires_at=? "
                    "WHERE work_session_id=?",
                    (cycle["hard_expires_at"], session_id),
                )
            else:
                epoch = db.execute(
                    "SELECT COALESCE(MAX(session_epoch),0)+1 "
                    "FROM logical_agent_work_sessions WHERE logical_agent_id=?",
                    (slot.logical_agent_id,),
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
                        slot.logical_agent_id,
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
                    session_epoch=excluded.session_epoch""",
                    (slot.issuer_id, slot.slot_id, session_id, cycle["started_at"], epoch),
                )
            db.execute(
                "UPDATE logical_agents SET state='active',updated_at=? WHERE logical_agent_id=?",
                (stamp, slot.logical_agent_id),
            )
            return {**identity, "work_session_id": session_id, "session_epoch": epoch}

    def issuer_slots(self, *, after: int = 0, limit: int = 21) -> list[tuple[int, SlotSnapshot]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.sequence,s.* FROM access_mesh_slot_catalog c "
                "JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id) "
                "WHERE c.issuer_id=? AND c.sequence>? ORDER BY c.sequence LIMIT ?",
                (self.local_node_id, after, limit),
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
                "legacy_enabled": bool(row["legacy_enabled"]),
            }
            if row
            else None
        )

    def change_defaults(self, *, expected_revision, policy, legacy_enabled, receipt):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM access_mesh_operator_defaults WHERE singleton=1"
            ).fetchone()
            revision = row[0] if row else 1
            if expected_revision != revision:
                raise AccessMeshError("revision_conflict")
            db.execute(
                "INSERT INTO access_mesh_operator_defaults VALUES(1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,"
                "policy_json=excluded.policy_json,legacy_enabled=excluded.legacy_enabled",
                (revision + 1, json.dumps(policy, sort_keys=True), int(legacy_enabled)),
            )
            self._save_receipt_tx(db, receipt)

    def latest_session_window(self, slot_id: str):
        """Return the latest explicit window anchor/deadline and its event revision.

        Consult the append-only event log: SessionEnded changes the slot anchor
        to a cooldown marker, so the original hard deadline is no longer on the
        live slot projection.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT revision,event_json FROM access_mesh_event_log "
                "WHERE issuer_id=? AND slot_id=? AND "
                "json_extract(event_json,'$.kind') IN "
                "('SlotIssued','SessionStarted','SessionUpdated') "
                "ORDER BY revision DESC LIMIT 1",
                (self.local_node_id, slot_id),
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
                "WHERE issuer_id=? AND slot_id=? AND "
                "json_extract(event_json,'$.kind')='SessionEnded' "
                "ORDER BY revision DESC LIMIT 1",
                (self.local_node_id, slot_id),
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
                LEFT JOIN access_mesh_slot_replicas m ON m.logical_agent_id=a.logical_agent_id
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
            rows = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? "
                "AND slot_id>? ORDER BY slot_id LIMIT ?",
                (issuer_id, after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_snapshot(self, raw: dict, *, authenticated_peer_id: str) -> str:
        fields = {
            "issuer_id",
            "slot_id",
            "logical_agent_id",
            "kind",
            "state",
            "revision",
            "event_id",
            "policy_json",
            "code_tag",
            "anchor",
        }
        if not isinstance(raw, dict) or set(raw) not in (
            fields,
            fields | {"deadline_at", "fence_revision"},
        ):
            raise AccessMeshError("access_mesh_invalid_snapshot")
        raw = {"deadline_at": None, "fence_revision": raw["revision"], **raw}
        fields |= {"deadline_at", "fence_revision"}
        if (
            authenticated_peer_id != raw["issuer_id"]
            or authenticated_peer_id not in self.trusted_issuers
        ):
            raise AccessMeshError("access_mesh_untrusted_issuer")
        try:
            from terminal_mcp.core.access_mesh_grants import SlotPolicy

            policy = SlotPolicy(**json.loads(raw["policy_json"]))
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
                logical_agent_id=raw["logical_agent_id"],
                revision=raw["revision"],
                event_id=raw["event_id"],
                kind="SlotIssued",
                policy=policy,
                code_tag=raw["code_tag"],
                effective_at=_parse(raw["anchor"]),
            )
            if raw["state"] not in {"active", "suspended", "deleted"} or raw["kind"] not in {
                "legacy",
                "persistent",
            }:
                raise ValueError("invalid state or slot kind")
        except (ValueError, TypeError, KeyError) as exc:
            raise AccessMeshError("access_mesh_invalid_snapshot") from exc
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (event.issuer_id, event.slot_id),
            ).fetchone()
            if old:
                if old["logical_agent_id"] != event.logical_agent_id or old["kind"] != raw["kind"]:
                    raise AccessMeshError("access_mesh_identity_conflict")
                if old["revision"] > event.revision:
                    return "stale"
                if old["revision"] == event.revision:
                    if dict(old) != raw:
                        raise AccessMeshError("access_mesh_event_conflict")
                    return "duplicate"
                if old["state"] == "deleted" and raw["state"] != "deleted":
                    raise AccessMeshError("access_mesh_deleted_slot")
            columns = tuple(sorted(fields))
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
            slot = self._snapshot(raw)
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
                    release_claims=slot.state == "deleted" or self._release_at_end(slot),
                )
            if slot.state == "deleted":
                db.execute(
                    "DELETE FROM access_mesh_attachments WHERE issuer_id=? AND slot_id=?",
                    (slot.issuer_id, slot.slot_id),
                )
        return "applied"

"""Durable access replicas connected to native command/session storage.

Issuer identity is global. WorkSession authority and every command remain local.
Fencing/claim cleanup is a durable job committed with the triggering transition.
A new cycle waits for cleanup, preventing delayed release of a newer claim.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from terminal_mcp.core.access_mesh_grants import (
    AccessMeshError,
    AccessSlotEvent,
    LocalAccessMesh,
    SlotSnapshot,
    _parse,
    _text,
    _utc,
)


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
    elapsed = (now - anchor).total_seconds()
    if not policy.rearm_enabled and elapsed >= policy.duration_seconds:
        return {**base, "state": "expired"}
    period = policy.duration_seconds + policy.cooldown_seconds
    index = int(elapsed // period) if policy.rearm_enabled else 0
    start = anchor + timedelta(seconds=index * period)
    end = start + timedelta(seconds=policy.duration_seconds)
    base.update(started_at=_text(start), hard_expires_at=_text(end))
    if now >= end:
        return {**base, "state": "cooldown", "rearm_at": _text(start + timedelta(seconds=period))}
    remaining = (end - now).total_seconds()
    phase = "active"
    if policy.draining_seconds and remaining <= policy.draining_seconds:
        phase = "draining"
    elif policy.warning_seconds and remaining <= policy.warning_seconds:
        phase = "warning"
    return {**base, "state": phase, "remaining_seconds": max(0, int(remaining))}


class AccessMeshStore(LocalAccessMesh):
    """Native storage adapter; construct after the parent directory exists."""

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
        if conflicting or (existing and existing[0] != self.local_node_id):
            raise AccessMeshError("access_mesh_identity_conflict")
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
        stamp = _text(datetime.now(UTC))
        self._native_slot(db, slot, stamp)
        if event.kind in {"SessionEnded", "SlotSuspended", "SlotDeleted"}:
            self._queue_cleanup(
                db,
                slot,
                job_id=f"event:{event.issuer_id}:{event.event_id}",
                reason=event.kind,
                stamp=stamp,
                release_claims=release_claims,
            )

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

    def pending_cleanup(self, *, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessMeshError("access_mesh_invalid_limit")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM access_mesh_cleanup_jobs WHERE completed_at IS NULL "
                "ORDER BY created_at,job_id LIMIT ?",
                (limit,),
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
        if not isinstance(raw, dict) or set(raw) != fields:
            raise AccessMeshError("access_mesh_invalid_snapshot")
        if (
            authenticated_peer_id != raw["issuer_id"]
            or authenticated_peer_id not in self.trusted_issuers
        ):
            raise AccessMeshError("access_mesh_untrusted_issuer")
        try:
            from terminal_mcp.core.access_mesh_grants import SlotPolicy

            policy = SlotPolicy(**json.loads(raw["policy_json"]))
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
            stamp = _text(datetime.now(UTC))
            self._native_slot(db, slot, stamp)
            # Missing intermediate events can include a stop/restart. Fence an old
            # local cycle before accepting a newer snapshot, including active ones.
            if old or slot.state != "active":
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

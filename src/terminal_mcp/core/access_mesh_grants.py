"""Locally evaluated multi-issuer Access grants.

This is the durable consumer-side domain primitive, deliberately independent from
HTTP/MCP routing and shell command execution. A trusted issuer publishes ordered
slot events; the recipient applies them once, binds connector identities via an
Access Code once, and checks only its local slot/session state for later writes.

A local binding is NOT a session.detach protocol. The issuer ends sessions or
the receiver enforces local deadlines/cooldown. There is no remote lookup here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

EventKind = Literal[
    "SlotIssued",
    "SlotPolicyChanged",
    "SlotSuspended",
    "SlotResumed",
    "SlotDeleted",
    "AccessCodeRotated",
    "SessionStarted",
    "SessionUpdated",
    "SessionEnded",
]

_EVENT_KINDS = frozenset(
    {
        "SlotIssued", "SlotPolicyChanged", "SlotSuspended", "SlotResumed",
        "SlotDeleted", "AccessCodeRotated", "SessionStarted", "SessionUpdated",
        "SessionEnded",
    }
)
_TAG_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class AccessMeshError(ValueError):
    """Stable domain error for consumers and eventually public adapters."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _required_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise AccessMeshError("access_mesh_invalid_identifier")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise AccessMeshError("access_mesh_invalid_identifier")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AccessMeshError("access_mesh_invalid_time")
    return value.astimezone(UTC)


def _text(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    duration_seconds: int
    cooldown_seconds: int = 0
    rearm_enabled: bool = True
    warning_seconds: int = 0
    draining_seconds: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.duration_seconds) is not int
            or self.duration_seconds < 1
            or type(self.cooldown_seconds) is not int
            or self.cooldown_seconds < 0
            or type(self.rearm_enabled) is not bool
            or type(self.warning_seconds) is not int
            or self.warning_seconds < 0
            or self.warning_seconds >= self.duration_seconds
            or type(self.draining_seconds) is not int
            or self.draining_seconds < 0
            or self.draining_seconds >= self.duration_seconds
        ):
            raise AccessMeshError("access_mesh_invalid_policy")

    def active_at(self, started_at: datetime | None, now: datetime) -> bool:
        """Compute the local WorkSession cycle without contacting the issuer."""
        if started_at is None:
            return False
        elapsed = (_utc(now) - _utc(started_at)).total_seconds()
        if elapsed < 0:
            return False
        if not self.rearm_enabled:
            return elapsed < self.duration_seconds
        cycle = self.duration_seconds + self.cooldown_seconds
        return elapsed % cycle < self.duration_seconds


@dataclass(frozen=True, slots=True)
class AccessSlotEvent:
    issuer_id: str
    slot_id: str
    logical_agent_id: str
    revision: int
    event_id: str
    kind: EventKind
    policy: SlotPolicy | None = None
    code_tag: str | None = None
    effective_at: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.issuer_id, self.slot_id, self.logical_agent_id, self.event_id):
            _required_identifier(value)
        if type(self.revision) is not int or self.revision < 1:
            raise AccessMeshError("access_mesh_invalid_revision")
        if self.kind not in _EVENT_KINDS:
            raise AccessMeshError("access_mesh_invalid_event")
        if self.code_tag is not None and not _TAG_PATTERN.fullmatch(self.code_tag):
            raise AccessMeshError("access_mesh_invalid_code_tag")
        if self.effective_at is not None:
            _utc(self.effective_at)
        if self.kind == "SlotIssued" and (self.policy is None or self.code_tag is None):
            raise AccessMeshError("access_mesh_incomplete_issue")
        if self.kind == "SlotPolicyChanged" and self.policy is None:
            raise AccessMeshError("access_mesh_missing_policy")
        if self.kind == "AccessCodeRotated" and self.code_tag is None:
            raise AccessMeshError("access_mesh_missing_code_tag")
        if self.kind in {"SessionStarted", "SessionUpdated", "SessionEnded"}:
            if self.effective_at is None:
                raise AccessMeshError("access_mesh_missing_time")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    outcome: Literal["applied", "duplicate", "stale"]
    release_claims: bool = False
    logical_agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class SlotSnapshot:
    issuer_id: str
    slot_id: str
    logical_agent_id: str
    kind: Literal["legacy", "persistent"]
    state: Literal["active", "suspended", "deleted"]
    revision: int
    policy: SlotPolicy
    anchor: datetime | None
    code_tag: str

    def writable_at(self, now: datetime) -> bool:
        return self.state == "active" and self.policy.active_at(self.anchor, now)


class LocalAccessMesh:
    """A durable projection. All write gates read *local* SQLite only.

    The proof key must be provisioned by the trusted mesh configuration. It is
    never generated per process, or old four-digit AC proofs would stop working.
    Actual inter-node delivery, outbox ACKs and issuer authority are higher layers.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        local_node_id: str,
        trusted_issuers: frozenset[str],
        proof_key: bytes,
    ) -> None:
        self.path = str(database_path)
        self.local_node_id = _required_identifier(local_node_id)
        if not trusted_issuers or not all(
            isinstance(i, str) and i == _required_identifier(i) for i in trusted_issuers
        ):
            raise AccessMeshError("access_mesh_invalid_issuers")
        self.trusted_issuers = frozenset(trusted_issuers)
        if not isinstance(proof_key, bytes) or len(proof_key) < 16:
            raise AccessMeshError("access_mesh_invalid_proof_key")
        self._proof_key = proof_key
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_slot_replicas (
                  issuer_id TEXT NOT NULL,
                  slot_id TEXT NOT NULL,
                  logical_agent_id TEXT NOT NULL,
                  kind TEXT NOT NULL CHECK(kind IN ('legacy','persistent')),
                  state TEXT NOT NULL CHECK(state IN ('active','suspended','deleted')),
                  revision INTEGER NOT NULL,
                  event_id TEXT NOT NULL,
                  policy_json TEXT NOT NULL,
                  code_tag TEXT NOT NULL,
                  anchor TEXT,
                  PRIMARY KEY(issuer_id,slot_id)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_attachments (
                  connection_key TEXT PRIMARY KEY,
                  issuer_id TEXT NOT NULL,
                  slot_id TEXT NOT NULL,
                  logical_agent_id TEXT NOT NULL,
                  FOREIGN KEY(issuer_id,slot_id)
                    REFERENCES access_mesh_slot_replicas(issuer_id,slot_id)
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS access_mesh_code_lookup
                ON access_mesh_slot_replicas(issuer_id,code_tag)
            """)

    def code_tag(self, issuer_id: str, code: str) -> str:
        """A mesh-keyed verifier, not the plaintext four-digit AC."""
        _required_identifier(issuer_id)
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}", code):
            raise AccessMeshError("access_mesh_invalid_code")
        return hmac.new(
            self._proof_key, f"terminal-mcp-ac-v1:{issuer_id}:{code}".encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SlotSnapshot:
        return SlotSnapshot(
            issuer_id=row["issuer_id"],
            slot_id=row["slot_id"],
            logical_agent_id=row["logical_agent_id"],
            kind=row["kind"],
            state=row["state"],
            revision=row["revision"],
            policy=SlotPolicy(**json.loads(row["policy_json"])),
            anchor=_parse(row["anchor"]),
            code_tag=row["code_tag"],
        )

    def slot(self, issuer_id: str, slot_id: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
        return self._snapshot(row) if row is not None else None

    def apply_event(
        self,
        event: AccessSlotEvent,
        *,
        authenticated_peer_id: str,
        issued_kind: Literal["legacy", "persistent"] | None = None,
    ) -> ApplyResult:
        """Process an issuer event once; out-of-order gaps demand snapshot/catchup.

        Transport must first authenticate the sending peer. An event claiming to
        come from another issuer cannot change this issuer's local replica.
        """
        if (
            authenticated_peer_id != event.issuer_id
            or event.issuer_id not in self.trusted_issuers
        ):
            raise AccessMeshError("access_mesh_untrusted_issuer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (event.issuer_id, event.slot_id),
            ).fetchone()
            if old is None:
                if event.kind != "SlotIssued" or issued_kind not in {"legacy", "persistent"}:
                    raise AccessMeshError("access_mesh_unknown_slot")
                if event.revision != 1:
                    raise AccessMeshError("access_mesh_event_gap")
                db.execute(
                    """INSERT INTO access_mesh_slot_replicas
                       (issuer_id,slot_id,logical_agent_id,kind,state,revision,
                        event_id,policy_json,code_tag,anchor)
                       VALUES(?,?,?,?,'active',?,?,?,?,?)""",
                    (
                        event.issuer_id, event.slot_id, event.logical_agent_id,
                        issued_kind, event.revision, event.event_id,
                        json.dumps(asdict(event.policy), sort_keys=True),
                        event.code_tag,
                        _text(event.effective_at) if event.effective_at else None,
                    ),
                )
                return ApplyResult("applied")
            if event.logical_agent_id != old["logical_agent_id"]:
                raise AccessMeshError("access_mesh_identity_conflict")
            if event.revision < old["revision"]:
                return ApplyResult("stale")
            if event.revision == old["revision"]:
                if event.event_id != old["event_id"]:
                    raise AccessMeshError("access_mesh_event_conflict")
                return ApplyResult("duplicate")
            if event.revision != old["revision"] + 1:
                raise AccessMeshError("access_mesh_event_gap")
            if old["state"] == "deleted":
                raise AccessMeshError("access_mesh_deleted_slot")

            state = old["state"]
            policy = SlotPolicy(**json.loads(old["policy_json"]))
            tag = old["code_tag"]
            anchor = old["anchor"]
            if event.kind == "SlotIssued":
                raise AccessMeshError("access_mesh_issue_replayed")
            if event.kind == "SlotPolicyChanged":
                policy = event.policy
            elif event.kind == "SlotSuspended":
                state = "suspended"
            elif event.kind == "SlotResumed":
                state = "active"
            elif event.kind == "SlotDeleted":
                state = "deleted"
            elif event.kind == "AccessCodeRotated":
                tag = event.code_tag
            elif event.kind in {"SessionStarted", "SessionUpdated"}:
                anchor = _text(event.effective_at)
            elif event.kind == "SessionEnded":
                anchor = (
                    _text(event.effective_at + timedelta(seconds=policy.cooldown_seconds))
                    if policy.rearm_enabled
                    else None
                )
            release_claims = (
                event.kind == "SlotDeleted"
                or (old["kind"] == "legacy" and event.kind in {
                    "SlotSuspended", "SessionEnded",
                })
            )
            db.execute(
                """UPDATE access_mesh_slot_replicas
                   SET state=?,revision=?,event_id=?,policy_json=?,code_tag=?,anchor=?
                   WHERE issuer_id=? AND slot_id=?""",
                (
                    state, event.revision, event.event_id,
                    json.dumps(asdict(policy), sort_keys=True), tag, anchor,
                    event.issuer_id, event.slot_id,
                ),
            )
            if event.kind == "SlotDeleted":
                db.execute(
                    "DELETE FROM access_mesh_attachments WHERE issuer_id=? AND slot_id=?",
                    (event.issuer_id, event.slot_id),
                )
        return ApplyResult(
            "applied", release_claims=release_claims,
            logical_agent_id=event.logical_agent_id if release_claims else None,
        )

    def attach(
        self,
        *,
        issuer_id: str,
        code: str,
        connection_key: str,
    ) -> SlotSnapshot:
        """Bind an MCP connector identity to a replicated grant exactly once.

        Subsequent writes use the binding and the local slot clock, never code.
        A ready-but-cooling-down slot can attach now and work after rearm.
        """
        _required_identifier(connection_key)
        tag = self.code_tag(issuer_id, code)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM access_mesh_slot_replicas
                   WHERE issuer_id=? AND code_tag=? AND state='active'""",
                (issuer_id, tag),
            ).fetchall()
            if len(rows) != 1:
                raise AccessMeshError(
                    "access_mesh_slot_not_found" if not rows else "access_mesh_ambiguous_code"
                )
            slot = self._snapshot(rows[0])
            old = db.execute(
                "SELECT issuer_id,slot_id FROM access_mesh_attachments WHERE connection_key=?",
                (connection_key,),
            ).fetchone()
            if old is not None and (
                old["issuer_id"] != issuer_id or old["slot_id"] != slot.slot_id
            ):
                raise AccessMeshError("access_mesh_binding_conflict")
            if old is None:
                db.execute(
                    """INSERT INTO access_mesh_attachments
                       (connection_key,issuer_id,slot_id,logical_agent_id)
                       VALUES(?,?,?,?)""",
                    (connection_key,issuer_id,slot.slot_id,slot.logical_agent_id),
                )
        return slot

    def may_write(self, *, connection_key: str, now: datetime) -> bool:
        """Fast local check: active slot and current WorkSession cycle only."""
        _required_identifier(connection_key)
        with self._connect() as db:
            row = db.execute(
                """SELECT s.* FROM access_mesh_attachments AS a
                   JOIN access_mesh_slot_replicas AS s
                     ON s.issuer_id=a.issuer_id AND s.slot_id=a.slot_id
                   WHERE a.connection_key=?""",
                (connection_key,),
            ).fetchone()
        return self._snapshot(row).writable_at(now) if row is not None else False

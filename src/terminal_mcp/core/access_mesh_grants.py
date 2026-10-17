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
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

# Keep aligned with storage.sqlite.SCHEMA_VERSION. The release fingerprint
# requires that storage constant to remain a literal integer.
ACCESS_MESH_SCHEMA_VERSION = 26

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
        "SlotIssued",
        "SlotPolicyChanged",
        "SlotSuspended",
        "SlotResumed",
        "SlotDeleted",
        "AccessCodeRotated",
        "SessionStarted",
        "SessionUpdated",
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
    release_on_end: bool | None = None

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
            or (self.release_on_end is not None and type(self.release_on_end) is not bool)
        ):
            raise AccessMeshError("access_mesh_invalid_policy")

    def active_at(
        self, started_at: datetime | None, now: datetime, deadline_at: datetime | None = None
    ) -> bool:
        """Compute the local WorkSession cycle without contacting the issuer."""
        if started_at is None:
            return False
        elapsed = (_utc(now) - _utc(started_at)).total_seconds()
        if elapsed < 0:
            return False
        first_duration = (
            (_utc(deadline_at) - _utc(started_at)).total_seconds()
            if deadline_at is not None
            else self.duration_seconds
        )
        if elapsed < first_duration:
            return True
        if not self.rearm_enabled:
            return False
        after_first = elapsed - first_duration - self.cooldown_seconds
        cycle = self.duration_seconds + self.cooldown_seconds
        return after_first >= 0 and after_first % cycle < self.duration_seconds


@dataclass(frozen=True, slots=True)
class CapturedAccessWindow:
    """Policy of the already-open window, separate from the next defaults."""

    policy: SlotPolicy
    started_at: datetime | None
    hard_expires_at: datetime | None
    rearm_at: datetime | None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SlotPolicy):
            raise AccessMeshError("access_mesh_invalid_policy_window")
        if (self.started_at is None) != (self.hard_expires_at is None):
            raise AccessMeshError("access_mesh_invalid_policy_window")
        for value in (self.started_at, self.hard_expires_at, self.rearm_at, self.closed_at):
            if value is not None:
                _utc(value)
        if self.started_at is not None and self.hard_expires_at < self.started_at:
            raise AccessMeshError("access_mesh_invalid_policy_window")
        if (
            self.hard_expires_at is not None
            and self.rearm_at is not None
            and self.rearm_at < (self.closed_at or self.hard_expires_at)
        ):
            raise AccessMeshError("access_mesh_invalid_policy_window")

    def to_wire(self) -> dict:
        return {
            "policy": asdict(self.policy),
            "started_at": _text(self.started_at) if self.started_at else None,
            "hard_expires_at": _text(self.hard_expires_at) if self.hard_expires_at else None,
            "rearm_at": _text(self.rearm_at) if self.rearm_at else None,
            **({"closed_at": _text(self.closed_at)} if self.closed_at is not None else {}),
        }

    @classmethod
    def from_wire(cls, value: dict) -> CapturedAccessWindow:
        if (
            not isinstance(value, dict)
            or not {"policy", "started_at", "hard_expires_at", "rearm_at"} <= set(value)
            or set(value) - {"policy", "started_at", "hard_expires_at", "rearm_at", "closed_at"}
        ):
            raise AccessMeshError("access_mesh_invalid_policy_window")
        try:
            if not isinstance(value["policy"], dict):
                raise ValueError("invalid policy")
            dates = []
            for key in ("started_at", "hard_expires_at", "rearm_at"):
                item = value[key]
                if item is not None and not isinstance(item, str):
                    raise ValueError("invalid timestamp")
                dates.append(datetime.fromisoformat(item) if item is not None else None)
            closed = value.get("closed_at")
            if closed is not None and not isinstance(closed, str):
                raise ValueError("invalid closed time")
            return cls(
                SlotPolicy(**value["policy"]),
                *dates,
                datetime.fromisoformat(closed) if closed is not None else None,
            )
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            raise AccessMeshError("access_mesh_invalid_policy_window") from exc


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
    deadline_at: datetime | None = None
    agent_name: str | None = None
    display_suffix: str | None = None
    policy_window: CapturedAccessWindow | None = None
    inherit_policy: bool | None = None

    def __post_init__(self) -> None:
        for value in (self.issuer_id, self.slot_id, self.logical_agent_id, self.event_id):
            _required_identifier(value)
        if type(self.revision) is not int or self.revision < 1:
            raise AccessMeshError("access_mesh_invalid_revision")
        if self.policy_window is not None and (
            self.kind != "SlotPolicyChanged"
            or not isinstance(self.policy_window, CapturedAccessWindow)
        ):
            raise AccessMeshError("access_mesh_invalid_policy_window")
        if self.inherit_policy is not None and (
            type(self.inherit_policy) is not bool
            or self.kind not in {"SlotIssued", "SlotPolicyChanged"}
        ):
            raise AccessMeshError("access_mesh_invalid_policy")
        if self.kind not in _EVENT_KINDS:
            raise AccessMeshError("access_mesh_invalid_event")
        if self.code_tag is not None and not _TAG_PATTERN.fullmatch(self.code_tag):
            raise AccessMeshError("access_mesh_invalid_code_tag")
        if self.effective_at is not None:
            _utc(self.effective_at)
        if self.deadline_at is not None:
            if (
                self.kind not in {"SessionStarted", "SessionUpdated"}
                or self.effective_at is None
                or _utc(self.deadline_at) <= _utc(self.effective_at)
            ):
                raise AccessMeshError("access_mesh_invalid_time")
        if self.agent_name is not None:
            from terminal_mcp.core.human_names import valid_agent_name

            if not valid_agent_name(self.agent_name):
                raise AccessMeshError("access_mesh_invalid_agent_name")
        if self.display_suffix is not None and (
            not isinstance(self.display_suffix, str) or len(self.display_suffix) > 80
        ):
            raise AccessMeshError("access_mesh_invalid_display_suffix")
        if self.kind != "SlotIssued" and (
            self.agent_name is not None or self.display_suffix is not None
        ):
            raise AccessMeshError("access_mesh_invalid_name_update")
        if self.kind == "SlotIssued" and (self.policy is None or self.code_tag is None):
            raise AccessMeshError("access_mesh_incomplete_issue")
        if self.kind == "SlotPolicyChanged" and self.policy is None:
            raise AccessMeshError("access_mesh_missing_policy")
        if self.kind == "AccessCodeRotated" and self.code_tag is None:
            raise AccessMeshError("access_mesh_missing_code_tag")
        if self.kind in {"SessionStarted", "SessionUpdated", "SessionEnded"}:
            if self.effective_at is None:
                raise AccessMeshError("access_mesh_missing_time")

    def to_wire(self) -> dict:
        """Bounded transport representation; never includes the raw Access Code."""
        return {
            "issuer_id": self.issuer_id,
            "slot_id": self.slot_id,
            "logical_agent_id": self.logical_agent_id,
            "revision": self.revision,
            "event_id": self.event_id,
            "kind": self.kind,
            "policy": asdict(self.policy) if self.policy is not None else None,
            "code_tag": self.code_tag,
            "effective_at": _text(self.effective_at) if self.effective_at else None,
            **(
                {"policy_window": self.policy_window.to_wire()}
                if self.policy_window is not None
                else {}
            ),
            **({"inherit_policy": self.inherit_policy} if self.inherit_policy is not None else {}),
            **({"deadline_at": _text(self.deadline_at)} if self.deadline_at else {}),
            **({"agent_name": self.agent_name} if self.agent_name is not None else {}),
            **({"display_suffix": self.display_suffix} if self.display_suffix is not None else {}),
        }

    @classmethod
    def from_wire(cls, data: dict) -> AccessSlotEvent:
        allowed = {
            "issuer_id",
            "slot_id",
            "logical_agent_id",
            "revision",
            "event_id",
            "kind",
            "policy",
            "code_tag",
            "effective_at",
        }
        optional = {
            "deadline_at",
            "agent_name",
            "display_suffix",
            "policy_window",
            "inherit_policy",
        }
        if not isinstance(data, dict) or not allowed <= set(data) or set(data) - allowed - optional:
            raise AccessMeshError("access_mesh_invalid_wire_event")
        try:
            raw_policy = data["policy"]
            if raw_policy is not None and type(raw_policy) is not dict:
                raise ValueError("policy must be an object")
            raw_deadline = data.get("deadline_at")
            if raw_deadline is not None and type(raw_deadline) is not str:
                raise ValueError("deadline_at must be a string")
            raw_time = data["effective_at"]
            if raw_time is not None and type(raw_time) is not str:
                raise ValueError("effective_at must be a string")
            return cls(
                issuer_id=data["issuer_id"],
                slot_id=data["slot_id"],
                logical_agent_id=data["logical_agent_id"],
                revision=data["revision"],
                event_id=data["event_id"],
                kind=data["kind"],
                policy=SlotPolicy(**raw_policy) if raw_policy is not None else None,
                code_tag=data["code_tag"],
                effective_at=datetime.fromisoformat(raw_time) if raw_time is not None else None,
                deadline_at=datetime.fromisoformat(raw_deadline)
                if raw_deadline is not None
                else None,
                agent_name=data.get("agent_name"),
                display_suffix=data.get("display_suffix"),
                policy_window=CapturedAccessWindow.from_wire(data["policy_window"])
                if "policy_window" in data
                else None,
                inherit_policy=data.get("inherit_policy"),
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise AccessMeshError("access_mesh_invalid_wire_event") from exc


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    peer_node_id: str
    event: AccessSlotEvent
    issued_kind: Literal["temporary", "persistent"] | None


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
    kind: Literal["temporary", "persistent"]
    state: Literal["active", "suspended", "deleted"]
    revision: int
    policy: SlotPolicy
    anchor: datetime | None
    code_tag: str
    deadline_at: datetime | None = None
    fence_revision: int = 1
    agent_name: str | None = None
    display_suffix: str | None = None
    policy_window: CapturedAccessWindow | None = None
    inherit_policy: bool | None = None
    code_generation: int = 1

    def writable_at(self, now: datetime) -> bool:
        slot = self.effective(now)
        return slot.state == "active" and slot.policy.active_at(slot.anchor, now, slot.deadline_at)

    def effective(self, now: datetime) -> SlotSnapshot:
        from terminal_mcp.core.access_policy_windows import effective_slot

        return effective_slot(self, _utc(now))


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
        # The node-local bootstrap proof remains immutable: it encrypts durable
        # receipts and validates pre-enrollment local slots. A verified managed
        # Mesh may install a separate group proof without rewriting either.
        self._proof_key = proof_key
        self._managed_proof_key: bytes | None = None
        self._local_proof_history: tuple[bytes, ...] = ()
        self._initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def _prepare_schema_upgrade(self, db, *, from_version: int) -> None:
        """Native adapters preserve configured defaults inside the migration transaction."""

    def _initialize(self) -> None:
        with self._connect() as db:
            # SQLite cannot replace a CHECK constraint in place. Rebuild the
            # old slot-kind table in one transaction while retaining every FK
            # dependant and rechecking them before committing the migration.
            db.execute("PRAGMA foreign_keys=OFF")
            # DDL otherwise autocommits one statement at a time. The version
            # fence must become durable with the first incompatible Access DDL,
            # before later store constructors or asynchronous startup can fail.
            db.execute("BEGIN IMMEDIATE")
            current_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if current_version > ACCESS_MESH_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current_version} is newer than supported "
                    f"{ACCESS_MESH_SCHEMA_VERSION}"
                )
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            ledger_exists = "schema_migration_state" in tables
            has_history = any(
                table != "sqlite_sequence" and not table.startswith("access_mesh_")
                for table in tables
            )
            if current_version == 0 and not has_history:
                # A first constructor, including an interrupted older one,
                # can leave only empty Access metadata. It has no historical
                # data migration to preserve. Never erase an existing ledger
                # or infer authorization; the identity guard remains unchanged.
                for table in tables:
                    if table == "sqlite_sequence":
                        history = db.execute(
                            "SELECT 1 FROM sqlite_sequence WHERE seq>0 LIMIT 1"
                        ).fetchone()
                    else:
                        quoted = table.replace('"', '""')
                        history = db.execute(f'SELECT 1 FROM "{quoted}" LIMIT 1').fetchone()
                    if history is not None:
                        has_history = True
                        break
            if current_version < ACCESS_MESH_SCHEMA_VERSION and (current_version or has_history):
                db.execute("""CREATE TABLE IF NOT EXISTS schema_migration_state (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL
                ) WITHOUT ROWID""")
                ledger_exists = True
                # SqliteRepository still needs the original revision for its
                # historical conditional migrations (notably pre-21 claims).
                db.execute(
                    "INSERT OR IGNORE INTO schema_migration_state(name,value) "
                    "VALUES('repository_schema_from',?)",
                    (current_version,),
                )
            origin = None
            if ledger_exists:
                origin = db.execute(
                    "SELECT value FROM schema_migration_state WHERE name='repository_schema_from'"
                ).fetchone()
            self._prepare_schema_upgrade(
                db, from_version=int(origin[0]) if origin else current_version
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_slot_replicas (
                  issuer_id TEXT NOT NULL,
                  slot_id TEXT NOT NULL,
                  logical_agent_id TEXT NOT NULL,
                  kind TEXT NOT NULL CHECK(kind IN ('temporary','persistent')),
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
            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_event_outbox (
                  peer_node_id TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  issuer_id TEXT NOT NULL,
                  slot_id TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  payload_json TEXT NOT NULL,
                  acked_at TEXT,
                  PRIMARY KEY(peer_node_id,event_id),
                  UNIQUE(peer_node_id,issuer_id,slot_id,revision)
                ) WITHOUT ROWID
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS access_mesh_outbox_unacked
                ON access_mesh_event_outbox(peer_node_id,acked_at,issuer_id,slot_id,revision)
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS access_mesh_event_log (
                  issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL,
                  revision INTEGER NOT NULL, event_id TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  PRIMARY KEY(issuer_id,slot_id,revision),
                  UNIQUE(issuer_id,event_id)
                ) WITHOUT ROWID
            """)

            columns = {row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")}
            for column in ("agent_name", "display_suffix"):
                if column not in columns:
                    db.execute(f"ALTER TABLE access_mesh_slot_replicas ADD COLUMN {column} TEXT")
            if "inherit_policy" not in columns:
                db.execute(
                    "ALTER TABLE access_mesh_slot_replicas "
                    "ADD COLUMN inherit_policy INTEGER NOT NULL DEFAULT 0"
                )
            if "policy_window_json" not in columns:
                db.execute(
                    "ALTER TABLE access_mesh_slot_replicas ADD COLUMN policy_window_json TEXT"
                )
            if "code_generation" not in columns:
                db.execute(
                    "ALTER TABLE access_mesh_slot_replicas "
                    "ADD COLUMN code_generation INTEGER NOT NULL DEFAULT 1"
                )
            if "deadline_at" not in columns:
                db.execute("ALTER TABLE access_mesh_slot_replicas ADD COLUMN deadline_at TEXT")
            if "fence_revision" not in columns:
                db.execute(
                    "ALTER TABLE access_mesh_slot_replicas "
                    "ADD COLUMN fence_revision INTEGER NOT NULL DEFAULT 1"
                )
            self._migrate_temporary_slot_kind(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS access_mesh_slots_agent "
                "ON access_mesh_slot_replicas(logical_agent_id)"
            )
            db.execute(f"PRAGMA user_version={ACCESS_MESH_SCHEMA_VERSION}")

    @staticmethod
    def _migrate_temporary_slot_kind(db):
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='access_mesh_slot_replicas'"
        ).fetchone()[0]
        if "'legacy'" not in schema:
            return
        objects = [
            row[0]
            for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='access_mesh_slot_replicas' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL"
            )
        ]
        columns = [row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")]
        new_schema = re.sub(
            r"\baccess_mesh_slot_replicas\b", "access_mesh_slot_kind_upgrade", schema, count=1
        )
        db.execute(new_schema.replace("'legacy'", "'temporary'"))
        projection = [
            "CASE kind WHEN 'legacy' THEN 'temporary' ELSE kind END" if c == "kind" else c
            for c in columns
        ]
        db.execute(
            "INSERT INTO access_mesh_slot_kind_upgrade (" + ",".join(columns) + ") "
            "SELECT " + ",".join(projection) + " FROM access_mesh_slot_replicas"
        )
        db.execute("DROP TABLE access_mesh_slot_replicas")
        db.execute("ALTER TABLE access_mesh_slot_kind_upgrade RENAME TO access_mesh_slot_replicas")
        for sql in objects:
            db.execute(sql)
        db.execute(
            "UPDATE access_mesh_event_outbox "
            "SET payload_json=json_set(payload_json,'$.issued_kind','temporary') "
            "WHERE json_extract(payload_json,'$.issued_kind')='legacy'"
        )
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Access slot-kind migration violates existing foreign keys")

    def _after_apply(self, db, event: AccessSlotEvent, *, release_claims: bool = False) -> None:
        """Runtime stores extend this transactional hook; domain replicas stay standalone."""

    def peer_proof_tag(self, *, receiver_id: str, sender_id: str, nonce: str) -> str:
        """Authenticate compatibility of the pre-enrolled Access proof secret.

        The random challenge is authenticated at the transport layer. Only the
        HMAC leaves this node; neither a private signing key nor the proof key
        itself is transmitted.
        """
        _required_identifier(receiver_id)
        _required_identifier(sender_id)
        if (
            receiver_id not in self.trusted_issuers
            or sender_id not in self.trusted_issuers
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        ):
            raise AccessMeshError("access_mesh_invalid_proof_challenge")
        return hmac.new(
            self._managed_proof_key or self._proof_key,
            f"terminal-mcp-native-proof-v1:{receiver_id}:{sender_id}:{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def code_tags(self, issuer_id: str, code: str) -> tuple[str, ...]:
        """Current verifier, plus LOCAL historical proof tags only.

        Remote claims are always validated against the current managed proof;
        archived proofs cannot resurrect a removed remote issuer. Historical
        node-local grants continue to attach after authority rotation, leave,
        or process restart, subject to their normal slot/session lifecycle.
        """
        _required_identifier(issuer_id)
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4}", code):
            raise AccessMeshError("access_mesh_invalid_code")
        keys = [self._managed_proof_key or self._proof_key]
        if issuer_id == self.local_node_id or getattr(self, "registry_mesh_id", None):
            keys.extend((self._proof_key, *self._local_proof_history))
        tags = []
        for key in keys:
            tag = hmac.new(
                key, f"terminal-mcp-ac-v1:{issuer_id}:{code}".encode(), hashlib.sha256
            ).hexdigest()
            if tag not in tags:
                tags.append(tag)
        return tuple(tags)

    def code_tag(self, issuer_id: str, code: str) -> str:
        """Current Mesh verifier for new grants, never the plaintext PIN."""
        return self.code_tags(issuer_id, code)[0]

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
            deadline_at=_parse(row["deadline_at"]) if "deadline_at" in row.keys() else None,
            fence_revision=row["fence_revision"] if "fence_revision" in row.keys() else 1,
            agent_name=row["agent_name"] if "agent_name" in row.keys() else None,
            display_suffix=row["display_suffix"] if "display_suffix" in row.keys() else None,
            policy_window=CapturedAccessWindow.from_wire(json.loads(row["policy_window_json"]))
            if "policy_window_json" in row.keys() and row["policy_window_json"]
            else None,
            code_generation=row["code_generation"] if "code_generation" in row.keys() else 1,
            inherit_policy=bool(row["inherit_policy"]) if "inherit_policy" in row.keys() else False,
        )

    def slot(self, issuer_id: str, slot_id: str) -> SlotSnapshot | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
        return self._snapshot(row) if row is not None else None

    def _queue_event(
        self,
        db: sqlite3.Connection,
        event: AccessSlotEvent,
        issued_kind: Literal["temporary", "persistent"] | None,
        recipients: tuple[str, ...],
    ) -> None:
        """Durably queue all peer deliveries in the same transaction as the event."""
        wire = json.dumps(event.to_wire(), sort_keys=True, separators=(",", ":"))
        db.execute(
            """INSERT INTO access_mesh_event_log
               (issuer_id,slot_id,revision,event_id,event_json)
               VALUES(?,?,?,?,?)""",
            (event.issuer_id, event.slot_id, event.revision, event.event_id, wire),
        )
        payload = json.dumps(
            {"event": event.to_wire(), "issued_kind": issued_kind},
            sort_keys=True,
        )
        for peer in recipients:
            db.execute(
                """INSERT INTO access_mesh_event_outbox
                   (peer_node_id,event_id,issuer_id,slot_id,revision,payload_json)
                   VALUES(?,?,?,?,?,?)""",
                (
                    peer,
                    event.event_id,
                    event.issuer_id,
                    event.slot_id,
                    event.revision,
                    payload,
                ),
            )

    def pending_outbox(self, *, peer_node_id: str, limit: int = 50) -> tuple[PendingDelivery, ...]:
        """Fetch bounded durable deliveries for an authenticated peer transport."""
        if (
            peer_node_id not in self.trusted_issuers
            or peer_node_id == self.local_node_id
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise AccessMeshError("access_mesh_invalid_peer")
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload_json FROM access_mesh_event_outbox
                   WHERE peer_node_id=? AND acked_at IS NULL
                   ORDER BY issuer_id,slot_id,revision
                   LIMIT ?""",
                (peer_node_id, limit),
            ).fetchall()
        values: list[PendingDelivery] = []
        for row in rows:
            content = json.loads(row["payload_json"])
            values.append(
                PendingDelivery(
                    peer_node_id=peer_node_id,
                    event=AccessSlotEvent.from_wire(content["event"]),
                    issued_kind=content["issued_kind"],
                )
            )
        return tuple(values)

    def acknowledge_delivery(
        self,
        *,
        peer_node_id: str,
        event_id: str,
        authenticated_peer_id: str,
    ) -> bool:
        """ACK only after a receiving peer durably commits its event."""
        if (
            peer_node_id != authenticated_peer_id
            or peer_node_id not in self.trusted_issuers
            or peer_node_id == self.local_node_id
        ):
            raise AccessMeshError("access_mesh_untrusted_peer")
        _required_identifier(event_id)
        with self._connect() as db:
            changed = db.execute(
                """UPDATE access_mesh_event_outbox
                   SET acked_at=COALESCE(acked_at,?)
                   WHERE peer_node_id=? AND event_id=?""",
                (_text(datetime.now(UTC)), peer_node_id, event_id),
            )
            return changed.rowcount == 1

    def _prepare_new_slot(self, db, event: AccessSlotEvent) -> None:
        """Hook: native adapters may recycle expired grants in this transaction."""

    def _before_slot_update(self, db, old, *, state, code_tag):
        """Subclass invariants participate in the same mutation transaction."""

    def apply_event(
        self,
        event: AccessSlotEvent,
        *,
        authenticated_peer_id: str,
        issued_kind: Literal["temporary", "persistent"] | None = None,
        broadcast_to: tuple[str, ...] = (),
        _connection=None,
    ) -> ApplyResult:
        """Process an issuer event once; out-of-order gaps demand snapshot/catchup.

        Transport must first authenticate the sending peer. An event claiming to
        come from another issuer cannot change this issuer's local replica.
        """
        authority = getattr(self, "registry_authority_id", self.local_node_id)
        relayed = bool(
            getattr(self, "registry_mesh_id", None) and authenticated_peer_id == authority
        )
        if not relayed and (
            authenticated_peer_id != event.issuer_id or event.issuer_id not in self.trusted_issuers
        ):
            raise AccessMeshError("access_mesh_untrusted_issuer")
        recipients = tuple(dict.fromkeys(broadcast_to))
        if recipients and (
            (event.issuer_id != self.local_node_id and self.local_node_id != authority)
            or any(p not in self.trusted_issuers or p == self.local_node_id for p in recipients)
        ):
            raise AccessMeshError("access_mesh_untrusted_peer")
        with self._connect() if _connection is None else nullcontext(_connection) as db:
            if _connection is None:
                db.execute("BEGIN IMMEDIATE")
            guard = getattr(self, "assert_registry_ready", None)
            if guard is not None:
                guard(db)
                self.assert_registry_writer(db, authenticated_peer_id)
            old = db.execute(
                "SELECT * FROM access_mesh_slot_replicas WHERE issuer_id=? AND slot_id=?",
                (event.issuer_id, event.slot_id),
            ).fetchone()
            recorded = db.execute(
                "SELECT event_id,event_json FROM access_mesh_event_log "
                "WHERE issuer_id=? AND slot_id=? AND revision=?",
                (event.issuer_id, event.slot_id, event.revision),
            ).fetchone()
            if recorded is not None and (
                recorded["event_id"] != event.event_id
                or recorded["event_json"]
                != json.dumps(event.to_wire(), sort_keys=True, separators=(",", ":"))
            ):
                raise AccessMeshError("access_mesh_event_conflict")
            if old is None:
                if event.kind == "SlotIssued":
                    self._prepare_new_slot(db, event)
                if event.kind != "SlotIssued" or issued_kind not in {"temporary", "persistent"}:
                    raise AccessMeshError("access_mesh_unknown_slot")
                if event.revision != 1:
                    raise AccessMeshError("access_mesh_event_gap")
                db.execute(
                    """INSERT INTO access_mesh_slot_replicas
                       (issuer_id,slot_id,logical_agent_id,kind,state,revision,
                        event_id,policy_json,code_tag,anchor,agent_name,display_suffix,inherit_policy)
                       VALUES(?,?,?,?,'active',?,?,?,?,?,?,?,?)""",
                    (
                        event.issuer_id,
                        event.slot_id,
                        event.logical_agent_id,
                        issued_kind,
                        event.revision,
                        event.event_id,
                        json.dumps(asdict(event.policy), sort_keys=True),
                        event.code_tag,
                        _text(event.effective_at) if event.effective_at else None,
                        event.agent_name,
                        event.display_suffix,
                        int(bool(event.inherit_policy)),
                    ),
                )
                self._queue_event(db, event, issued_kind, recipients)
                self._after_apply(db, event)
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
            deadline = old["deadline_at"]
            fence_revision = old["fence_revision"]
            policy_window = self._snapshot(old).policy_window
            code_generation = old["code_generation"]
            inherit_policy = bool(old["inherit_policy"])
            ended_policy = policy
            if event.kind in {"SlotSuspended", "SlotDeleted", "SessionStarted", "SessionEnded"}:
                fence_revision = event.revision
            if event.kind == "SlotIssued":
                raise AccessMeshError("access_mesh_issue_replayed")
            if event.kind == "SlotPolicyChanged":
                policy = event.policy
                if event.inherit_policy is not None:
                    inherit_policy = event.inherit_policy
                if event.policy_window is not None:
                    policy_window = event.policy_window
            elif event.kind == "SlotSuspended":
                state = "suspended"
            elif event.kind == "SlotResumed":
                state = "active"
            elif event.kind == "SlotDeleted":
                state = "deleted"
            elif event.kind == "AccessCodeRotated":
                tag = event.code_tag
                code_generation += 1
            elif event.kind in {"SessionStarted", "SessionUpdated"}:
                anchor = _text(event.effective_at)
                deadline = _text(event.deadline_at) if event.deadline_at else None
                if event.policy is not None:
                    policy = event.policy
                if event.kind == "SessionStarted":
                    if (
                        policy_window is not None
                        and event.deadline_at is not None
                        and event.effective_at == policy_window.started_at
                    ):
                        captured_policy = policy_window.policy
                        policy_window = CapturedAccessWindow(
                            captured_policy,
                            event.effective_at,
                            event.deadline_at,
                            event.deadline_at + timedelta(seconds=captured_policy.cooldown_seconds)
                            if captured_policy.rearm_enabled
                            else None,
                        )
                    else:
                        policy_window = None
                elif policy_window is not None:
                    current_policy = self._snapshot(old).effective(event.effective_at).policy
                    end = event.deadline_at or event.effective_at + timedelta(
                        seconds=current_policy.duration_seconds
                    )
                    policy_window = CapturedAccessWindow(
                        current_policy,
                        event.effective_at,
                        end,
                        end + timedelta(seconds=current_policy.cooldown_seconds)
                        if current_policy.rearm_enabled
                        else None,
                    )
            elif event.kind == "SessionEnded":
                ended_policy = self._snapshot(old).effective(event.effective_at).policy
                deadline = None
                next_start = (
                    event.effective_at + timedelta(seconds=ended_policy.cooldown_seconds)
                    if ended_policy.rearm_enabled
                    else None
                )
                anchor = _text(next_start) if next_start is not None else None
                if policy_window is not None:
                    # Explicit end preserves the captured window's own cooldown;
                    # subsequent windows then resolve the committed new defaults.
                    policy_window = CapturedAccessWindow(
                        ended_policy,
                        policy_window.started_at,
                        policy_window.hard_expires_at,
                        next_start,
                        event.effective_at,
                    )
            self._before_slot_update(db, old, state=state, code_tag=tag)
            release_claims = (
                event.kind == "SlotDeleted"
                or (old["kind"] == "temporary" and event.kind == "SlotSuspended")
                or (
                    event.kind == "SessionEnded"
                    and (old["kind"] == "temporary" or ended_policy.release_on_end is True)
                )
            )
            db.execute(
                """UPDATE access_mesh_slot_replicas
                   SET state=?,revision=?,event_id=?,policy_json=?,code_tag=?,anchor=?,
                       deadline_at=?,fence_revision=?,policy_window_json=?,code_generation=?,inherit_policy=?
                   WHERE issuer_id=? AND slot_id=?""",
                (
                    state,
                    event.revision,
                    event.event_id,
                    json.dumps(asdict(policy), sort_keys=True),
                    tag,
                    anchor,
                    deadline,
                    fence_revision,
                    json.dumps(policy_window.to_wire(), sort_keys=True)
                    if policy_window is not None
                    else None,
                    code_generation,
                    int(inherit_policy),
                    event.issuer_id,
                    event.slot_id,
                ),
            )
            if event.kind == "SlotDeleted":
                db.execute(
                    "DELETE FROM access_mesh_attachments WHERE issuer_id=? AND slot_id=?",
                    (event.issuer_id, event.slot_id),
                )
            self._queue_event(db, event, issued_kind, recipients)
            self._after_apply(db, event, release_claims=release_claims)
        return ApplyResult(
            "applied",
            release_claims=release_claims,
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
        # Old, persisted claims never re-authorize a node after it leaves the
        # Mesh or moves to a different Mesh/authority.
        trusted = getattr(
            self, "registry_issuer_trusted", lambda value: value in self.trusted_issuers
        )
        if not trusted(issuer_id):
            raise AccessMeshError("access_mesh_untrusted_issuer")
        if (
            issuer_id != self.local_node_id
            and getattr(self, "managed_trust_required", False)
            and self._managed_proof_key is None
        ):
            # A verified Mesh topology by itself does not enroll Native Access.
            # A stale HMAC tag from an old/bootstrap key cannot authorize a
            # foreign issuer while its Mesh authority proof is still pending.
            raise AccessMeshError("access_mesh_proof_enrollment_pending")
        tags = self.code_tags(issuer_id, code)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            guard = getattr(self, "assert_registry_ready", None)
            if guard is not None:
                guard(db)
            placeholders = ",".join("?" for _ in tags)
            rows = db.execute(
                "SELECT * FROM access_mesh_slot_replicas "
                f"WHERE issuer_id=? AND code_tag IN ({placeholders}) AND state='active'",
                (issuer_id, *tags),
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
                    (connection_key, issuer_id, slot.slot_id, slot.logical_agent_id),
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

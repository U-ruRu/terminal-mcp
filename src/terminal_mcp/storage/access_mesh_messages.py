"""Local message acceptance, obligations and delivery outbox in one SQLite transaction."""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from terminal_mcp.core.access_mesh_scope import assert_registry_scope

MESSAGE_TTL = timedelta(hours=24)
CUTOVER_REASON = "delivery_stopped_local_cutover"
LOCAL_MESSAGE_TAG = ":localmsg:"


def parse_creation_time(text):
    if not isinstance(text, str) or not text:
        raise ValueError("missing timestamp")
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("naive timestamp")
    return stamp.astimezone(UTC)


def unexpired(wire, now=None):
    try:
        return parse_creation_time(wire["created_at"]) + MESSAGE_TTL > (now or datetime.now(UTC))
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class MeshMessagingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AccessMeshMessageStore:
    def __init__(self, path: str | Path, local_node_id: str, *, now_provider=None):
        self.clock = now_provider or (lambda: datetime.now(UTC))
        self.path = Path(path)
        self.local_node_id = local_node_id
        self.local_prefix = f"{local_node_id}{LOCAL_MESSAGE_TAG}"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS access_mesh_messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_hash TEXT NOT NULL UNIQUE, wire_json TEXT NOT NULL,
                    origin_node_id TEXT NOT NULL, receipt_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_mesh_message_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id TEXT NOT NULL,
                    kind TEXT NOT NULL, event_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                    message_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, acked_at TEXT,
                    terminal_at TEXT, terminal_reason TEXT,
                    UNIQUE(peer_id,kind,event_key)
                );
                CREATE INDEX IF NOT EXISTS access_mesh_message_pending
                    ON access_mesh_message_outbox(acked_at,id);
                CREATE TABLE IF NOT EXISTS access_mesh_message_delivery_receipts (
                    message_hash TEXT NOT NULL, agent_id TEXT NOT NULL, peer_id TEXT NOT NULL,
                    public_name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'delivered',
                    read_at TEXT, replied_at TEXT, reply_message_hash TEXT,
                    PRIMARY KEY(message_hash,agent_id)
                );
            """)
            old_cols = {
                row[1] for row in db.execute("PRAGMA table_info(access_mesh_message_outbox)")
            }
            if "terminal_at" not in old_cols:
                db.execute("ALTER TABLE access_mesh_message_outbox ADD COLUMN terminal_at TEXT")
            if "terminal_reason" not in old_cols:
                db.execute("ALTER TABLE access_mesh_message_outbox ADD COLUMN terminal_reason TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def health_probe(self) -> bool:
        """Verify local write, read and recipient access without durable effects.

        The savepoint rollback removes all inserted message and event rows. No
        peer delivery, outbox or session/task state is involved in this probe.
        """
        now = self.clock().isoformat()
        message_hash = f"{self.local_prefix}health-{secrets.token_hex(12)}"
        with self.connect() as db:
            db.execute("SAVEPOINT local_message_health")
            try:
                db.execute(
                    "INSERT INTO coordination_messages("
                    "message_hash,sender_agent_id,target_name,text,created_at,delivery_mode) "
                    "VALUES(?,?,?,?,?,?)",
                    (message_hash, "health-sender", "health-recipient", "health", now, "notify"),
                )
                db.execute(
                    "INSERT INTO coordination_message_recipients(message_hash,recipient_agent_id) "
                    "VALUES(?,?)",
                    (message_hash, "health-recipient"),
                )
                wire = {"message_hash": message_hash, "created_at": now}
                db.execute(
                    "INSERT INTO access_mesh_messages("
                    "message_hash,wire_json,origin_node_id,receipt_json) VALUES(?,?,?,?)",
                    (message_hash, canonical(wire), self.local_node_id, "{}"),
                )
                record = self._recipient_tx(db, message_hash, "health-recipient")
                return record["message_hash"] == message_hash
            finally:
                db.execute("ROLLBACK TO local_message_health")
                db.execute("RELEASE local_message_health")

    def activity(self, issuer_id, slot_id):
        """Observe local attachment activity without materializing a session."""
        with self.connect() as db:
            row = db.execute(
                "SELECT MAX(t.last_active_at) FROM access_mesh_activity t "
                "JOIN access_mesh_attachments a USING(connection_key) "
                "WHERE a.issuer_id=? AND a.slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
        return row[0] if row else None

    def wire(self, message_hash):
        if not message_hash.startswith(self.local_prefix):
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
            ).fetchone()
        if row is None:
            return None
        wire = json.loads(row[0])
        return wire if unexpired(wire, self.clock()) else None

    def _receipt_tx(self, db, message_hash):
        if not message_hash.startswith(self.local_prefix):
            raise MeshMessagingError("message_not_found")
        row = db.execute(
            "SELECT receipt_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
        ).fetchone()
        wire_row = db.execute(
            "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
        ).fetchone()
        if row is None or wire_row is None or not unexpired(json.loads(wire_row[0]), self.clock()):
            raise MeshMessagingError("message_not_found")
        receipt = json.loads(row[0])
        recipients = {item["logical_agent_id"]: item for item in receipt["deliveries"]}
        receipt["deliveries"] = list(recipients.values())
        receipt["delivered_to"] = [item["public_name"] for item in recipients.values()]
        receipt["recipient_count"] = len(recipients)
        receipt["pending_peers"] = []
        receipt["delivery_errors"] = []
        receipt["state"] = "delivered" if recipients else "no_recipients"
        return receipt

    def receipt(self, message_hash):
        with self.connect() as db:
            return self._receipt_tx(db, message_hash)

    def _acceptance_receipt_tx(self, db, message_hash):
        receipt = self._receipt_tx(db, message_hash)
        # Keep the committed local receipt within the public response budget.
        if len(canonical(receipt).encode("utf-8")) > 64 * 1024:
            raise MeshMessagingError("output_item_too_large")
        return receipt

    def accept(self, wire, recipients, *, reply_author=None):
        """Commit one node-local message and its recipient obligations."""
        message_hash = wire["message_hash"]
        if not message_hash.startswith(self.local_prefix):
            raise MeshMessagingError("feature_disabled")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_registry_scope(db)
            existing = db.execute(
                "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
            ).fetchone()
            if existing:
                if existing[0] != canonical(wire):
                    raise MeshMessagingError("idempotency_conflict")
                return self._acceptance_receipt_tx(db, message_hash)
            parent = None
            if reply_author is not None:
                parent = self._recipient_tx(db, wire["reply_to"], reply_author)
                # Allow multiple deliberate replies to a single parent.
            db.execute(
                "INSERT INTO coordination_messages(message_hash,sender_agent_id,target_name,text,"
                "created_at,require_reply,alert,delivery_mode,task_namespace,task_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    message_hash,
                    wire["sender_id"],
                    wire["target"],
                    wire["text"],
                    wire["created_at"],
                    int(wire["require_reply"] or wire["alert"]),
                    int(wire["alert"]),
                    wire["mode"],
                    wire["namespace"],
                    wire["task_id"],
                ),
            )
            unique = {item["logical_agent_id"]: item for item in recipients}
            db.executemany(
                "INSERT INTO coordination_message_recipients"
                "(message_hash,recipient_agent_id,delivered_at) VALUES(?,?,?)",
                [(message_hash, agent, wire["created_at"]) for agent in unique],
            )
            receipt = {
                "ok": True,
                "action": "reply" if wire["reply_to"] else "send",
                "message_hash": message_hash,
                "sender": wire["sender_name"],
                "target": wire["target"],
                "scope": wire["scope"],
                "mode": wire["mode"],
                "outcome": "committed",
                "reply_to": wire["reply_to"],
                "deliveries": [
                    {
                        "logical_agent_id": agent,
                        "public_name": item["public_name"],
                        "server_id": self.local_node_id,
                        "state": "delivered",
                    }
                    for agent, item in unique.items()
                ],
            }
            db.execute(
                "INSERT INTO access_mesh_messages"
                "(message_hash,wire_json,origin_node_id,receipt_json) VALUES(?,?,?,?)",
                (message_hash, canonical(wire), wire["origin_node_id"], canonical(receipt)),
            )
            if parent is not None:
                stamp = wire["created_at"]
                db.execute(
                    "UPDATE coordination_message_recipients SET read_at=COALESCE(read_at,?),"
                    "replied_at=COALESCE(replied_at,?),"
                    "reply_message_hash=COALESCE(reply_message_hash,?) "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (stamp, stamp, message_hash, wire["reply_to"], reply_author),
                )
                self._validate_receipt_recipient(db, wire["reply_to"], reply_author)
            return self._acceptance_receipt_tx(db, message_hash)

    def _recipient_tx(self, db, message_hash, agent_id):
        if not message_hash.startswith(self.local_prefix):
            raise MeshMessagingError("message_not_found")
        row = db.execute(
            "SELECT * FROM coordination_message_recipients "
            "WHERE message_hash=? AND recipient_agent_id=?",
            (message_hash, agent_id),
        ).fetchone()
        wire_row = db.execute(
            "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?", (message_hash,)
        ).fetchone()
        if wire_row is None or not unexpired(json.loads(wire_row[0]), self.clock()):
            raise MeshMessagingError("message_not_found")
        if row is None:
            raise MeshMessagingError("message_forbidden")
        return row

    def recipient(self, message_hash, agent_id):
        with self.connect() as db:
            return dict(self._recipient_tx(db, message_hash, agent_id))

    def _validate_receipt_recipient(self, db, message_hash, agent_id):
        """Validate the local recipient before changing its ACK/reply state."""
        self._recipient_tx(db, message_hash, agent_id)

    def acknowledge(self, message_hash, agent_id, stamp):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_registry_scope(db)
            self._recipient_tx(db, message_hash, agent_id)
            message = db.execute(
                "SELECT wire_json FROM access_mesh_messages WHERE message_hash=?",
                (message_hash,),
            ).fetchone()
            system_alert = bool(
                message
                and json.loads(message["wire_json"]).get("sender_id") == "terminal-mcp-system"
            )
            db.execute(
                "UPDATE coordination_message_recipients SET "
                "read_at=COALESCE(read_at,?), "
                "replied_at=CASE WHEN ? THEN COALESCE(replied_at,?) "
                "ELSE replied_at END "
                "WHERE message_hash=? AND recipient_agent_id=? "
                "AND (read_at IS NULL OR (?=1 AND replied_at IS NULL))",
                (stamp, int(system_alert), stamp, message_hash, agent_id, int(system_alert)),
            )
            # Keep system ACK's reply completion atomic with read.
            # Other alert messages continue to require an actual reply.
            self._validate_receipt_recipient(db, message_hash, agent_id)
            row = self._recipient_tx(db, message_hash, agent_id)
            return {
                "ok": True,
                "action": "ack",
                "message_hash": message_hash,
                "state": "replied" if row["replied_at"] else "read",
                "outcome": "committed",
            }

    def surface(self, agent_id, message_hashes, stamp):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_registry_scope(db)
            for message_hash in message_hashes:
                self._recipient_tx(db, message_hash, agent_id)
                db.execute(
                    "UPDATE coordination_message_recipients SET "
                    "first_seen_at=COALESCE(first_seen_at,?),last_seen_at=?,seen_count=seen_count+1,"
                    "read_at=CASE WHEN EXISTS(SELECT 1 FROM coordination_messages "
                    "WHERE message_hash=? AND delivery_mode='notify') "
                    "THEN COALESCE(read_at,?) ELSE read_at END "
                    "WHERE message_hash=? AND recipient_agent_id=?",
                    (stamp, stamp, message_hash, stamp, message_hash, agent_id),
                )
                self._validate_receipt_recipient(db, message_hash, agent_id)

    def page(
        self,
        agent_id,
        *,
        history=False,
        before=0,
        limit=21,
        namespace=None,
        task_id=None,
        message_hash=None,
    ):
        where = [
            "substr(x.message_hash,1,?)=? AND julianday(m.created_at)>julianday(?)",
            "(r.recipient_agent_id IS NOT NULL OR m.sender_agent_id=?)"
            if history
            else "r.recipient_agent_id IS NOT NULL AND (r.read_at IS NULL OR "
            "(m.require_reply=1 AND m.delivery_mode!='ack' AND r.replied_at IS NULL))",
        ]
        params = [
            agent_id,
            len(self.local_prefix),
            self.local_prefix,
            (self.clock() - MESSAGE_TTL).isoformat(),
        ]
        if history:
            params.append(agent_id)
        if message_hash is not None:
            where.append("m.message_hash=?")
            params.append(message_hash)
        if before:
            where.append("x.sequence<?")
            params.append(before)
        if namespace is not None:
            where.append("m.task_namespace=? AND m.task_id=?")
            params.extend([namespace, task_id])
        params.append(limit)
        with self.connect() as db:
            # Keep message pagination and recipient facts in one read snapshot.
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT x.sequence,m.*,x.wire_json,x.receipt_json,r.recipient_agent_id,r.read_at,"
                "r.first_seen_at,r.last_seen_at,r.seen_count,r.replied_at,r.reply_message_hash "
                "FROM coordination_messages m JOIN access_mesh_messages x USING(message_hash) "
                "LEFT JOIN coordination_message_recipients r ON r.message_hash=m.message_hash "
                "AND r.recipient_agent_id=? WHERE "
                + " AND ".join(where)
                + " ORDER BY x.sequence DESC LIMIT ?",
                params,
            ).fetchall()
            result = [dict(row) for row in rows]
            outgoing = {
                row["message_hash"]: row
                for row in result
                if row["sender_agent_id"] == agent_id and row["recipient_agent_id"] is None
            }
            if outgoing:
                # Do not join recipients before LIMIT: a broadcast is still one
                # message, one sequence and one cursor position.
                placeholders = ",".join("?" for _ in outgoing)
                receipts = {message_hash: [] for message_hash in outgoing}
                for row in db.execute(
                    "SELECT * FROM coordination_message_recipients "
                    f"WHERE message_hash IN ({placeholders}) ORDER BY recipient_agent_id",
                    tuple(outgoing),
                ):
                    receipts[row["message_hash"]].append(dict(row))
                for message_hash, row in outgoing.items():
                    row.update(self._sender_status(receipts[message_hash]))
                    names = {
                        item["logical_agent_id"]: item["public_name"]
                        for item in json.loads(row["receipt_json"])["deliveries"]
                    }
                    row["delivery_recipients"] = [
                        {**receipt, "public_name": names.get(receipt["recipient_agent_id"])}
                        for receipt in receipts[message_hash]
                    ]
        return result

    @staticmethod
    def _sender_status(recipients):
        """Project recipient facts without creating/surfacing a sender receipt.

        For broadcast, scalar progress is reached only by ALL recipients; its
        timestamp is when the final recipient reached it. A reply reference is
        unambiguous only for a directed/single-recipient delivery.
        """
        if not recipients:
            return {}
        result = {}
        for key in ("first_seen_at", "read_at", "replied_at"):
            values = [row[key] for row in recipients]
            result[key] = max(values) if all(values) else None
        result["last_seen_at"] = max(
            (row["last_seen_at"] for row in recipients if row["last_seen_at"]), default=None
        )
        result["seen_count"] = min(row["seen_count"] or 0 for row in recipients)
        result["reply_message_hash"] = (
            recipients[0]["reply_message_hash"] if len(recipients) == 1 else None
        )
        return result

    def _local_cutover_report_tx(self, db, now):
        """Only diagnose new local rows; historic payloads are disposable.

        Malformed legacy timestamps, orphan old receipts and old peer records
        are deliberately not migration prerequisites.
        """
        rows = db.execute(
            "SELECT message_hash,wire_json FROM access_mesh_messages "
            "WHERE substr(message_hash,1,?)=?",
            (len(self.local_prefix), self.local_prefix),
        ).fetchall()
        pending = db.execute(
            "SELECT kind,COUNT(*) AS count FROM access_mesh_message_outbox "
            "WHERE acked_at IS NULL AND terminal_at IS NULL GROUP BY kind"
        ).fetchall()
        result = {
            "retained": 0,
            "expired": 0,
            "terminate_jobs": {row["kind"]: row["count"] for row in pending},
            "processed_jobs": 0,
            "blockers": [],
        }
        for row in rows:
            try:
                wire = json.loads(row["wire_json"])
                if wire.get("message_hash") != row["message_hash"]:
                    raise ValueError("message hash mismatch")
                if parse_creation_time(wire["created_at"]) + MESSAGE_TTL <= now:
                    result["expired"] += 1
                else:
                    result["retained"] += 1
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                result["blockers"].append({"message_hash": row["message_hash"], "reason": str(exc)})
        return result

    def local_cutover_report(self, *, now=None):
        """Non-mutating new-local-message and retired-job summary."""
        now = now or self.clock()
        with self.connect() as db:
            return self._local_cutover_report_tx(db, now)

    def activate_local_cutover(self, *, now=None):
        """Terminalize old peer jobs without restoring/inspecting old messages."""
        now = now or self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_registry_scope(db)
            report = self._local_cutover_report_tx(db, now)
            if report["blockers"]:
                raise MeshMessagingError("migration_blocked")
            cursor = db.execute(
                "UPDATE access_mesh_message_outbox SET terminal_at=?,terminal_reason=?, "
                "last_error=? WHERE acked_at IS NULL AND terminal_at IS NULL",
                (now.isoformat(), CUTOVER_REASON, CUTOVER_REASON),
            )
            report["processed_jobs"] = max(0, cursor.rowcount)
            return report

    def prune_expired(self, *, now=None):
        """Delete only expired messages and directly subordinate message data."""
        now = now or self.clock()
        cutoff = (now - MESSAGE_TTL).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assert_registry_scope(db)
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT message_hash FROM access_mesh_messages WHERE "
                    "substr(message_hash,1,?)=? AND "
                    "CASE WHEN json_valid(wire_json) "
                    "THEN julianday(json_extract(wire_json,'$.created_at'))<=julianday(?) "
                    "ELSE 0 END",
                    (len(self.local_prefix), self.local_prefix, cutoff),
                )
            ]
            for message_hash in ids:
                db.execute(
                    "DELETE FROM access_mesh_message_outbox WHERE message_hash=?", (message_hash,)
                )
                db.execute(
                    "DELETE FROM access_mesh_message_delivery_receipts WHERE message_hash=?",
                    (message_hash,),
                )
                db.execute(
                    "DELETE FROM coordination_message_recipients WHERE message_hash=?",
                    (message_hash,),
                )
                db.execute("DELETE FROM access_mesh_messages WHERE message_hash=?", (message_hash,))
                db.execute(
                    "DELETE FROM coordination_messages WHERE message_hash=?", (message_hash,)
                )
            return len(ids)

"""Durable, node-scoped MCP Events source, subscriptions and delivery queue."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import aiosqlite

from terminal_mcp.core.orchestration import (
    parse_utc,
    public_agent_name,
    utc_now,
    utc_text,
)
from terminal_mcp.fleet.event_mesh import initialize_mesh_storage, queue_lifecycle_events
from terminal_mcp.fleet.message_event_mesh import queue_message_events, remote_message_interest
from terminal_mcp.storage.permissions import secure_database_path

EVENT_NAMES = ("agent.attached", "agent.inactive", "session.ended", "message.received")
INACTIVE_AFTER_SECONDS = 180
MAX_DELIVERY_ATTEMPTS = 5


def event_catalog():
    data = {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string"},
            "server": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["agent_name", "server"],
        "additionalProperties": False,
    }
    private_message = {
        "type": "object",
        "properties": {
            "server": {"type": "string"},
            "sender": {"type": "string"},
            "recipient": {"type": "string"},
            "message_id": {"type": "string"},
            "text": {"type": "string"},
            "timestamp": {"type": "string"},
        },
        "required": [
            "server",
            "sender",
            "recipient",
            "message_id",
            "text",
            "timestamp",
        ],
        "additionalProperties": False,
    }
    return {
        "events": [
            {
                "name": name,
                "description": {
                    "agent.attached": (
                        "Agent acquired active local presence on a trusted fleet source server."
                    ),
                    "agent.inactive": (
                        "Agent has been inactive on its trusted fleet source server "
                        "for over 180 seconds."
                    ),
                    "session.ended": (
                        "A limited agent work session has ended on a trusted fleet source server."
                    ),
                    "message.received": "A new local message for the selected session number.",
                }[name],
                "delivery": ["webhook"],
                "inputSchema": {
                    "type": "object",
                    "properties": (
                        {"session_number": {
                            "type": "string", "pattern": "^[0-9]{4}$",
                            "minLength": 4, "maxLength": 4,
                        }}
                        if name == "message.received"
                        else {}
                    ),
                    "additionalProperties": False,
                },
                "payloadSchema": private_message if name == "message.received" else data,
            }
            for name in EVENT_NAMES
        ]
    }


class SubscriptionAuthorizationError(PermissionError):
    """Subscription rights changed before the durable write."""


class RecipientBindingChangedError(SubscriptionAuthorizationError):
    """A refresh cannot redirect an existing public-number subscription."""


class MCPEventStore:
    def __init__(self, path, node_id: str, auth_database_path=None):
        self.path = path
        self.node_id = node_id
        self.auth_database_path = auth_database_path

    async def initialize(self):
        secure_database_path(self.path)
        async with aiosqlite.connect(self.path, timeout=5) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS mcp_event_cursor(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    last_seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_records(
                    event_id TEXT PRIMARY KEY,
                    source_key TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    recipient_agent_id TEXT,
                    namespace TEXT
                );
                CREATE TABLE IF NOT EXISTS mcp_event_inactivity(
                    subject_key TEXT PRIMARY KEY,
                    last_notified_activity TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_local_attached(
                    work_session_id TEXT PRIMARY KEY,
                    logical_agent_id TEXT NOT NULL,
                    session_epoch INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    attached_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_managed_activity(
                    work_session_id TEXT PRIMARY KEY,
                    session_epoch INTEGER NOT NULL,
                    node_instance_id TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_subscriptions(
                    id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    credential_id TEXT,
                    auth_mode TEXT,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    signing_secret TEXT NOT NULL,
                    previous_signing_secret TEXT,
                    rotation_until TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_message_bindings(
                    subscription_id TEXT PRIMARY KEY,
                    recipient_agent_id TEXT NOT NULL,
                    recipient_session_id TEXT NOT NULL,
                    recipient_epoch INTEGER NOT NULL,
                    recipient_connection_key TEXT NOT NULL,
                    recipient_node_id TEXT NOT NULL,
                    recipient_issuer_id TEXT NOT NULL,
                    recipient_slot_id TEXT NOT NULL,
                    recipient_role TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_verified_callbacks(
                    principal_id TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    verified_until TEXT NOT NULL,
                    PRIMARY KEY(principal_id,callback_url)
                );
                CREATE TABLE IF NOT EXISTS mcp_event_deliveries(
                    event_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_status INTEGER,
                    PRIMARY KEY(event_id,subscription_id)
                );
                CREATE INDEX IF NOT EXISTS ix_mcp_event_delivery_due
                    ON mcp_event_deliveries(state,next_attempt_at);
                -- Creation is a local, committed presence boundary, not a remote grant.
                CREATE TRIGGER IF NOT EXISTS tr_mcp_agent_attached_insert
                AFTER INSERT ON agent_sessions
                BEGIN
                    INSERT INTO instance_events(
                        event_type,entity_type,entity_id,actor_id,payload_json,created_at
                    ) VALUES (
                        'mcp.agent.attached','agent',NEW.agent_id,NULL,'{}',
                        strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS tr_mcp_agent_attached_reactivate
                AFTER UPDATE OF state ON agent_sessions
                WHEN OLD.state='forced' AND OLD.end_reason='idle_timeout'
                     AND NEW.state='active' AND NEW.source_instance_id IS NOT NULL
                BEGIN
                    INSERT INTO instance_events(
                        event_type,entity_type,entity_id,actor_id,payload_json,created_at
                    ) VALUES (
                        'mcp.agent.attached','agent',NEW.agent_id,NULL,'{}',
                        strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    );
                END;
                -- A grant on the authority is NOT local presence; materialization is.
                CREATE TRIGGER IF NOT EXISTS tr_mcp_persistent_presence_insert
                AFTER INSERT ON persistent_attachment_presence
                BEGIN
                    INSERT INTO instance_events(
                        event_type,entity_type,entity_id,actor_id,payload_json,created_at
                    ) VALUES (
                        'mcp.presence.attached','attachment_presence',
                        NEW.node_attachment_id,NULL,
                        json_object('node',NEW.node_instance_id,
                                    'logical_agent_id',NEW.logical_agent_id),
                        strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    );
                END;
            """)
            await initialize_mesh_storage(db)
            record_columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(mcp_event_records)")
                ).fetchall()
            }
            if "recipient_agent_id" not in record_columns:
                await db.execute("ALTER TABLE mcp_event_records ADD COLUMN recipient_agent_id TEXT")
            if "namespace" not in record_columns:
                await db.execute("ALTER TABLE mcp_event_records ADD COLUMN namespace TEXT")
            columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(mcp_event_subscriptions)")
                ).fetchall()
            }
            if "credential_id" not in columns:
                await db.execute(
                    "ALTER TABLE mcp_event_subscriptions ADD COLUMN credential_id TEXT"
                )
            if "auth_mode" not in columns:
                await db.execute("ALTER TABLE mcp_event_subscriptions ADD COLUMN auth_mode TEXT")
            await db.execute(
                "INSERT OR IGNORE INTO mcp_event_cursor(singleton,last_seq) "
                "SELECT 1,COALESCE(MAX(seq),0) FROM instance_events"
            )
            # Crash-safe requeue. The event ID survives retries.
            await db.execute(
                "UPDATE mcp_event_deliveries SET state='pending' WHERE state='sending'"
            )
            await db.commit()

    @staticmethod
    async def _insert_event(
        db,
        *,
        source_key,
        name,
        occurred_at,
        data,
        recipient_agent_id=None,
        namespace=None,
    ):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]
        await db.execute(
            "INSERT OR IGNORE INTO mcp_event_records"
            "(event_id,source_key,name,occurred_at,data_json,recipient_agent_id,namespace) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "evt_" + digest,
                source_key,
                name,
                occurred_at,
                payload,
                recipient_agent_id,
                namespace,
            ),
        )

    async def pump(self, *, now=None):
        """Consume committed local journal boundaries; one-shot idle scan."""
        current = now or utc_now()
        stamp = utc_text(current)
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                state = await (
                    await db.execute("SELECT last_seq FROM mcp_event_cursor WHERE singleton=1")
                ).fetchone()
                last_seq = int(state[0])
                rows = await (
                    await db.execute(
                        "SELECT seq,event_type,entity_id,actor_id,payload_json,created_at "
                        "FROM instance_events WHERE seq>? ORDER BY seq LIMIT 500",
                        (last_seq,),
                    )
                ).fetchall()
                for item in rows:
                    source_key = f"{self.node_id}:journal:{item['seq']}"
                    event_type = item["event_type"]
                    if event_type == "mcp.agent.attached":
                        row = await (
                            await db.execute(
                                "SELECT agent_id FROM agent_sessions WHERE agent_id=?",
                                (item["entity_id"],),
                            )
                        ).fetchone()
                        if row:
                            await self._insert_event(
                                db,
                                source_key=source_key,
                                name="agent.attached",
                                occurred_at=item["created_at"],
                                data={
                                    "agent_name": public_agent_name(row["agent_id"]),
                                    "server": self.node_id,
                                },
                            )
                    elif event_type == "mcp.presence.attached":
                        source = json.loads(item["payload_json"])
                        if source.get("node") == self.node_id:
                            row = await (
                                await db.execute(
                                    "SELECT a.display_name,p.work_session_id,p.session_epoch "
                                    "FROM persistent_attachment_presence p "
                                    "JOIN logical_agents a ON "
                                    "a.logical_agent_id=p.logical_agent_id "
                                    "WHERE p.node_attachment_id=? AND p.node_instance_id=?",
                                    (item["entity_id"], self.node_id),
                                )
                            ).fetchone()
                            if row:
                                await db.execute(
                                    "INSERT OR IGNORE INTO mcp_event_local_attached("
                                    "work_session_id,logical_agent_id,session_epoch,"
                                    "agent_name,attached_at) "
                                    "SELECT p.work_session_id,p.logical_agent_id,"
                                    "p.session_epoch,?,? "
                                    "FROM persistent_attachment_presence p "
                                    "WHERE p.node_attachment_id=? AND p.node_instance_id=?",
                                    (
                                        row["display_name"],
                                        item["created_at"],
                                        item["entity_id"],
                                        self.node_id,
                                    ),
                                )
                                await self._insert_event(
                                    db,
                                    source_key=(
                                        f"{self.node_id}:work_session_attached:"
                                        f"{row['work_session_id']}:{row['session_epoch']}"
                                    ),
                                    name="agent.attached",
                                    occurred_at=item["created_at"],
                                    data={
                                        "agent_name": row["display_name"],
                                        "server": self.node_id,
                                    },
                                )
                    elif event_type == "message.receipt":
                        # The Access Mesh messages table may not exist on a
                        # legacy-only installation; its unrelated journal rows
                        # must never prevent other Events from being delivered.
                        mesh_table = await (
                            await db.execute(
                                "SELECT 1 FROM sqlite_master "
                                "WHERE type='table' AND name='access_mesh_messages'"
                            )
                        ).fetchone()
                        if not mesh_table:
                            continue
                        # The recipient INSERT has delivered=1; receipt UPDATE
                        # events for seen, read, ACK or reply must be ignored.
                        receipt = json.loads(item["payload_json"])
                        recipient = item["actor_id"]
                        if receipt.get("delivered") != 1 or not recipient:
                            continue
                        message = await (
                            await db.execute(
                                "SELECT m.message_hash,m.text,m.created_at,"
                                "m.task_namespace,w.wire_json,w.receipt_json "
                                "FROM coordination_messages m "
                                "JOIN coordination_message_recipients r "
                                "ON r.message_hash=m.message_hash "
                                "JOIN access_mesh_messages w ON w.message_hash=m.message_hash "
                                "WHERE m.message_hash=? AND r.recipient_agent_id=? "
                                "AND substr(m.message_hash,1,?)=?",
                                (
                                    item["entity_id"],
                                    recipient,
                                    len(self.node_id + ":localmsg:"),
                                    self.node_id + ":localmsg:",
                                ),
                            )
                        ).fetchone()
                        if message is not None:
                            # Do not persist private message content when there
                            # is no explicit active subscription for its recipient.
                            allowed = await (
                                await db.execute(
                                    "SELECT 1 FROM mcp_event_subscriptions s "
                                    "JOIN mcp_event_message_bindings b "
                                    "ON b.subscription_id=s.id "
                                    "WHERE s.name='message.received' "
                                    "AND b.recipient_agent_id=? AND b.recipient_node_id=? "
                                    "AND s.created_at<=? AND s.expires_at>? LIMIT 1",
                                    (
                                        recipient,
                                        self.node_id,
                                        message["created_at"],
                                        stamp,
                                    ),
                                )
                            ).fetchone()
                            remote_allowed = await remote_message_interest(
                                db, recipient, message["created_at"], stamp,
                            )
                            if allowed or remote_allowed:
                                wire = json.loads(message["wire_json"])
                                deliveries = json.loads(message["receipt_json"]).get(
                                    "deliveries", []
                                )
                                recipient_name = next((
                                    delivery.get("public_name") for delivery in deliveries
                                    if delivery.get("logical_agent_id") == recipient
                                ), None)
                                if not recipient_name:
                                    # Never attribute old deliveries to a newly renamed slot.
                                    continue
                                await self._insert_event(
                                    db,
                                    source_key=(
                                        f"{self.node_id}:message.received:"
                                        f"{item['entity_id']}:{recipient}"
                                    ),
                                    name="message.received",
                                    occurred_at=message["created_at"],
                                    data={
                                        "server": self.node_id,
                                        "sender": str(wire.get("sender_name") or "")[:120],
                                        "recipient": recipient_name,
                                        "message_id": message["message_hash"],
                                        "text": message["text"],
                                        "timestamp": message["created_at"],
                                    },
                                    recipient_agent_id=recipient,
                                    namespace=message["task_namespace"],
                                )
                    elif event_type == "work_session.changed":
                        source = json.loads(item["payload_json"])
                        if source.get("state") in ("ended", "expired"):
                            # Include actual locally started managed sessions
                            # even when no legacy persistent presence rows exist.
                            local = await (
                                await db.execute(
                                    "SELECT agent_name AS display_name "
                                    "FROM mcp_event_local_attached "
                                    "WHERE work_session_id=? AND logical_agent_id=? "
                                    "AND session_epoch=?",
                                    (
                                        item["entity_id"],
                                        source.get("logical_agent_id"),
                                        source.get("session_epoch"),
                                    ),
                                )
                            ).fetchone()
                            presences = await (
                                await db.execute(
                                    "SELECT DISTINCT a.display_name "
                                    "FROM persistent_attachment_presence p "
                                    "JOIN logical_agents a ON "
                                    "a.logical_agent_id=p.logical_agent_id "
                                    "WHERE p.work_session_id=? AND p.session_epoch=? "
                                    "AND p.logical_agent_id=? AND p.node_instance_id=?",
                                    (
                                        item["entity_id"],
                                        source.get("session_epoch"),
                                        source.get("logical_agent_id"),
                                        self.node_id,
                                    ),
                                )
                            ).fetchall()
                            if local or presences:
                                # Stable identity across terminal-state metadata
                                # updates, retries and multiple representations.
                                await self._insert_event(
                                    db,
                                    source_key=(
                                        f"{self.node_id}:work_session_ended:"
                                        f"{item['entity_id']}:{source.get('session_epoch')}"
                                    ),
                                    name="session.ended",
                                    occurred_at=item["created_at"],
                                    data={
                                        "agent_name": (local or presences[0])["display_name"],
                                        "server": self.node_id,
                                    },
                                )
                expired = utc_text(current - timedelta(hours=24))
                await db.execute(
                    "DELETE FROM mcp_event_deliveries WHERE event_id IN ("
                    "SELECT event_id FROM mcp_event_records "
                    "WHERE name='message.received' AND occurred_at<?)",
                    (expired,),
                )
                await db.execute(
                    "DELETE FROM mcp_event_records WHERE name='message.received' AND occurred_at<?",
                    (expired,),
                )
                if rows:
                    await db.execute(
                        "UPDATE mcp_event_cursor SET last_seq=? WHERE singleton=1",
                        (rows[-1]["seq"],),
                    )
                legacy = await (
                    await db.execute(
                        "SELECT agent_id,last_activity_at FROM agent_sessions WHERE state='active' "
                        "AND (global_expires_at IS NULL OR global_expires_at>?)",
                        (stamp,),
                    )
                ).fetchall()
                presences = await (
                    await db.execute(
                        "SELECT p.node_attachment_id,p.last_activity_at FROM "
                        "persistent_attachment_presence p "
                        "JOIN logical_agent_node_attachments n "
                        "ON n.node_attachment_id=p.node_attachment_id "
                        "JOIN logical_agent_work_sessions s "
                        "ON s.work_session_id=p.work_session_id "
                        "WHERE p.node_instance_id=? AND n.revoked_at IS NULL "
                        "AND s.state='active' AND s.hard_expires_at>?",
                        (self.node_id, stamp),
                    )
                ).fetchall()
                subjects = [
                    (
                        "legacy:" + row["agent_id"],
                        row["last_activity_at"],
                        public_agent_name(row["agent_id"]),
                    )
                    for row in legacy
                ]
                for presence in presences:
                    row = await (
                        await db.execute(
                            "SELECT a.display_name FROM persistent_attachment_presence p "
                            "JOIN logical_agents a ON "
                            "a.logical_agent_id=p.logical_agent_id "
                            "WHERE p.node_attachment_id=?",
                            (presence["node_attachment_id"],),
                        )
                    ).fetchone()
                    if row:
                        subjects.append(
                            (
                                "persistent:" + presence["node_attachment_id"],
                                presence["last_activity_at"],
                                row["display_name"],
                            )
                        )
                managed = await (
                    await db.execute(
                        "SELECT s.work_session_id,a.agent_name,"
                        "COALESCE(m.last_activity_at,a.attached_at) AS last_activity_at "
                        "FROM mcp_event_local_attached a "
                        "JOIN logical_agent_work_sessions s "
                        "ON s.work_session_id=a.work_session_id "
                        "AND s.session_epoch=a.session_epoch "
                        "LEFT JOIN mcp_event_managed_activity m ON "
                        "m.work_session_id=s.work_session_id "
                        "AND m.session_epoch=s.session_epoch "
                        "WHERE s.state='active' AND s.authority_node_id=? "
                        "AND s.hard_expires_at>? "
                        "AND NOT EXISTS (SELECT 1 FROM persistent_attachment_presence p "
                        "WHERE p.work_session_id=s.work_session_id "
                        "AND p.session_epoch=s.session_epoch AND p.node_instance_id=?)",
                        (self.node_id, stamp, self.node_id),
                    )
                ).fetchall()
                subjects.extend(
                    (
                        "managed:" + row["work_session_id"],
                        row["last_activity_at"],
                        row["agent_name"],
                    )
                    for row in managed
                )
                for subject_key, activity, name in subjects:
                    if (current - parse_utc(activity)).total_seconds() <= INACTIVE_AFTER_SECONDS:
                        continue
                    await self._insert_event(
                        db,
                        source_key="inactive:" + self.node_id + ":" + subject_key + ":" + activity,
                        name="agent.inactive",
                        occurred_at=stamp,
                        data={"agent_name": name, "server": self.node_id},
                    )
                # Map event + subscription only when the event occurred during
                # the subscription lifetime; no retroactive unsolicited delivery.
                await db.execute(
                    "INSERT OR IGNORE INTO mcp_event_deliveries"
                    "(event_id,subscription_id,next_attempt_at) "
                    "SELECT e.event_id,s.id,? FROM mcp_event_records e "
                    "JOIN mcp_event_subscriptions s ON s.name=e.name "
                    "WHERE (e.name!='message.received' OR EXISTS("
                    "SELECT 1 FROM mcp_event_message_bindings b "
                    "WHERE b.subscription_id=s.id AND "
                    "b.recipient_agent_id=e.recipient_agent_id "
                    "AND b.recipient_node_id=?)) "
                    "AND e.occurred_at >= s.created_at AND e.occurred_at < s.expires_at "
                    "AND s.expires_at>?",
                    (stamp, self.node_id, stamp),
                )
                await queue_lifecycle_events(db, stamp)
                await queue_message_events(db, stamp)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def note_local_attach(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        agent_name: str | None = None,
        now=None,
    ) -> bool:
        """Only invoke after a successful authenticated local attach/start.

        A remote grant or a replicated WorkSession row cannot invoke this on
        its own. One event per locally-attached WorkSession epoch.
        """
        stamp = utc_text(now or utc_now())
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await (
                    await db.execute(
                        "SELECT s.work_session_id,s.session_epoch,a.display_name "
                        "FROM logical_agent_work_sessions s "
                        "JOIN logical_agents a ON a.logical_agent_id=s.logical_agent_id "
                        "WHERE s.logical_agent_id=? AND s.work_session_id=? "
                        "AND s.session_epoch=? AND s.state='active' "
                        "AND s.authority_node_id=? AND s.hard_expires_at>?",
                        (logical_agent_id, work_session_id, session_epoch, self.node_id, stamp),
                    )
                ).fetchone()
                if current is None:
                    await db.rollback()
                    return False
                display = agent_name or current["display_name"]
                cur = await db.execute(
                    "INSERT OR IGNORE INTO mcp_event_local_attached("
                    "work_session_id,logical_agent_id,session_epoch,agent_name,attached_at)"
                    "VALUES(?,?,?,?,?)",
                    (work_session_id, logical_agent_id, session_epoch, display, stamp),
                )
                if cur.rowcount == 1:
                    await self._insert_event(
                        db,
                        source_key=(
                            f"{self.node_id}:work_session_attached:"
                            f"{work_session_id}:{session_epoch}"
                        ),
                        name="agent.attached",
                        occurred_at=stamp,
                        data={"agent_name": display, "server": self.node_id},
                    )
                await db.commit()
                return cur.rowcount == 1
            except BaseException:
                await db.rollback()
                raise

    async def note_managed_activity(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        now=None,
    ) -> bool:
        """Write only an authenticated agent's real operation to its own session."""
        stamp = utc_text(now or utc_now())
        async with aiosqlite.connect(self.path, timeout=5) as db:
            cur = await db.execute(
                "INSERT INTO mcp_event_managed_activity("
                "work_session_id,session_epoch,node_instance_id,last_activity_at) "
                "SELECT s.work_session_id,s.session_epoch,s.authority_node_id,? "
                "FROM logical_agent_work_sessions s "
                "JOIN mcp_event_local_attached a ON a.work_session_id=s.work_session_id "
                "AND a.session_epoch=s.session_epoch "
                "WHERE s.logical_agent_id=? AND s.work_session_id=? "
                "AND s.session_epoch=? AND s.state='active' "
                "AND s.authority_node_id=? AND s.hard_expires_at>? "
                "ON CONFLICT(work_session_id) DO UPDATE SET "
                "last_activity_at=CASE WHEN excluded.last_activity_at>last_activity_at "
                "THEN excluded.last_activity_at ELSE last_activity_at END",
                (
                    stamp,
                    logical_agent_id,
                    work_session_id,
                    session_epoch,
                    self.node_id,
                    stamp,
                ),
            )
            await db.commit()
            return bool(cur.rowcount)

    async def subscription(self, subscription_id):
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM mcp_event_subscriptions WHERE id=?",
                    (subscription_id,),
                )
            ).fetchone()
            return dict(row) if row else None

    async def verified_recently(self, principal, callback_url):
        async with aiosqlite.connect(self.path, timeout=5) as db:
            row = await (
                await db.execute(
                    "SELECT 1 FROM mcp_event_verified_callbacks "
                    "WHERE principal_id=? AND callback_url=? AND verified_until>?",
                    (principal, callback_url, utc_text()),
                )
            ).fetchone()
            return bool(row)

    async def save_subscription(
        self,
        *,
        sub_id,
        principal,
        credential_id,
        auth_mode,
        name,
        callback,
        secret,
        ttl_ms=None,
        callback_verified_fresh=False,
        message_binding=None,
        authorization_checker=None,
        arguments=None,
    ):
        arguments_json = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))
        now = utc_now()
        created_at = utc_text(now)
        expires_at = utc_text(now + timedelta(milliseconds=min(ttl_ms or 3600000, 3600000)))
        async with aiosqlite.connect(self.path, timeout=5) as db:
            # OAuth continuity stores credentials in protected auth SQLite,
            # while Access Mesh bindings and subscriptions live in main SQLite.
            # Reserve writers in BOTH databases before the final checker; an
            # OAuth revoke or Access unbind must serialize before the checker
            # or after the committed subscription. No identity rows are changed.
            if auth_mode == "oauth" and self.auth_database_path is not None:
                await db.execute(
                    "ATTACH DATABASE ? AS protected_identity",
                    (str(self.auth_database_path),),
                )
            await db.execute("BEGIN IMMEDIATE")
            if message_binding is not None and authorization_checker is None:
                raise SubscriptionAuthorizationError
            if message_binding is not None and message_binding.role == "session_number":
                existing = await (
                    await db.execute(
                        "SELECT id FROM mcp_event_subscriptions WHERE principal_id=? "
                        "AND name=? AND arguments_json=? AND callback_url=? AND expires_at>?",
                        (principal, name, arguments_json, callback, created_at),
                    )
                ).fetchall()
                if any(row[0] != sub_id for row in existing):
                    raise RecipientBindingChangedError
            if authorization_checker is not None:
                candidate = {
                    "name": name,
                    "principal_id": principal,
                    "credential_id": credential_id,
                    "auth_mode": auth_mode,
                }
                if message_binding is not None:
                    candidate.update(
                        recipient_agent_id=message_binding.recipient_agent_id,
                        recipient_session_id=message_binding.work_session_id,
                        recipient_epoch=message_binding.session_epoch,
                        recipient_connection_key=message_binding.connection_key,
                        recipient_node_id=self.node_id,
                        recipient_issuer_id=message_binding.issuer_id,
                        recipient_slot_id=message_binding.slot_id,
                        recipient_role=message_binding.role,
                        recipient_require_live=message_binding.role == "session_number",
                    )
                try:
                    authorized = await authorization_checker(candidate)
                except Exception:
                    raise SubscriptionAuthorizationError from None
                if not authorized:
                    raise SubscriptionAuthorizationError
            await db.execute(
                "INSERT INTO mcp_event_subscriptions"
                "(id,principal_id,credential_id,auth_mode,name,arguments_json,"
                "callback_url,signing_secret,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "previous_signing_secret=CASE WHEN "
                "mcp_event_subscriptions.signing_secret!=excluded.signing_secret "
                "THEN mcp_event_subscriptions.signing_secret "
                "ELSE mcp_event_subscriptions.previous_signing_secret END,"
                "rotation_until=CASE WHEN "
                "mcp_event_subscriptions.signing_secret!=excluded.signing_secret "
                "THEN ? ELSE mcp_event_subscriptions.rotation_until END,"
                "signing_secret=excluded.signing_secret,"
                "credential_id=excluded.credential_id,auth_mode=excluded.auth_mode,"
                "expires_at=excluded.expires_at",
                (
                    sub_id,
                    principal,
                    credential_id,
                    auth_mode,
                    name,
                    arguments_json,
                    callback,
                    secret,
                    created_at,
                    expires_at,
                    utc_text(now + timedelta(minutes=5)),
                ),
            )
            if message_binding is not None:
                await db.execute(
                    "INSERT INTO mcp_event_message_bindings("
                    "subscription_id,recipient_agent_id,recipient_session_id,"
                    "recipient_epoch,recipient_connection_key,recipient_node_id,"
                    "recipient_issuer_id,recipient_slot_id,recipient_role)"
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET "
                    "recipient_agent_id=excluded.recipient_agent_id,"
                    "recipient_session_id=excluded.recipient_session_id,"
                    "recipient_epoch=excluded.recipient_epoch,"
                    "recipient_connection_key=excluded.recipient_connection_key,"
                    "recipient_node_id=excluded.recipient_node_id,"
                    "recipient_issuer_id=excluded.recipient_issuer_id,"
                    "recipient_slot_id=excluded.recipient_slot_id,"
                    "recipient_role=excluded.recipient_role",
                    (
                        sub_id,
                        message_binding.recipient_agent_id,
                        message_binding.work_session_id,
                        message_binding.session_epoch,
                        message_binding.connection_key,
                        self.node_id,
                        message_binding.issuer_id,
                        message_binding.slot_id,
                        message_binding.role,
                    ),
                )
            # Only a fresh signed challenge grants a new verification window.
            # A cached refresh must never extend the window indefinitely.
            if callback_verified_fresh:
                await db.execute(
                    "INSERT INTO mcp_event_verified_callbacks"
                    "(principal_id,callback_url,verified_until) VALUES(?,?,?) "
                    "ON CONFLICT(principal_id,callback_url) DO UPDATE SET "
                    "verified_until=excluded.verified_until",
                    (principal, callback, utc_text(now + timedelta(minutes=10))),
                )
            await db.commit()
        return expires_at

    async def unsubscribe(self, sub_id, principal):
        async with aiosqlite.connect(self.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM mcp_event_deliveries WHERE subscription_id=? AND EXISTS"
                "(SELECT 1 FROM mcp_event_subscriptions WHERE id=? AND principal_id=?)",
                (sub_id, sub_id, principal),
            )
            await db.execute(
                "DELETE FROM mcp_event_message_bindings WHERE subscription_id=? "
                "AND EXISTS(SELECT 1 FROM mcp_event_subscriptions "
                "WHERE id=? AND principal_id=?)",
                (sub_id, sub_id, principal),
            )
            await db.execute(
                "DELETE FROM mcp_event_subscriptions WHERE id=? AND principal_id=?",
                (sub_id, principal),
            )
            await db.commit()

    async def unsubscribe_matching(self, principal, name, callback, arguments=None):
        """Idempotent owner-authorized cleanup by the official subscription tuple.

        The stored recipient binding may already be revoked/expired. Never
        consult or create a new binding while removing an existing subscription.
        """
        selector = (
            "SELECT id FROM mcp_event_subscriptions WHERE principal_id=? "
            "AND name=? AND arguments_json=? AND callback_url=?"
        )
        arguments_json = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))
        params = (principal, name, arguments_json, callback)
        async with aiosqlite.connect(self.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM mcp_event_deliveries WHERE subscription_id IN (" + selector + ")",
                params,
            )
            await db.execute(
                "DELETE FROM mcp_event_message_bindings WHERE subscription_id IN ("
                + selector
                + ")",
                params,
            )
            await db.execute(
                "DELETE FROM mcp_event_subscriptions WHERE principal_id=? "
                "AND name=? AND arguments_json=? AND callback_url=?",
                params,
            )
            await db.commit()

    async def due_deliveries(self, limit=20):
        """Atomically claim a bounded batch across concurrent adapters/workers."""
        stamp = utc_text()
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await (
                    await db.execute(
                        "SELECT d.event_id,d.subscription_id,d.attempts,"
                        "e.name,e.occurred_at,e.data_json,s.callback_url,s.signing_secret,"
                        "s.principal_id,s.credential_id,s.auth_mode,"
                        "s.previous_signing_secret,s.rotation_until,"
                        "b.recipient_agent_id,b.recipient_session_id,b.recipient_epoch,"
                        "b.recipient_connection_key,b.recipient_node_id,"
                        "b.recipient_issuer_id,b.recipient_slot_id,b.recipient_role "
                        "FROM mcp_event_deliveries d "
                        "JOIN mcp_event_records e ON e.event_id=d.event_id "
                        "JOIN mcp_event_subscriptions s ON s.id=d.subscription_id "
                        "LEFT JOIN mcp_event_message_bindings b ON b.subscription_id=s.id "
                        "WHERE d.state='pending' AND d.next_attempt_at<=? "
                        "AND s.expires_at>? ORDER BY d.next_attempt_at LIMIT ?",
                        (stamp, stamp, limit),
                    )
                ).fetchall()
                for row in rows:
                    await db.execute(
                        "UPDATE mcp_event_deliveries SET state='sending' "
                        "WHERE event_id=? AND subscription_id=? AND state='pending'",
                        (row["event_id"], row["subscription_id"]),
                    )
                await db.commit()
                return [dict(row) for row in rows]
            except BaseException:
                await db.rollback()
                raise

    async def finish_attempt(self, event_id, sub_id, *, status=None):
        async with aiosqlite.connect(self.path, timeout=5) as db:
            row = await (
                await db.execute(
                    "SELECT attempts FROM mcp_event_deliveries WHERE "
                    "event_id=? AND subscription_id=?",
                    (event_id, sub_id),
                )
            ).fetchone()
            if row is None:
                return
            attempt = int(row[0]) + 1
            terminal = status is not None and (200 <= status < 300 or status in (410, 413))
            state = (
                "sent"
                if status is not None and 200 <= status < 300
                else ("dead" if terminal or attempt >= MAX_DELIVERY_ATTEMPTS else "pending")
            )
            delay = min(300, 2**attempt)
            await db.execute(
                "UPDATE mcp_event_deliveries SET attempts=?,state=?,last_status=?,"
                "next_attempt_at=? WHERE event_id=? AND subscription_id=?",
                (
                    attempt,
                    state,
                    status,
                    utc_text(utc_now() + timedelta(seconds=delay)),
                    event_id,
                    sub_id,
                ),
            )
            await db.commit()

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
from terminal_mcp.storage.permissions import secure_database_path

EVENT_NAMES = ("agent.attached", "agent.inactive", "session.ended")
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
    return {
        "events": [
            {
                "name": name,
                "description": {
                    "agent.attached": "Agent acquired active local presence on this server.",
                    "agent.inactive": (
                        "Agent has been inactive on this server for over 180 seconds."
                    ),
                    "session.ended": "A limited agent work session has ended on this server.",
                }[name],
                "delivery": ["webhook"],
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "payloadSchema": data,
            }
            for name in EVENT_NAMES
        ]
    }


class MCPEventStore:
    def __init__(self, path, node_id: str):
        self.path = path
        self.node_id = node_id

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
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mcp_event_inactivity(
                    subject_key TEXT PRIMARY KEY,
                    last_notified_activity TEXT NOT NULL
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
    async def _insert_event(db, *, source_key, name, occurred_at, data):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]
        await db.execute(
            "INSERT OR IGNORE INTO mcp_event_records"
            "(event_id,source_key,name,occurred_at,data_json) VALUES(?,?,?,?,?)",
            ("evt_" + digest, source_key, name, occurred_at, payload),
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
                        "SELECT seq,event_type,entity_id,payload_json,created_at "
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
                                    "SELECT a.display_name FROM persistent_attachment_presence p "
                                    "JOIN logical_agents a ON "
                                    "a.logical_agent_id=p.logical_agent_id "
                                    "WHERE p.node_attachment_id=? AND p.node_instance_id=?",
                                    (item["entity_id"], self.node_id),
                                )
                            ).fetchone()
                            if row:
                                await self._insert_event(
                                    db,
                                    source_key=source_key,
                                    name="agent.attached",
                                    occurred_at=item["created_at"],
                                    data={
                                        "agent_name": row["display_name"],
                                        "server": self.node_id,
                                    },
                                )
                    elif event_type == "work_session.changed":
                        source = json.loads(item["payload_json"])
                        if source.get("state") in ("ended", "expired"):
                            presences = await (
                                await db.execute(
                                    "SELECT DISTINCT p.node_instance_id,a.display_name "
                                    "FROM persistent_attachment_presence p "
                                    "JOIN logical_agents a ON "
                                    "a.logical_agent_id=p.logical_agent_id "
                                    "WHERE p.work_session_id=? AND p.session_epoch=? "
                                    "AND p.logical_agent_id=?",
                                    (
                                        item["entity_id"],
                                        source.get("session_epoch"),
                                        source.get("logical_agent_id"),
                                    ),
                                )
                            ).fetchall()
                            for presence in presences:
                                if presence["node_instance_id"] != self.node_id:
                                    continue
                                await self._insert_event(
                                    db,
                                    source_key=(
                                        f"{self.node_id}:work_session_ended:"
                                        f"{item['entity_id']}:"
                                        f"{source.get('session_epoch')}"
                                    ),
                                    name="session.ended",
                                    occurred_at=item["created_at"],
                                    data={
                                        "agent_name": presence["display_name"],
                                        "server": self.node_id,
                                    },
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
                    "WHERE e.occurred_at >= s.created_at AND e.occurred_at < s.expires_at "
                    "AND s.expires_at>?",
                    (stamp, stamp),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

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
    ):
        now = utc_now()
        created_at = utc_text(now)
        expires_at = utc_text(now + timedelta(milliseconds=min(ttl_ms or 3600000, 3600000)))
        async with aiosqlite.connect(self.path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
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
                    "{}",
                    callback,
                    secret,
                    created_at,
                    expires_at,
                    utc_text(now + timedelta(minutes=5)),
                ),
            )
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
                "DELETE FROM mcp_event_subscriptions WHERE id=? AND principal_id=?",
                (sub_id, principal),
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
                        "s.previous_signing_secret,s.rotation_until "
                        "FROM mcp_event_deliveries d "
                        "JOIN mcp_event_records e ON e.event_id=d.event_id "
                        "JOIN mcp_event_subscriptions s ON s.id=d.subscription_id "
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

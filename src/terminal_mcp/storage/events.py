from __future__ import annotations

import json
from typing import Any

import aiosqlite

from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.permissions import secure_database_path

DEFAULT_EVENT_MAX_ROWS = 50_000
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 1_000
MAX_EVENT_PAYLOAD_BYTES = 4_096

_DROP_PAYLOAD_KEYS = {
    "cmd",
    "command",
    "content",
    "lines",
    "output",
    "stderr",
    "stdout",
}


def _clean_value(value: Any, *, depth: int = 0):
    if depth >= 3:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:509] + "..."
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            name = str(key)
            if name.lower() in _DROP_PAYLOAD_KEYS:
                continue
            cleaned = _clean_value(item, depth=depth + 1)
            if cleaned is not None:
                result[name] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [
            cleaned
            for item in list(value)[:20]
            if (cleaned := _clean_value(item, depth=depth + 1)) is not None
        ]
    return str(value)[:512]


def compact_payload(payload: Any) -> str:
    cleaned = _clean_value(payload if payload is not None else {})
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_EVENT_PAYLOAD_BYTES:
        return encoded
    if isinstance(cleaned, dict):
        scalars = {
            key: value
            for key, value in cleaned.items()
            if value is None or isinstance(value, (bool, int, float, str))
        }
    else:
        scalars = {}
    scalars["truncated"] = True
    return json.dumps(scalars, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


async def install_event_journal(db) -> None:
    """Install journal storage and durable mutation-boundary triggers."""

    await db.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS instance_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            actor_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_instance_events_type_seq
            ON instance_events(event_type,seq);

        CREATE TRIGGER IF NOT EXISTS tr_instance_events_retention
        AFTER INSERT ON instance_events
        BEGIN
            DELETE FROM instance_events
            WHERE seq <= COALESCE((
                SELECT seq FROM instance_events
                ORDER BY seq DESC LIMIT 1 OFFSET {DEFAULT_EVENT_MAX_ROWS}
            ), -1);
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_agent_started
        AFTER INSERT ON agent_sessions
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'agent.started','agent',NEW.agent_id,NEW.agent_id,
                json_object('task_summary',NEW.task_summary,'intent',NEW.intent,'step',NEW.current_step),
                NEW.registered_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_agent_intent
        AFTER INSERT ON agent_task_events
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'agent.intent','agent',NEW.agent_id,NEW.agent_id,
                json_object('intent',NEW.intent,'step',NEW.step),
                NEW.timestamp
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_agent_ended
        AFTER UPDATE OF state,ended_at,end_reason ON agent_sessions
        WHEN OLD.state IS NOT NEW.state
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'agent.ended','agent',NEW.agent_id,NEW.agent_id,
                json_object('state',NEW.state,'reason',NEW.end_reason),
                COALESCE(NEW.ended_at,NEW.last_activity_at)
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_agent_activity
        AFTER INSERT ON agent_activity_events
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'agent.activity','agent',NEW.agent_id,NEW.agent_id,
                json_object('tool',NEW.tool,'command_hash',NEW.command_hash),
                NEW.timestamp
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_message_created
        AFTER INSERT ON coordination_messages
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'message.created','message',NEW.message_hash,NEW.sender_agent_id,
                json_object(
                    'target',NEW.target_name,
                    'require_reply',NEW.require_reply,
                    'alert',NEW.alert,
                    'task_namespace',NEW.task_namespace,
                    'task_id',NEW.task_id
                ),
                NEW.created_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_message_delivered
        AFTER INSERT ON coordination_message_recipients
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'message.receipt','message',NEW.message_hash,NEW.recipient_agent_id,
                json_object(
                    'recipient_agent_id',NEW.recipient_agent_id,
                    'delivered',1,
                    'seen',CASE WHEN NEW.first_seen_at IS NULL THEN 0 ELSE 1 END,
                    'read',CASE WHEN NEW.read_at IS NULL THEN 0 ELSE 1 END,
                    'replied',CASE WHEN NEW.replied_at IS NULL THEN 0 ELSE 1 END
                ),
                COALESCE(NEW.delivered_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_message_receipt_updated
        AFTER UPDATE OF first_seen_at,read_at,replied_at,reply_message_hash,seen_count
        ON coordination_message_recipients
        WHEN OLD.first_seen_at IS NOT NEW.first_seen_at
          OR OLD.read_at IS NOT NEW.read_at
          OR OLD.replied_at IS NOT NEW.replied_at
          OR OLD.reply_message_hash IS NOT NEW.reply_message_hash
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'message.receipt','message',NEW.message_hash,NEW.recipient_agent_id,
                json_object(
                    'recipient_agent_id',NEW.recipient_agent_id,
                    'seen',CASE WHEN NEW.first_seen_at IS NULL THEN 0 ELSE 1 END,
                    'read',CASE WHEN NEW.read_at IS NULL THEN 0 ELSE 1 END,
                    'replied',CASE WHEN NEW.replied_at IS NULL THEN 0 ELSE 1 END,
                    'reply_message_hash',NEW.reply_message_hash
                ),
                COALESCE(
                    NEW.replied_at,NEW.read_at,NEW.last_seen_at,NEW.first_seen_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_context_created
        AFTER INSERT ON instance_context
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'context.created','context',CAST(NEW.id AS TEXT),NULL,
                json_object('summary',NEW.summary,'primary',NEW.is_primary),
                strftime('%Y-%m-%dT%H:%M:%fZ','now')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_context_updated
        AFTER UPDATE ON instance_context
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'context.updated','context',CAST(NEW.id AS TEXT),NULL,
                json_object('summary',NEW.summary,'primary',NEW.is_primary),
                strftime('%Y-%m-%dT%H:%M:%fZ','now')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_context_deleted
        AFTER DELETE ON instance_context
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'context.deleted','context',CAST(OLD.id AS TEXT),NULL,
                json_object('summary',OLD.summary,'primary',OLD.is_primary),
                strftime('%Y-%m-%dT%H:%M:%fZ','now')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_task_work_event
        AFTER INSERT ON work_events
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'task.' || NEW.event_type,
                'task',
                NEW.namespace || '/' || NEW.task_id,
                NEW.agent_id,
                json_object(
                    'state',json_extract(NEW.payload_json,'$.state'),
                    'lane',json_extract(NEW.payload_json,'$.lane'),
                    'priority',json_extract(NEW.payload_json,'$.priority'),
                    'fields',json_extract(NEW.payload_json,'$.fields'),
                    'candidate_ref',json_extract(NEW.payload_json,'$.candidate_ref'),
                    'kind',json_extract(NEW.payload_json,'$.kind'),
                    'namespace',json_extract(NEW.payload_json,'$.namespace'),
                    'task_id',json_extract(NEW.payload_json,'$.task_id'),
                    'verdict',json_extract(NEW.payload_json,'$.verdict'),
                    'dimensions',json_extract(NEW.payload_json,'$.dimensions'),
                    'command_hash',json_extract(NEW.payload_json,'$.command_hash'),
                    'command_type',json_extract(NEW.payload_json,'$.command_type')
                ),
                NEW.created_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_command_created
        AFTER INSERT ON commands
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'command.created','command',NEW.hash,NULL,
                json_object('status',NEW.status,'queue_id',NEW.queue_id),
                COALESCE(
                    NEW.enqueued_at,NEW.started_at,NEW.finished_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_command_status
        AFTER UPDATE OF status ON commands
        WHEN OLD.status IS NOT NEW.status
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'command.status','command',NEW.hash,NULL,
                json_object(
                    'status',NEW.status,
                    'queue_id',NEW.queue_id,
                    'exit_code',NEW.exit_code,
                    'has_error',CASE WHEN NEW.error IS NULL THEN 0 ELSE 1 END
                ),
                COALESCE(
                    NEW.finished_at,NEW.started_at,NEW.claimed_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
            );
        END;
        """
    )
    await db.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS tr_event_logical_agent_created
        AFTER INSERT ON logical_agents
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'logical_agent.created','logical_agent',NEW.logical_agent_id,NULL,
                json_object(
                    'display_name',NEW.display_name,
                    'state',NEW.state,
                    'authority_node_id',NEW.authority_node_id,
                    'authority_epoch',NEW.authority_epoch,
                    'slot_revision',NEW.slot_revision,
                    'selector_generation',NEW.selector_generation,
                    'auth_generation',NEW.auth_generation,
                    'deleted_at',NEW.deleted_at
                ),
                NEW.created_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_logical_agent_changed
        AFTER UPDATE ON logical_agents
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'logical_agent.changed','logical_agent',NEW.logical_agent_id,NULL,
                json_object(
                    'display_name',NEW.display_name,
                    'state',NEW.state,
                    'authority_node_id',NEW.authority_node_id,
                    'authority_epoch',NEW.authority_epoch,
                    'slot_revision',NEW.slot_revision,
                    'selector_generation',NEW.selector_generation,
                    'auth_generation',NEW.auth_generation,
                    'deleted_at',NEW.deleted_at
                ),
                NEW.updated_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_work_session_created
        AFTER INSERT ON logical_agent_work_sessions
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'work_session.created','work_session',NEW.work_session_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'session_epoch',NEW.session_epoch,
                    'authority_node_id',NEW.authority_node_id,
                    'authority_epoch',NEW.authority_epoch,
                    'hard_expires_at',NEW.hard_expires_at,
                    'state',NEW.state,
                    'origin_instance_id',NEW.origin_instance_id
                ),
                NEW.started_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_work_session_changed
        AFTER UPDATE ON logical_agent_work_sessions
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'work_session.changed','work_session',NEW.work_session_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'session_epoch',NEW.session_epoch,
                    'authority_node_id',NEW.authority_node_id,
                    'authority_epoch',NEW.authority_epoch,
                    'hard_expires_at',NEW.hard_expires_at,
                    'state',NEW.state,
                    'origin_instance_id',NEW.origin_instance_id,
                    'ended_at',NEW.ended_at,
                    'end_reason',NEW.end_reason
                ),
                COALESCE(NEW.ended_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_node_attachment_created
        AFTER INSERT ON logical_agent_node_attachments
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'node_attachment.created','node_attachment',NEW.node_attachment_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'work_session_id',NEW.work_session_id,
                    'session_epoch',NEW.session_epoch,
                    'node_instance_id',NEW.node_instance_id,
                    'authority_epoch',NEW.authority_epoch,
                    'hard_expires_at',NEW.hard_expires_at,
                    'revoked_at',NEW.revoked_at
                ),
                NEW.attached_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_node_attachment_changed
        AFTER UPDATE ON logical_agent_node_attachments
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'node_attachment.changed','node_attachment',NEW.node_attachment_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'work_session_id',NEW.work_session_id,
                    'session_epoch',NEW.session_epoch,
                    'node_instance_id',NEW.node_instance_id,
                    'authority_epoch',NEW.authority_epoch,
                    'hard_expires_at',NEW.hard_expires_at,
                    'revoked_at',NEW.revoked_at
                ),
                COALESCE(NEW.revoked_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_attachment_presence_created
        AFTER INSERT ON persistent_attachment_presence
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'attachment_presence.changed','attachment_presence',NEW.node_attachment_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'work_session_id',NEW.work_session_id,
                    'session_epoch',NEW.session_epoch,
                    'node_instance_id',NEW.node_instance_id,
                    'task_summary',NEW.task_summary,
                    'intent',NEW.intent,
                    'work_scope_json',NEW.work_scope_json,
                    'details_json',NEW.details_json,
                    'current_step',NEW.current_step,
                    'intent_updated_at',NEW.intent_updated_at,
                    'last_activity_at',NEW.last_activity_at
                ),
                NEW.last_activity_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_attachment_presence_changed
        AFTER UPDATE ON persistent_attachment_presence
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'attachment_presence.changed','attachment_presence',NEW.node_attachment_id,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'work_session_id',NEW.work_session_id,
                    'session_epoch',NEW.session_epoch,
                    'node_instance_id',NEW.node_instance_id,
                    'task_summary',NEW.task_summary,
                    'intent',NEW.intent,
                    'work_scope_json',NEW.work_scope_json,
                    'details_json',NEW.details_json,
                    'current_step',NEW.current_step,
                    'intent_updated_at',NEW.intent_updated_at,
                    'last_activity_at',NEW.last_activity_at
                ),
                NEW.last_activity_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_message_obligation_created
        AFTER INSERT ON persistent_message_obligations
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'message_obligation.created','message_obligation',NEW.message_ref,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'sender_agent_id',NEW.sender_agent_id,
                    'text',NEW.text,
                    'require_reply',NEW.require_reply,
                    'alert',NEW.alert,
                    'gate_revision',NEW.gate_revision,
                    'resolved_at',NEW.resolved_at,
                    'resolution',NEW.resolution
                ),
                NEW.created_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_message_obligation_changed
        AFTER UPDATE ON persistent_message_obligations
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'message_obligation.changed','message_obligation',NEW.message_ref,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'sender_agent_id',NEW.sender_agent_id,
                    'text',NEW.text,
                    'require_reply',NEW.require_reply,
                    'alert',NEW.alert,
                    'gate_revision',NEW.gate_revision,
                    'resolved_at',NEW.resolved_at,
                    'resolution',NEW.resolution
                ),
                COALESCE(NEW.resolved_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_persistent_claim_created
        AFTER INSERT ON work_claims
        WHEN NEW.owner_kind='logical_agent'
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'work_claim.created','work_claim',CAST(NEW.id AS TEXT),NULL,
                json_object(
                    'namespace',NEW.namespace,
                    'task_id',NEW.task_id,
                    'owner_kind',NEW.owner_kind,
                    'owner_id',NEW.owner_id,
                    'claimed_at',NEW.claimed_at,
                    'released_at',NEW.released_at
                ),
                NEW.claimed_at
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_persistent_claim_released
        AFTER UPDATE OF released_at,owner_id ON work_claims
        WHEN NEW.owner_kind='logical_agent'
          AND (OLD.released_at IS NOT NEW.released_at OR OLD.owner_id IS NOT NEW.owner_id)
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'work_claim.changed','work_claim',CAST(NEW.id AS TEXT),NULL,
                json_object(
                    'namespace',NEW.namespace,
                    'task_id',NEW.task_id,
                    'owner_kind',NEW.owner_kind,
                    'owner_id',NEW.owner_id,
                    'claimed_at',NEW.claimed_at,
                    'released_at',NEW.released_at
                ),
                COALESCE(NEW.released_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        END;

        CREATE TRIGGER IF NOT EXISTS tr_event_command_attribution
        AFTER INSERT ON command_agent_attribution
        WHEN NEW.logical_agent_id IS NOT NULL
        BEGIN
            INSERT INTO instance_events(
                event_type,entity_type,entity_id,actor_id,payload_json,created_at
            ) VALUES(
                'command.attribution','command',NEW.command_hash,NULL,
                json_object(
                    'logical_agent_id',NEW.logical_agent_id,
                    'work_session_id',NEW.work_session_id,
                    'session_epoch',NEW.session_epoch,
                    'command_type',NEW.command_type
                ),
                NEW.created_at
            );
        END;
        """
    )


class EventJournalStore:
    def __init__(self, path, *, max_rows: int = DEFAULT_EVENT_MAX_ROWS):
        if int(max_rows) < 1:
            raise ValueError("max_rows must be positive")
        self.path = path
        self.max_rows = int(max_rows)

    async def initialize(self) -> None:
        secure_database_path(self.path)
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS instance_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_instance_events_type_seq
                    ON instance_events(event_type,seq);
                """
            )
            await db.commit()

    async def append(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        *,
        actor_id: str | None = None,
        payload: Any = None,
        created_at: str | None = None,
    ) -> int:
        event_type = str(event_type).strip()
        entity_type = str(entity_type).strip()
        entity_id = str(entity_id).strip()
        if not event_type or not entity_type or not entity_id:
            raise ValueError("event_type, entity_type and entity_id are required")
        created_at = created_at or utc_text()
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "INSERT INTO instance_events("
                "event_type,entity_type,entity_id,actor_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    event_type,
                    entity_type,
                    entity_id,
                    actor_id,
                    compact_payload(payload),
                    created_at,
                ),
            )
            await db.execute(
                "DELETE FROM instance_events WHERE seq IN ("
                "SELECT seq FROM instance_events ORDER BY seq DESC LIMIT -1 OFFSET ?"
                ")",
                (self.max_rows,),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def high_water_seq(self) -> int:
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            row = await (
                await db.execute("SELECT COALESCE(MAX(seq),0) FROM instance_events")
            ).fetchone()
        return int(row[0])

    async def read_before(self, *, before: int, limit: int = DEFAULT_EVENT_LIMIT) -> dict:
        before = int(before)
        if before < 1:
            raise ValueError("before must be positive")
        limit = max(1, min(int(limit), MAX_EVENT_LIMIT))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            bounds = await (
                await db.execute(
                    "SELECT MIN(seq),COALESCE(MAX(seq),0) FROM instance_events"
                )
            ).fetchone()
            oldest = int(bounds[0]) if bounds[0] is not None else None
            high_water = int(bounds[1])
            rows = await (
                await db.execute(
                    "SELECT seq,event_type,entity_type,entity_id,actor_id,payload_json,created_at "
                    "FROM instance_events WHERE seq<? ORDER BY seq DESC LIMIT ?",
                    (before, limit),
                )
            ).fetchall()

        rows = list(reversed(rows))
        events = [
            {
                "seq": int(row[0]),
                "event_type": row[1],
                "entity_type": row[2],
                "entity_id": row[3],
                "actor_id": row[4],
                "payload": json.loads(row[5]),
                "created_at": row[6],
            }
            for row in rows
        ]
        return {
            "events": events,
            "since": 0,
            "next_cursor": events[-1]["seq"] if events else max(0, before - 1),
            "oldest_seq": oldest,
            "high_water_seq": high_water,
            "gap": False,
            "gap_from_seq": None,
            "gap_to_seq": None,
        }

    async def read(self, *, since: int = 0, limit: int = DEFAULT_EVENT_LIMIT) -> dict:
        since = int(since)
        if since < 0:
            raise ValueError("since must be non-negative")
        limit = max(1, min(int(limit), MAX_EVENT_LIMIT))
        async with aiosqlite.connect(self.path, timeout=1.0) as db:
            bounds = await (
                await db.execute(
                    "SELECT MIN(seq),COALESCE(MAX(seq),0) FROM instance_events"
                )
            ).fetchone()
            oldest = int(bounds[0]) if bounds[0] is not None else None
            high_water = int(bounds[1])
            gap = bool(oldest is not None and since < oldest - 1)
            rows = await (
                await db.execute(
                    "SELECT seq,event_type,entity_type,entity_id,actor_id,payload_json,created_at "
                    "FROM instance_events WHERE seq>? ORDER BY seq LIMIT ?",
                    (since, limit),
                )
            ).fetchall()

        events = [
            {
                "seq": int(row[0]),
                "event_type": row[1],
                "entity_type": row[2],
                "entity_id": row[3],
                "actor_id": row[4],
                "payload": json.loads(row[5]),
                "created_at": row[6],
            }
            for row in rows
        ]
        return {
            "events": events,
            "since": since,
            "next_cursor": events[-1]["seq"] if events else since,
            "oldest_seq": oldest,
            "high_water_seq": high_water,
            "gap": gap,
            "gap_from_seq": since + 1 if gap else None,
            "gap_to_seq": oldest - 1 if gap and oldest is not None else None,
        }

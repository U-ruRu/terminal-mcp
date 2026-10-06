# ruff: noqa: E501
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlite3 import IntegrityError

import aiosqlite

from terminal_mcp.core.models import Command
from terminal_mcp.core.orchestration import utc_text
from terminal_mcp.storage.events import install_event_journal
from terminal_mcp.storage.output import (
    DEFAULT_COMMAND_MAX_BYTES,
    DEFAULT_LINE_MAX_BYTES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    DEFAULT_PRUNE_ROWS,
    DEFAULT_TARGET_BYTES,
    OutputStore,
)
from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import SqliteDiagnostics, observed_connection
from terminal_mcp.storage.work_windows import install_work_window_schema

SCHEMA_VERSION = 20

_COMMAND_COLUMNS = (
    "hash,cmd,status,pid,exit_code,error,started_at,finished_at,"
    "queue_id,queue_sequence,enqueued_at,claimed_at"
)
_SCRUBBED_COMMAND_BODY = "[command body pruned]"


class SqliteRepository:
    def __init__(
        self,
        path,
        output_path=None,
        *,
        output_line_max_bytes=DEFAULT_LINE_MAX_BYTES,
        output_command_max_bytes=DEFAULT_COMMAND_MAX_BYTES,
        output_target_bytes=DEFAULT_TARGET_BYTES,
        output_max_bytes=DEFAULT_MAX_BYTES,
        output_max_rows=DEFAULT_MAX_ROWS,
        output_prune_rows=DEFAULT_PRUNE_ROWS,
    ):
        self.path = path
        output_path = output_path or Path(path).with_name("output.sqlite3")
        self.output = OutputStore(
            output_path,
            line_max_bytes=output_line_max_bytes,
            command_max_bytes=output_command_max_bytes,
            target_bytes=output_target_bytes,
            max_bytes=output_max_bytes,
            max_rows=output_max_rows,
            prune_rows=output_prune_rows,
        )
        self.output_line_max_bytes = self.output.line_max_bytes
        self.events = None
        self.metrics = None
        self.sqlite_diagnostics = SqliteDiagnostics("durable")

    def configure_observability(self, events, metrics):
        self.events = events
        self.metrics = metrics
        self.sqlite_diagnostics.configure(events, metrics)
        self.output.configure_observability(events, metrics)

    @asynccontextmanager
    async def _connect(
        self,
        operation="unknown",
        *,
        command_hash=None,
        execution_outcome=None,
        durable_finalization_outcome=None,
        ensure_wal=False,
    ):
        started = time.monotonic()
        async with observed_connection(
            aiosqlite.connect,
            self.path,
            busy_timeout=1.0,
            diagnostics=self.sqlite_diagnostics,
            operation=operation,
            pragmas=(
                *(("PRAGMA journal_mode=WAL",) if ensure_wal else ()),
                "PRAGMA synchronous=NORMAL",
                "PRAGMA busy_timeout=1000",
                "PRAGMA foreign_keys=ON",
            ),
            command_hash=command_hash,
            execution_outcome=execution_outcome,
            durable_finalization_outcome=durable_finalization_outcome,
        ) as db:
            outcome = "error"
            try:
                yield db
                outcome = "success"
            finally:
                if self.metrics:
                    self.metrics.inc(
                        "terminal_mcp_sqlite_operations_total",
                        (("operation", operation), ("outcome", outcome)),
                    )
                    self.metrics.observe(
                        "terminal_mcp_sqlite_operation_duration_seconds",
                        time.monotonic() - started,
                        (("operation", operation),),
                    )

    async def ping(self):
        async with self._connect("health") as db:
            await (await db.execute("SELECT 1")).fetchone()
        return True

    async def initialize(self):
        secure_database_path(self.path)
        await self.output.initialize()
        async with self._connect("initialize", ensure_wal=True) as db:
            current_version = int((await (await db.execute("PRAGMA user_version")).fetchone())[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands(
                    hash TEXT PRIMARY KEY, cmd TEXT, status TEXT,
                    pid INTEGER, exit_code INTEGER, error TEXT,
                    started_at TEXT, finished_at TEXT,
                    queue_id INTEGER, queue_sequence INTEGER,
                    enqueued_at TEXT, claimed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS command_output_state(
                    command_hash TEXT PRIMARY KEY,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    pruned_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_sessions(
                    agent_id TEXT PRIMARY KEY, registered_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                    task_summary TEXT NOT NULL, intent TEXT NOT NULL, work_scope TEXT NOT NULL, state TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '[]', current_step INTEGER NOT NULL DEFAULT 1,
                    ended_at TEXT, end_reason TEXT, preferred_queue_id INTEGER,
                    source_instance_id TEXT, global_expires_at TEXT,
                    intent_scopes TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS agent_admission_proposals(
                    agent_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_task_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    intent TEXT NOT NULL, work_scope TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS command_agent_attribution(
                    command_hash TEXT PRIMARY KEY, agent_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    command_type TEXT NOT NULL, command_preview TEXT NOT NULL,
                    logical_agent_id TEXT, work_session_id TEXT, session_epoch INTEGER
                );
                CREATE TABLE IF NOT EXISTS agent_activity_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    tool TEXT NOT NULL, command_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS coordination_messages(
                    message_hash TEXT PRIMARY KEY, sender_agent_id TEXT NOT NULL,
                    target_name TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    require_reply INTEGER NOT NULL DEFAULT 0, alert INTEGER NOT NULL DEFAULT 0,
                    delivery_mode TEXT NOT NULL DEFAULT 'legacy',
                    task_namespace TEXT, task_id TEXT
                );
                CREATE TABLE IF NOT EXISTS coordination_message_recipients(
                    message_hash TEXT NOT NULL, recipient_agent_id TEXT NOT NULL,
                    delivered_at TEXT, first_seen_at TEXT, last_seen_at TEXT,
                    seen_count INTEGER NOT NULL DEFAULT 0, read_at TEXT,
                    replied_at TEXT, reply_message_hash TEXT,
                    PRIMARY KEY(message_hash, recipient_agent_id)
                );
                CREATE TABLE IF NOT EXISTS instance_context(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL CHECK(length(summary) <= 100),
                    content TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS work_items(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK(lane IN ('implementation','review','release','integration','general')),
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(state IN ('ready','in_progress','blocked','deferred','done')),
                    description TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL DEFAULT '',
                    isolation_hint TEXT NOT NULL DEFAULT 'none',
                    resource_json TEXT NOT NULL DEFAULT '{}', reviews_json TEXT NOT NULL DEFAULT '[]',
                    cooperative INTEGER NOT NULL DEFAULT 0 CHECK(cooperative IN (0,1)),
                    checkpoint_json TEXT NOT NULL DEFAULT '{}', candidate_ref TEXT, result_json TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]', state_changed_at TEXT NOT NULL, ready_since TEXT,
                    archived_at TEXT, archive_note TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, task_id)
                );
                CREATE TABLE IF NOT EXISTS work_claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, claimed_at TEXT NOT NULL, released_at TEXT,
                    claim_intent TEXT NOT NULL DEFAULT '',
                    owner_kind TEXT NOT NULL DEFAULT 'legacy_session'
                        CHECK(owner_kind IN ('legacy_session','logical_agent')),
                    owner_id TEXT NOT NULL,
                    FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS work_dependencies(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                    dependency_namespace TEXT NOT NULL, dependency_task_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,task_id,dependency_namespace,dependency_task_id),
                    FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS work_relations(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                    related_namespace TEXT NOT NULL, related_task_id TEXT NOT NULL,
                    relation_kind TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT,
                    PRIMARY KEY(namespace,task_id,related_namespace,related_task_id,relation_kind),
                    FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS work_reviews(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL, candidate_ref TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL CHECK(dimension IN ('A','C','R')), verdict TEXT NOT NULL,
                    agent_id TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]', reviewed_at TEXT NOT NULL,
                    PRIMARY KEY(namespace,task_id,candidate_ref,dimension),
                    FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS work_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, agent_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    logical_agent_id TEXT, work_session_id TEXT, session_epoch INTEGER,
                    FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS logical_agents(
                    logical_agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'suspended'
                        CHECK(state IN ('suspended','armed','active','stopping','deleting','deleted')),
                    authority_node_id TEXT NOT NULL, authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
                    slot_revision INTEGER NOT NULL DEFAULT 1 CHECK(slot_revision > 0),
                    selector_generation INTEGER NOT NULL DEFAULT 1 CHECK(selector_generation > 0),
                    auth_generation INTEGER NOT NULL DEFAULT 1 CHECK(auth_generation > 0),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT, tombstone_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS logical_agent_selectors(
                    selector TEXT PRIMARY KEY CHECK(length(selector)=4), logical_agent_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0), created_at TEXT NOT NULL,
                    retired_at TEXT, tombstoned_at TEXT,
                    UNIQUE(logical_agent_id,generation),
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS logical_agent_arms(
                    logical_agent_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation > 0),
                    armed_at TEXT NOT NULL, armed_until TEXT NOT NULL,
                    captured_duration_seconds INTEGER NOT NULL CHECK(captured_duration_seconds > 0),
                    selector_generation INTEGER NOT NULL CHECK(selector_generation > 0),
                    auth_generation INTEGER NOT NULL CHECK(auth_generation > 0),
                    slot_revision INTEGER NOT NULL CHECK(slot_revision > 0), consumed_at TEXT, revoked_at TEXT,
                    PRIMARY KEY(logical_agent_id,generation),
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS logical_agent_work_sessions(
                    work_session_id TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL,
                    session_epoch INTEGER NOT NULL CHECK(session_epoch > 0),
                    authority_node_id TEXT NOT NULL, authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
                    started_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL, auth_principal_id TEXT,
                    auth_generation INTEGER NOT NULL CHECK(auth_generation > 0),
                    state TEXT NOT NULL CHECK(state IN ('active','stopping','ended','expired','suspended','failed')),
                    origin_instance_id TEXT, ended_at TEXT, end_reason TEXT,
                    UNIQUE(logical_agent_id,session_epoch),
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS persistent_agent_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, logical_agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, principal_id TEXT NOT NULL, work_session_id TEXT,
                    session_epoch INTEGER, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS logical_agent_rearms(
                    logical_agent_id TEXT PRIMARY KEY, work_session_id TEXT NOT NULL,
                    rearm_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    cancelled_at TEXT, rearmed_at TEXT,
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT,
                    FOREIGN KEY(work_session_id) REFERENCES logical_agent_work_sessions(work_session_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_logical_agent_rearms_due
                    ON logical_agent_rearms(cancelled_at,rearmed_at,rearm_at);
                CREATE TABLE IF NOT EXISTS persistent_idempotency(
                    logical_agent_id TEXT NOT NULL, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','complete')),
                    result_json TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(logical_agent_id,operation,idempotency_key),
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS logical_agent_node_attachments(
                    node_attachment_id TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL,
                    work_session_id TEXT NOT NULL, session_epoch INTEGER NOT NULL CHECK(session_epoch > 0),
                    node_instance_id TEXT NOT NULL, authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
                    attached_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL, revoked_at TEXT,
                    UNIQUE(logical_agent_id,work_session_id,session_epoch,node_instance_id),
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS persistent_attachment_presence(
                    node_attachment_id TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL,
                    work_session_id TEXT NOT NULL, session_epoch INTEGER NOT NULL CHECK(session_epoch > 0),
                    node_instance_id TEXT NOT NULL, task_summary TEXT NOT NULL DEFAULT '',
                    intent TEXT NOT NULL DEFAULT '', work_scope_json TEXT NOT NULL DEFAULT '[]',
                    details_json TEXT NOT NULL DEFAULT '[]', current_step INTEGER NOT NULL DEFAULT 1 CHECK(current_step > 0),
                    intent_updated_at TEXT NOT NULL, last_activity_at TEXT NOT NULL,
                    FOREIGN KEY(node_attachment_id) REFERENCES logical_agent_node_attachments(node_attachment_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS persistent_command_permits(
                    command_hash TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL, work_session_id TEXT NOT NULL,
                    session_epoch INTEGER NOT NULL CHECK(session_epoch > 0), authority_node_id TEXT NOT NULL,
                    authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0), node_attachment_id TEXT NOT NULL,
                    node_instance_id TEXT NOT NULL, scope TEXT NOT NULL, hard_expires_at TEXT NOT NULL,
                    permit_expires_at TEXT NOT NULL, signature TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT,
                    gate_revision INTEGER NOT NULL DEFAULT 1 CHECK(gate_revision > 0),
                    operation TEXT NOT NULL DEFAULT 'run',
                    ttl_ms INTEGER NOT NULL DEFAULT 10000 CHECK(ttl_ms > 0),
                    slot_revision INTEGER,
                    principal_id TEXT
                );
                CREATE TABLE IF NOT EXISTS fleet_request_dedup(
                    issuer_node_id TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','complete')),
                    result_json TEXT, retain_until TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(issuer_node_id,operation,request_id)
                );
                CREATE TABLE IF NOT EXISTS persistent_fleet_gates(
                    logical_agent_id TEXT PRIMARY KEY,
                    gate_revision INTEGER NOT NULL DEFAULT 1 CHECK(gate_revision > 0),
                    blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
                    reason TEXT, updated_at TEXT NOT NULL,
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS persistent_message_obligations(
                    message_ref TEXT PRIMARY KEY, logical_agent_id TEXT NOT NULL,
                    sender_agent_id TEXT NOT NULL, text TEXT NOT NULL,
                    require_reply INTEGER NOT NULL DEFAULT 0 CHECK(require_reply IN (0,1)),
                    alert INTEGER NOT NULL DEFAULT 0 CHECK(alert IN (0,1)),
                    gate_revision INTEGER NOT NULL CHECK(gate_revision > 0),
                    created_at TEXT NOT NULL,
                    first_seen_at TEXT, last_seen_at TEXT,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    read_at TEXT, replied_at TEXT, reply_message_ref TEXT,
                    resolved_at TEXT, resolution TEXT,
                    FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS persistent_message_receipts(
                    message_ref TEXT NOT NULL, node_instance_id TEXT NOT NULL,
                    seen_at TEXT, read_at TEXT, replied_at TEXT, reply_message_ref TEXT,
                    PRIMARY KEY(message_ref,node_instance_id),
                    FOREIGN KEY(message_ref) REFERENCES persistent_message_obligations(message_ref) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_agent_sessions_last_activity ON agent_sessions(last_activity_at DESC);
                CREATE INDEX IF NOT EXISTS ix_agent_task_events_agent ON agent_task_events(agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_agent ON command_agent_attribution(agent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS ix_command_agent_hash ON command_agent_attribution(command_hash);
                CREATE INDEX IF NOT EXISTS ix_agent_activity_agent ON agent_activity_events(agent_id, id DESC);
                CREATE INDEX IF NOT EXISTS ix_coord_message_recipient_legacy
                    ON coordination_message_recipients(recipient_agent_id, read_at);
                CREATE INDEX IF NOT EXISTS ix_work_items_state
                    ON work_items(namespace,state,priority DESC,updated_at DESC);
                CREATE INDEX IF NOT EXISTS ix_work_items_lane
                    ON work_items(namespace,lane,state,priority DESC,updated_at DESC);
                CREATE INDEX IF NOT EXISTS ix_work_claims_active
                    ON work_claims(namespace,task_id,released_at,claimed_at DESC);
                CREATE INDEX IF NOT EXISTS ix_work_claims_current
                    ON work_claims(released_at,id);
                CREATE INDEX IF NOT EXISTS ix_logical_work_sessions_current
                    ON logical_agent_work_sessions(state,work_session_id);
                CREATE INDEX IF NOT EXISTS ix_node_attachments_current
                    ON logical_agent_node_attachments(revoked_at,node_attachment_id);
                CREATE INDEX IF NOT EXISTS ix_message_obligations_current
                    ON persistent_message_obligations(resolved_at,message_ref);
                CREATE INDEX IF NOT EXISTS ix_commands_current
                    ON commands(status,hash);
                CREATE INDEX IF NOT EXISTS ix_work_dependencies_target
                    ON work_dependencies(dependency_namespace,dependency_task_id);
                CREATE INDEX IF NOT EXISTS ix_work_relations_source
                    ON work_relations(namespace,task_id,relation_kind);
                CREATE INDEX IF NOT EXISTS ix_work_relations_target
                    ON work_relations(related_namespace,related_task_id,relation_kind);
                CREATE INDEX IF NOT EXISTS ix_work_reviews_task
                    ON work_reviews(namespace,task_id,candidate_ref,dimension);
                CREATE INDEX IF NOT EXISTS ix_work_events_task
                    ON work_events(namespace,task_id,id DESC);
                CREATE TABLE IF NOT EXISTS fleet_agent_identities(
                    source_instance_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    record_json TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(source_instance_id,agent_id)
                );
                CREATE TABLE IF NOT EXISTS fleet_peer_outbox(
                    peer_instance_id TEXT NOT NULL,
                    source_instance_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    record_json TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY(peer_instance_id,source_instance_id,agent_id)
                );
                CREATE INDEX IF NOT EXISTS ix_fleet_identities_agent
                    ON fleet_agent_identities(agent_id,received_at DESC);
                CREATE INDEX IF NOT EXISTS ix_fleet_outbox_peer
                    ON fleet_peer_outbox(peer_instance_id,queued_at);
                CREATE TABLE IF NOT EXISTS fleet_finish_outbox(
                    origin_instance_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY(origin_instance_id,agent_id)
                );
                CREATE INDEX IF NOT EXISTS ix_fleet_finish_outbox_origin
                    ON fleet_finish_outbox(origin_instance_id,queued_at);
                CREATE TABLE IF NOT EXISTS fleet_session_update_outbox(
                    origin_instance_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    source_instance_id TEXT NOT NULL,
                    activity_at TEXT NOT NULL,
                    intent TEXT,
                    intent_step INTEGER,
                    intent_updated_at TEXT,
                    queued_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY(origin_instance_id,agent_id,source_instance_id)
                );
                CREATE INDEX IF NOT EXISTS ix_fleet_session_update_outbox_origin
                    ON fleet_session_update_outbox(origin_instance_id,queued_at);
                """
            )
            await self._migrate(db)
            await install_work_window_schema(db)
            await install_event_journal(db)
            legacy_output_migrated = await self._migrate_legacy_output(db)
            recovered_at = utc_text()
            await db.execute(
                "UPDATE commands SET status='failed', error='startup.recover: application restarted', "
                "finished_at=COALESCE(finished_at, ?) WHERE status IN ('queued', 'running')",
                (recovered_at,),
            )
            await self._scrub_pruned_command_bodies(db)
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await db.commit()
            if legacy_output_migrated:
                await db.execute("VACUUM")

    async def _migrate(self, db):
        async def add_columns(table, definitions):
            columns = {
                row[1] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
            }
            for name, definition in definitions:
                if name not in columns:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            return columns

        command_columns = await add_columns(
            "commands",
            [
                ("started_at", "TEXT"),
                ("finished_at", "TEXT"),
                ("queue_id", "INTEGER"),
                ("queue_sequence", "INTEGER"),
                ("enqueued_at", "TEXT"),
                ("claimed_at", "TEXT"),
            ],
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_commands_terminal_recent "
            "ON commands(finished_at DESC,hash) "
            "WHERE status IN ('completed','failed','cancelled')"
        )
        session_columns = await add_columns(
            "agent_sessions",
            [
                ("details", "TEXT NOT NULL DEFAULT '[]'"),
                ("current_step", "INTEGER NOT NULL DEFAULT 1"),
                ("ended_at", "TEXT"),
                ("end_reason", "TEXT"),
                ("preferred_queue_id", "INTEGER"),
                ("source_instance_id", "TEXT"),
                ("global_expires_at", "TEXT"),
                ("intent_scopes", "TEXT NOT NULL DEFAULT '{}'"),
            ],
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS agent_admission_proposals("
            "agent_id TEXT PRIMARY KEY,created_at TEXT NOT NULL)"
        )
        await add_columns("agent_task_events", [("step", "INTEGER NOT NULL DEFAULT 1")])
        await add_columns(
            "coordination_messages",
            [
                ("require_reply", "INTEGER NOT NULL DEFAULT 0"),
                ("alert", "INTEGER NOT NULL DEFAULT 0"),
                ("delivery_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("task_namespace", "TEXT"),
                ("task_id", "TEXT"),
            ],
        )
        recipient_columns = await add_columns(
            "coordination_message_recipients",
            [
                ("delivered_at", "TEXT"),
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("seen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("replied_at", "TEXT"),
                ("reply_message_hash", "TEXT"),
            ],
        )
        await add_columns(
            "work_items",
            [
                ("result_json", "TEXT"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("state_changed_at", "TEXT"),
                ("ready_since", "TEXT"),
                ("archived_at", "TEXT"),
                ("archive_note", "TEXT"),
            ],
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_items_current "
            "ON work_items(archived_at,state,namespace,task_id)"
        )
        await add_columns(
            "work_claims",
            [
                ("claim_intent", "TEXT NOT NULL DEFAULT ''"),
                ("owner_kind", "TEXT NOT NULL DEFAULT 'legacy_session'"),
                ("owner_id", "TEXT"),
            ],
        )
        await db.execute(
            "UPDATE work_claims SET claim_intent='legacy claim' "
            "WHERE claim_intent IS NULL OR claim_intent=''"
        )
        await db.execute(
            "UPDATE work_claims SET owner_kind='legacy_session',owner_id=agent_id "
            "WHERE owner_id IS NULL OR owner_id=''"
        )
        await add_columns(
            "command_agent_attribution",
            [
                ("logical_agent_id", "TEXT"),
                ("work_session_id", "TEXT"),
                ("session_epoch", "INTEGER"),
            ],
        )
        await add_columns(
            "work_events",
            [
                ("logical_agent_id", "TEXT"),
                ("work_session_id", "TEXT"),
                ("session_epoch", "INTEGER"),
            ],
        )
        await self._migrate_work_items_archive_lifecycle(db)
        # Schema v10: add creator-supplied isolation metadata after the v9 work_items rebuild.
        await add_columns("work_items", [("isolation_hint", "TEXT NOT NULL DEFAULT 'none'")])
        # Schema v18: task execution state is explicit and independent from claims.
        await self._migrate_work_items_explicit_state(db)
        # Schema v19: task input/output refs and output-state-bound review history.
        await self._migrate_task_refs(db)
        await db.execute(
            "UPDATE work_items SET state_changed_at=COALESCE(state_changed_at,updated_at)"
        )
        await db.execute(
            "UPDATE work_items SET ready_since=CASE "
            "WHEN state='ready' THEN COALESCE(ready_since,updated_at) ELSE NULL END"
        )
        if "details" not in session_columns:
            await db.execute(
                "UPDATE agent_sessions SET state='forced',ended_at=COALESCE(ended_at,last_activity_at),"
                "end_reason='schema_upgrade' WHERE state='active'"
            )
        await db.execute(
            "UPDATE agent_sessions SET state='forced',ended_at=COALESCE(ended_at,last_activity_at),"
            "end_reason=COALESCE(end_reason,'legacy_expired') WHERE state='expired'"
        )
        if "queue_id" not in command_columns:
            await db.execute(
                "UPDATE commands SET queue_id=1, queue_sequence=rowid, enqueued_at=COALESCE(enqueued_at, started_at) "
                "WHERE queue_id IS NULL AND status='queued'"
            )
        if "delivered_at" not in recipient_columns:
            await db.execute(
                "UPDATE coordination_message_recipients SET delivered_at=("
                "SELECT created_at FROM coordination_messages m WHERE m.message_hash=coordination_message_recipients.message_hash"
                ") WHERE delivered_at IS NULL"
            )
        await db.execute("DROP INDEX IF EXISTS ux_work_claims_agent_active")
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_work_claims_owner_active "
            "ON work_claims(namespace,task_id,owner_kind,owner_id) WHERE released_at IS NULL"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_claims_owner "
            "ON work_claims(owner_kind,owner_id,released_at,claimed_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_logical_agent_sessions_agent "
            "ON logical_agent_work_sessions(logical_agent_id,session_epoch DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_audit_agent "
            "ON persistent_agent_audit(logical_agent_id,id DESC)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS persistent_attachment_presence("
            "node_attachment_id TEXT PRIMARY KEY,logical_agent_id TEXT NOT NULL,"
            "work_session_id TEXT NOT NULL,session_epoch INTEGER NOT NULL CHECK(session_epoch > 0),"
            "node_instance_id TEXT NOT NULL,task_summary TEXT NOT NULL DEFAULT '',"
            "intent TEXT NOT NULL DEFAULT '',work_scope_json TEXT NOT NULL DEFAULT '[]',"
            "details_json TEXT NOT NULL DEFAULT '[]',current_step INTEGER NOT NULL DEFAULT 1 CHECK(current_step > 0),"
            "intent_updated_at TEXT NOT NULL,last_activity_at TEXT NOT NULL,"
            "FOREIGN KEY(node_attachment_id) REFERENCES logical_agent_node_attachments(node_attachment_id) ON DELETE CASCADE)"
        )
        await add_columns(
            "persistent_command_permits",
            [
                ("gate_revision", "INTEGER NOT NULL DEFAULT 1"),
                ("operation", "TEXT NOT NULL DEFAULT 'run'"),
                ("ttl_ms", "INTEGER NOT NULL DEFAULT 10000"),
                ("slot_revision", "INTEGER"),
                ("principal_id", "TEXT"),
            ],
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS fleet_request_dedup("
            "issuer_node_id TEXT NOT NULL,operation TEXT NOT NULL,request_id TEXT NOT NULL,"
            "request_fingerprint TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','complete')),"
            "result_json TEXT,retain_until TEXT NOT NULL,created_at TEXT NOT NULL,"
            "PRIMARY KEY(issuer_node_id,operation,request_id))"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS persistent_fleet_gates("
            "logical_agent_id TEXT PRIMARY KEY,"
            "gate_revision INTEGER NOT NULL DEFAULT 1 CHECK(gate_revision > 0),"
            "blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),"
            "reason TEXT,updated_at TEXT NOT NULL,"
            "FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS persistent_message_obligations("
            "message_ref TEXT PRIMARY KEY,logical_agent_id TEXT NOT NULL,"
            "sender_agent_id TEXT NOT NULL,text TEXT NOT NULL,"
            "require_reply INTEGER NOT NULL DEFAULT 0 CHECK(require_reply IN (0,1)),"
            "alert INTEGER NOT NULL DEFAULT 0 CHECK(alert IN (0,1)),"
            "gate_revision INTEGER NOT NULL CHECK(gate_revision > 0),"
            "created_at TEXT NOT NULL,first_seen_at TEXT,last_seen_at TEXT,"
            "seen_count INTEGER NOT NULL DEFAULT 0,read_at TEXT,replied_at TEXT,"
            "reply_message_ref TEXT,resolved_at TEXT,resolution TEXT,"
            "FOREIGN KEY(logical_agent_id) REFERENCES logical_agents(logical_agent_id) ON DELETE RESTRICT)"
        )
        await add_columns(
            "persistent_message_obligations",
            [
                ("first_seen_at", "TEXT"),
                ("last_seen_at", "TEXT"),
                ("seen_count", "INTEGER NOT NULL DEFAULT 0"),
                ("read_at", "TEXT"),
                ("replied_at", "TEXT"),
                ("reply_message_ref", "TEXT"),
            ],
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS persistent_message_receipts("
            "message_ref TEXT NOT NULL,node_instance_id TEXT NOT NULL,"
            "seen_at TEXT,read_at TEXT,replied_at TEXT,reply_message_ref TEXT,"
            "PRIMARY KEY(message_ref,node_instance_id),"
            "FOREIGN KEY(message_ref) REFERENCES persistent_message_obligations(message_ref) ON DELETE CASCADE)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_fleet_request_dedup_retain "
            "ON fleet_request_dedup(retain_until,state)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_obligations_agent "
            "ON persistent_message_obligations(logical_agent_id,resolved_at,created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_receipts_message "
            "ON persistent_message_receipts(message_ref,read_at,replied_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_attachments_session "
            "ON logical_agent_node_attachments(logical_agent_id,work_session_id,session_epoch,revoked_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_presence_session "
            "ON persistent_attachment_presence(logical_agent_id,work_session_id,session_epoch,node_instance_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_permits_session "
            "ON persistent_command_permits(logical_agent_id,work_session_id,session_epoch,revoked_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_persistent_permits_expiry "
            "ON persistent_command_permits(revoked_at,hard_expires_at,permit_expires_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_commands_queue ON commands(queue_id, status, queue_sequence)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_coord_message_recipient "
            "ON coordination_message_recipients(recipient_agent_id, read_at, replied_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_coord_messages_task "
            "ON coordination_messages(task_namespace,task_id,created_at)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS fleet_finish_outbox("
            "origin_instance_id TEXT NOT NULL,agent_id TEXT NOT NULL,ended_at TEXT NOT NULL,"
            "reason TEXT NOT NULL,queued_at TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,"
            "last_attempt_at TEXT,last_error TEXT,"
            "PRIMARY KEY(origin_instance_id,agent_id))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_fleet_finish_outbox_origin "
            "ON fleet_finish_outbox(origin_instance_id,queued_at)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS work_relations("
            "namespace TEXT NOT NULL,task_id TEXT NOT NULL,"
            "related_namespace TEXT NOT NULL,related_task_id TEXT NOT NULL,"
            "relation_kind TEXT NOT NULL,created_at TEXT NOT NULL,created_by TEXT,"
            "PRIMARY KEY(namespace,task_id,related_namespace,related_task_id,relation_kind),"
            "FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_relations_source "
            "ON work_relations(namespace,task_id,relation_kind)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_relations_target "
            "ON work_relations(related_namespace,related_task_id,relation_kind)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_items_archived ON work_items(namespace,archived_at)"
        )
        await db.execute("DROP INDEX IF EXISTS ix_work_items_state")
        await db.execute("DROP INDEX IF EXISTS ix_work_items_lane")
        await db.execute(
            "CREATE INDEX ix_work_items_state "
            "ON work_items(namespace,state,priority DESC,ready_since,updated_at)"
        )
        await db.execute(
            "CREATE INDEX ix_work_items_lane "
            "ON work_items(namespace,lane,state,priority DESC,ready_since,updated_at)"
        )

    async def _migrate_task_refs(self, db):
        item_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(work_items)")).fetchall()
        }
        for name, definition in (
            ("input_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("output_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("output_state_id", "INTEGER"),
        ):
            if name not in item_columns:
                await db.execute(f"ALTER TABLE work_items ADD COLUMN {name} {definition}")

        await db.execute(
            "CREATE TABLE IF NOT EXISTS work_output_states("
            "output_state_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "namespace TEXT NOT NULL,task_id TEXT NOT NULL,"
            "output_refs_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,"
            "FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) ON DELETE CASCADE)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_output_states_task "
            "ON work_output_states(namespace,task_id,output_state_id)"
        )

        review_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(work_reviews)")).fetchall()
        }
        if "output_state_id" not in review_columns:
            await db.execute("ALTER TABLE work_reviews ADD COLUMN output_state_id INTEGER")
        if "output_refs_json" not in review_columns:
            await db.execute(
                "ALTER TABLE work_reviews ADD COLUMN output_refs_json TEXT NOT NULL DEFAULT '[]'"
            )

        tasks = await (
            await db.execute(
                "SELECT namespace,task_id,candidate_ref,created_at,"
                "input_refs_json,output_refs_json,output_state_id FROM work_items"
            )
        ).fetchall()
        for (
            namespace,
            task_id,
            candidate_ref,
            created_at,
            _input_json,
            _output_json,
            state_id,
        ) in tasks:
            current_refs = [candidate_ref] if candidate_ref else []
            current_json = json.dumps(current_refs, separators=(",", ":"), ensure_ascii=False)

            if state_id is None:
                cursor = await db.execute(
                    "INSERT INTO work_output_states(namespace,task_id,output_refs_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (namespace, task_id, current_json, created_at),
                )
                state_id = int(cursor.lastrowid)
                await db.execute(
                    "UPDATE work_items SET input_refs_json=COALESCE(input_refs_json,'[]'),"
                    "output_refs_json=?,output_state_id=? WHERE namespace=? AND task_id=?",
                    (current_json, state_id, namespace, task_id),
                )
            else:
                state = await (
                    await db.execute(
                        "SELECT output_refs_json FROM work_output_states WHERE output_state_id=? "
                        "AND namespace=? AND task_id=?",
                        (state_id, namespace, task_id),
                    )
                ).fetchone()
                if state is not None:
                    current_json = state[0]

            legacy_reviews = await (
                await db.execute(
                    "SELECT DISTINCT candidate_ref FROM work_reviews "
                    "WHERE namespace=? AND task_id=? AND output_state_id IS NULL",
                    (namespace, task_id),
                )
            ).fetchall()
            for (legacy_candidate,) in legacy_reviews:
                legacy_candidate = legacy_candidate or ""
                if legacy_candidate == (candidate_ref or ""):
                    review_state_id = state_id
                    review_refs_json = current_json
                else:
                    legacy_refs = [legacy_candidate] if legacy_candidate else []
                    review_refs_json = json.dumps(
                        legacy_refs, separators=(",", ":"), ensure_ascii=False
                    )
                    first_review = await (
                        await db.execute(
                            "SELECT MIN(reviewed_at) FROM work_reviews "
                            "WHERE namespace=? AND task_id=? AND candidate_ref=?",
                            (namespace, task_id, legacy_candidate),
                        )
                    ).fetchone()
                    state_created_at = (first_review or [None])[0] or created_at
                    cursor = await db.execute(
                        "INSERT INTO work_output_states(namespace,task_id,output_refs_json,created_at) "
                        "VALUES(?,?,?,?)",
                        (namespace, task_id, review_refs_json, state_created_at),
                    )
                    review_state_id = int(cursor.lastrowid)
                await db.execute(
                    "UPDATE work_reviews SET output_state_id=?,output_refs_json=? "
                    "WHERE namespace=? AND task_id=? AND candidate_ref=? "
                    "AND output_state_id IS NULL",
                    (
                        review_state_id,
                        review_refs_json,
                        namespace,
                        task_id,
                        legacy_candidate,
                    ),
                )

        review_info = await (await db.execute("PRAGMA table_info(work_reviews)")).fetchall()
        review_pk = [row[1] for row in review_info if int(row[5]) > 0]
        expected_pk = ["namespace", "task_id", "output_state_id", "dimension"]
        if review_pk != expected_pk:
            await db.execute("DROP TABLE IF EXISTS work_reviews_v19")
            await db.execute(
                "CREATE TABLE work_reviews_v19("
                "namespace TEXT NOT NULL,task_id TEXT NOT NULL,"
                "output_state_id INTEGER NOT NULL,"
                "output_refs_json TEXT NOT NULL DEFAULT '[]',"
                "candidate_ref TEXT NOT NULL DEFAULT '',"
                "dimension TEXT NOT NULL CHECK(dimension IN ('A','C','R')),"
                "verdict TEXT NOT NULL,agent_id TEXT NOT NULL,"
                "evidence_json TEXT NOT NULL DEFAULT '{}',"
                "warnings_json TEXT NOT NULL DEFAULT '[]',reviewed_at TEXT NOT NULL,"
                "PRIMARY KEY(namespace,task_id,output_state_id,dimension),"
                "FOREIGN KEY(namespace,task_id) REFERENCES work_items(namespace,task_id) "
                "ON DELETE CASCADE,"
                "FOREIGN KEY(output_state_id) REFERENCES work_output_states(output_state_id) "
                "ON DELETE CASCADE)"
            )
            await db.execute(
                "INSERT INTO work_reviews_v19("
                "namespace,task_id,output_state_id,output_refs_json,candidate_ref,dimension,"
                "verdict,agent_id,evidence_json,warnings_json,reviewed_at"
                ") SELECT namespace,task_id,output_state_id,output_refs_json,candidate_ref,"
                "dimension,verdict,agent_id,evidence_json,warnings_json,reviewed_at "
                "FROM work_reviews WHERE output_state_id IS NOT NULL"
            )
            await db.execute("DROP TABLE work_reviews")
            await db.execute("ALTER TABLE work_reviews_v19 RENAME TO work_reviews")

        await db.execute("DROP INDEX IF EXISTS ux_work_reviews_output_state_dimension")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_reviews_task "
            "ON work_reviews(namespace,task_id,output_state_id,dimension)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_work_reviews_output_state "
            "ON work_reviews(namespace,task_id,output_state_id,reviewed_at)"
        )

    async def _migrate_work_items_archive_lifecycle(self, db):
        row = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
            )
        ).fetchone()
        if row is None:
            return False
        table_sql = row[0] or ""
        if "'archived'" not in table_sql:
            return False

        # SQLite ignores PRAGMA foreign_keys changes while a transaction is active.
        # Commit the additive v9 columns first, disable FK enforcement outside a
        # transaction, then rebuild work_items atomically so ON DELETE CASCADE does
        # not erase durable child history (claims, dependencies, reviews, events).
        await db.commit()
        await db.execute("PRAGMA foreign_keys=OFF")
        foreign_keys = int((await (await db.execute("PRAGMA foreign_keys")).fetchone())[0])
        if foreign_keys != 0:
            raise RuntimeError("schema v9 migration could not disable foreign keys")

        try:
            await db.execute("BEGIN IMMEDIATE")
            archived = await (
                await db.execute(
                    "SELECT namespace,task_id,result_json,updated_at,archived_at,archive_note "
                    "FROM work_items WHERE state='archived' ORDER BY namespace,task_id"
                )
            ).fetchall()
            valid_states = {"ready", "blocked", "deferred", "done"}
            for namespace, task_id, result_json, updated_at, archived_at, archive_note in archived:
                events = await (
                    await db.execute(
                        "SELECT event_type,payload_json,created_at FROM work_events "
                        "WHERE namespace=? AND task_id=? ORDER BY id DESC",
                        (namespace, task_id),
                    )
                ).fetchall()
                resolved_state = None
                archive_time = archived_at
                note = archive_note
                for event_type, payload_json, created_at in events:
                    try:
                        payload = json.loads(payload_json or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                    if event_type == "archived" and archive_time is None:
                        archive_time = created_at
                        note = note or payload.get("archive_note") or payload.get("note")
                    candidate = payload.get("state")
                    if resolved_state is None and candidate in valid_states:
                        resolved_state = candidate
                if resolved_state is None:
                    try:
                        result_value = json.loads(result_json) if result_json is not None else None
                    except json.JSONDecodeError:
                        result_value = result_json
                    resolved_state = "done" if result_value not in (None, "", {}, []) else "blocked"
                archive_time = archive_time or updated_at
                note = note or "legacy archived task migrated to lifecycle archive"
                await db.execute(
                    "UPDATE work_items SET state=?,archived_at=?,archive_note=? "
                    "WHERE namespace=? AND task_id=?",
                    (resolved_state, archive_time, note, namespace, task_id),
                )
                await db.execute(
                    "INSERT INTO work_events("
                    "namespace,task_id,event_type,agent_id,payload_json,created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        namespace,
                        task_id,
                        "archive_migrated",
                        None,
                        json.dumps(
                            {
                                "legacy_state": "archived",
                                "resolved_state": resolved_state,
                                "archive_note": note,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        archive_time,
                    ),
                )

            await db.execute(
                """
                CREATE TABLE work_items_v9(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK(
                        lane IN ('implementation','review','release','integration','general')
                    ),
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(state IN ('ready','blocked','deferred','done')),
                    description TEXT NOT NULL DEFAULT '',
                    next_action TEXT NOT NULL DEFAULT '',
                    resource_json TEXT NOT NULL DEFAULT '{}',
                    reviews_json TEXT NOT NULL DEFAULT '[]',
                    cooperative INTEGER NOT NULL DEFAULT 0 CHECK(cooperative IN (0,1)),
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    candidate_ref TEXT,
                    result_json TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    state_changed_at TEXT,
                    ready_since TEXT,
                    archived_at TEXT,
                    archive_note TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, task_id)
                )
                """
            )
            await db.execute(
                """
                INSERT INTO work_items_v9(
                    namespace,task_id,title,lane,priority,state,description,next_action,
                    resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,
                    result_json,tags_json,state_changed_at,ready_since,archived_at,archive_note,
                    revision,created_at,updated_at
                )
                SELECT
                    namespace,task_id,title,lane,priority,state,description,next_action,
                    resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,
                    result_json,tags_json,state_changed_at,ready_since,archived_at,archive_note,
                    revision,created_at,updated_at
                FROM work_items
                """
            )
            await db.execute("DROP TABLE work_items")
            await db.execute("ALTER TABLE work_items_v9 RENAME TO work_items")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.execute("PRAGMA foreign_keys=ON")

        foreign_keys = int((await (await db.execute("PRAGMA foreign_keys")).fetchone())[0])
        if foreign_keys != 1:
            raise RuntimeError("schema v9 migration could not re-enable foreign keys")
        violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise RuntimeError(
                f"schema v9 archive lifecycle migration broke foreign keys: {violations}"
            )
        return True

    async def _migrate_work_items_explicit_state(self, db):
        row = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
            )
        ).fetchone()
        table_sql = (row[0] or "") if row else ""
        if "'in_progress'" in table_sql:
            return False
        await db.commit()
        await db.execute("PRAGMA foreign_keys=OFF")
        if int((await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]) != 0:
            raise RuntimeError("schema v18 migration could not disable foreign keys")
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("DROP TABLE IF EXISTS work_items_v18")
            await db.execute(
                """
                CREATE TABLE work_items_v18(
                    namespace TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK(
                        lane IN ('implementation','review','release','integration','general')
                    ),
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK(
                        state IN ('ready','in_progress','blocked','deferred','done')
                    ),
                    description TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL DEFAULT '',
                    isolation_hint TEXT NOT NULL DEFAULT 'none',
                    resource_json TEXT NOT NULL DEFAULT '{}', reviews_json TEXT NOT NULL DEFAULT '[]',
                    cooperative INTEGER NOT NULL DEFAULT 0 CHECK(cooperative IN (0,1)),
                    checkpoint_json TEXT NOT NULL DEFAULT '{}', candidate_ref TEXT, result_json TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]', state_changed_at TEXT, ready_since TEXT,
                    archived_at TEXT, archive_note TEXT, revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, task_id)
                )
                """
            )
            await db.execute(
                """
                INSERT INTO work_items_v18(
                    namespace,task_id,title,lane,priority,state,description,next_action,isolation_hint,
                    resource_json,reviews_json,cooperative,checkpoint_json,candidate_ref,result_json,
                    tags_json,state_changed_at,ready_since,archived_at,archive_note,revision,created_at,updated_at
                )
                SELECT namespace,task_id,title,lane,priority,
                       CASE
                           WHEN state='ready' AND EXISTS (
                               SELECT 1 FROM work_claims c
                               WHERE c.namespace=work_items.namespace
                                 AND c.task_id=work_items.task_id
                                 AND c.released_at IS NULL
                           ) THEN 'in_progress'
                           ELSE state
                       END,
                       description,next_action,isolation_hint,resource_json,reviews_json,cooperative,
                       checkpoint_json,candidate_ref,result_json,tags_json,
                       CASE
                           WHEN state='ready' AND EXISTS (
                               SELECT 1 FROM work_claims c
                               WHERE c.namespace=work_items.namespace
                                 AND c.task_id=work_items.task_id
                                 AND c.released_at IS NULL
                           ) THEN COALESCE((
                               SELECT MIN(c.claimed_at) FROM work_claims c
                               WHERE c.namespace=work_items.namespace
                                 AND c.task_id=work_items.task_id
                                 AND c.released_at IS NULL
                           ), state_changed_at, updated_at)
                           ELSE state_changed_at
                       END,
                       CASE
                           WHEN state='ready' AND EXISTS (
                               SELECT 1 FROM work_claims c
                               WHERE c.namespace=work_items.namespace
                                 AND c.task_id=work_items.task_id
                                 AND c.released_at IS NULL
                           ) THEN NULL
                           ELSE ready_since
                       END,
                       archived_at,archive_note,revision,created_at,updated_at
                FROM work_items
                """
            )
            await db.execute("DROP TABLE work_items")
            await db.execute("ALTER TABLE work_items_v18 RENAME TO work_items")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.execute("PRAGMA foreign_keys=ON")
        if int((await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]) != 1:
            raise RuntimeError("schema v18 migration could not re-enable foreign keys")
        violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise RuntimeError(
                f"schema v18 explicit-state migration broke foreign keys: {violations}"
            )
        return True

    async def _migrate_legacy_output(self, db):
        table = await (
            await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lines'")
        ).fetchone()
        if table is None:
            return False
        count = int((await (await db.execute("SELECT COUNT(*) FROM lines")).fetchone())[0])
        if count == 0:
            await db.execute("DROP INDEX IF EXISTS ix_lines_hash_seq")
            await db.execute("DROP INDEX IF EXISTS idx_lines_hash_seq")
            await db.execute("DROP TABLE lines")
            return True

        # A failed migration can be retried safely because the legacy table remains authoritative
        # until the import succeeds and the final DROP TABLE commits.
        await self.output.reset()
        aggregate = await (
            await db.execute(
                "SELECT hash,MAX(seq),SUM(CASE WHEN length(CAST(text AS BLOB))>? THEN ? "
                "ELSE length(CAST(text AS BLOB)) END) FROM lines GROUP BY hash ORDER BY MAX(seq) DESC",
                (self.output.line_max_bytes, self.output.line_max_bytes),
            )
        ).fetchall()
        selected = []
        budget = 0
        for cmd_hash, _last_seq, clipped_bytes in aggregate:
            effective = min(int(clipped_bytes or 0), self.output.command_max_bytes)
            if selected and budget + effective > self.output.target_bytes:
                break
            selected.append(cmd_hash)
            budget += effective
            if budget >= self.output.target_bytes:
                break

        truncated_hashes = set()
        for start in range(0, len(selected), 250):
            chunk = selected[start : start + 250]
            marks = ",".join("?" for _ in chunk)
            cursor = await db.execute(
                f"SELECT seq,hash,appeared_at,text FROM lines WHERE hash IN ({marks}) ORDER BY seq",
                chunk,
            )
            pending = {}
            while True:
                rows = await cursor.fetchmany(256)
                if not rows:
                    break
                for seq, cmd_hash, appeared_at, text in rows:
                    values = pending.setdefault(cmd_hash, [])
                    values.append((int(seq), appeared_at or "00:00:00", text or ""))
                    if len(values) >= 64:
                        result = await self.output.append_records(cmd_hash, values)
                        if result["truncated"]:
                            truncated_hashes.add(cmd_hash)
                        pending[cmd_hash] = []
            for cmd_hash, records in pending.items():
                if records:
                    result = await self.output.append_records(cmd_hash, records)
                    if result["truncated"]:
                        truncated_hashes.add(cmd_hash)

        migrated_at = utc_text()
        selected_set = set(selected)
        pruned_hashes = [row[0] for row in aggregate if row[0] not in selected_set]
        if truncated_hashes:
            await db.executemany(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,1,NULL) "
                "ON CONFLICT(command_hash) DO UPDATE SET truncated=1",
                [(value,) for value in sorted(truncated_hashes)],
            )
        if pruned_hashes:
            await db.executemany(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,0,?) "
                "ON CONFLICT(command_hash) DO UPDATE SET pruned_at=excluded.pruned_at",
                [(value, migrated_at) for value in pruned_hashes],
            )
        await db.execute("DROP INDEX IF EXISTS ix_lines_hash_seq")
        await db.execute("DROP INDEX IF EXISTS idx_lines_hash_seq")
        await db.execute("DROP TABLE lines")
        return True

    async def create(
        self,
        cmd,
        *,
        status="queued",
        cmd_hash=None,
        agent_id=None,
        command_type="run",
        command_preview="",
        queue_id=None,
        logical_agent_id=None,
        work_session_id=None,
        session_epoch=None,
        persistent_permit=None,
    ):
        attempts = 1 if cmd_hash is not None else 32
        for _ in range(attempts):
            h = cmd_hash or secrets.token_hex(4)
            now = utc_text()
            effective_queue = (queue_id or 1) if status == "queued" else queue_id
            try:
                async with self._connect("create_command", command_hash=h) as db:
                    if status == "queued":
                        await db.execute("BEGIN IMMEDIATE")
                        row = await (
                            await db.execute(
                                "SELECT COALESCE(MAX(queue_sequence),0)+1 FROM commands WHERE queue_id=?",
                                (effective_queue,),
                            )
                        ).fetchone()
                        queue_sequence = int(row[0])
                    else:
                        queue_sequence = None
                    started_at = now if status == "running" else None
                    finished_at = now if status in {"completed", "failed", "cancelled"} else None
                    enqueued_at = now if status == "queued" else None
                    claimed_at = now if status == "running" else None
                    await db.execute(
                        "INSERT INTO commands("
                        "hash,cmd,status,pid,exit_code,error,started_at,finished_at,"
                        "queue_id,queue_sequence,enqueued_at,claimed_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            h,
                            cmd,
                            status,
                            None,
                            None,
                            None,
                            started_at,
                            finished_at,
                            effective_queue,
                            queue_sequence,
                            enqueued_at,
                            claimed_at,
                        ),
                    )
                    if agent_id:
                        await db.execute(
                            "INSERT INTO command_agent_attribution("
                            "command_hash,agent_id,created_at,command_type,command_preview,"
                            "logical_agent_id,work_session_id,session_epoch) VALUES(?,?,?,?,?,?,?,?)",
                            (
                                h,
                                agent_id,
                                now,
                                command_type,
                                command_preview,
                                logical_agent_id,
                                work_session_id,
                                session_epoch,
                            ),
                        )
                    if persistent_permit is not None:
                        if not logical_agent_id or not work_session_id or session_epoch is None:
                            raise ValueError("persistent permit requires exact command attribution")
                        await db.execute(
                            "INSERT INTO persistent_command_permits("
                            "command_hash,logical_agent_id,work_session_id,session_epoch,authority_node_id,"
                            "authority_epoch,node_attachment_id,node_instance_id,scope,hard_expires_at,"
                            "permit_expires_at,signature,created_at,revoked_at,gate_revision,operation,"
                            "ttl_ms,slot_revision,principal_id) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
                            (
                                h,
                                logical_agent_id,
                                work_session_id,
                                session_epoch,
                                persistent_permit["authority_node_id"],
                                int(persistent_permit["authority_epoch"]),
                                persistent_permit["node_attachment_id"],
                                persistent_permit["node_instance_id"],
                                persistent_permit["scope"],
                                persistent_permit["hard_expires_at"],
                                persistent_permit["permit_expires_at"],
                                persistent_permit["signature"],
                                persistent_permit["issued_at"],
                                int(persistent_permit.get("gate_revision", 1)),
                                str(
                                    persistent_permit.get("operation") or persistent_permit["scope"]
                                ),
                                int(persistent_permit.get("ttl_ms", 10000)),
                                (
                                    int(persistent_permit["slot_revision"])
                                    if persistent_permit.get("slot_revision") is not None
                                    else None
                                ),
                                persistent_permit.get("principal_id"),
                            ),
                        )
                    await db.commit()
                return Command(
                    h,
                    cmd,
                    status,
                    started_at=started_at,
                    finished_at=finished_at,
                    queue_id=effective_queue,
                    queue_sequence=queue_sequence,
                    enqueued_at=enqueued_at,
                    claimed_at=claimed_at,
                )
            except IntegrityError:
                if cmd_hash is not None:
                    raise
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def update(self, command):
        """Compatibility update. Execution paths use compare-and-set helpers below."""
        now = utc_text()
        if command.status == "running" and command.started_at is None:
            command.started_at = now
        if command.status in {"completed", "failed", "cancelled"} and command.finished_at is None:
            command.finished_at = now
        async with self._connect("update_command", command_hash=command.cmd_hash) as db:
            await db.execute(
                "UPDATE commands SET status=?,pid=?,exit_code=?,error=?,started_at=?,finished_at=?,"
                "queue_id=?,queue_sequence=?,enqueued_at=?,claimed_at=? WHERE hash=?",
                (
                    command.status,
                    command.pid,
                    command.exit_code,
                    command.error,
                    command.started_at,
                    command.finished_at,
                    command.queue_id,
                    command.queue_sequence,
                    command.enqueued_at,
                    command.claimed_at,
                    command.cmd_hash,
                ),
            )
            await db.commit()

    async def claim_next(self, queue_id):
        now = utc_text()
        async with self._connect("claim_command") as db:
            await db.execute("BEGIN IMMEDIATE")
            # Queue authority is durable, not merely one local worker task.
            # In particular, do not overlap an unowned/uncertain surviving
            # process after restart or a lost execution-port response.
            running = await (
                await db.execute(
                    "SELECT 1 FROM commands WHERE queue_id=? AND status='running' LIMIT 1",
                    (queue_id,),
                )
            ).fetchone()
            if running is not None:
                await db.commit()
                return None
            while True:
                row = await (
                    await db.execute(
                        "SELECT hash FROM commands WHERE queue_id=? AND status='queued' "
                        "ORDER BY queue_sequence,rowid LIMIT 1",
                        (queue_id,),
                    )
                ).fetchone()
                if row is None:
                    await db.commit()
                    return None
                cmd_hash = row[0]
                attribution = await (
                    await db.execute(
                        "SELECT logical_agent_id,work_session_id,session_epoch "
                        "FROM command_agent_attribution WHERE command_hash=?",
                        (cmd_hash,),
                    )
                ).fetchone()
                if attribution and attribution[0] is not None:
                    allowed = await (
                        await db.execute(
                            "SELECT 1 FROM logical_agent_work_sessions s "
                            "JOIN logical_agents a ON a.logical_agent_id=s.logical_agent_id "
                            "WHERE s.logical_agent_id=? AND s.work_session_id=? AND s.session_epoch=? "
                            "AND s.state='active' AND a.state='active' "
                            "AND s.authority_epoch=a.authority_epoch AND s.hard_expires_at>?",
                            (attribution[0], attribution[1], attribution[2], now),
                        )
                    ).fetchone()
                    if allowed is None:
                        allowed = await (
                            await db.execute(
                                "SELECT 1 FROM persistent_command_permits p "
                                "WHERE p.command_hash=? AND p.logical_agent_id=? "
                                "AND p.work_session_id=? AND p.session_epoch=? "
                                "AND p.scope='run' AND p.revoked_at IS NULL "
                                "AND p.permit_expires_at>? AND p.hard_expires_at>?",
                                (
                                    cmd_hash,
                                    attribution[0],
                                    attribution[1],
                                    attribution[2],
                                    now,
                                    now,
                                ),
                            )
                        ).fetchone()
                    if allowed is None:
                        await db.execute(
                            "UPDATE commands SET status='cancelled',error='persistent.session_fenced',"
                            "finished_at=? WHERE hash=? AND status='queued'",
                            (now, cmd_hash),
                        )
                        continue
                cur = await db.execute(
                    "UPDATE commands SET status='running', started_at=COALESCE(started_at,?), claimed_at=? "
                    "WHERE hash=? AND status='queued'",
                    (now, now, cmd_hash),
                )
                if cur.rowcount != 1:
                    await db.rollback()
                    return None
                row = await (
                    await db.execute(
                        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?", (cmd_hash,)
                    )
                ).fetchone()
                await db.commit()
                return Command(*row)

    async def set_pid(self, cmd_hash, pid):
        async with self._connect("set_pid", command_hash=cmd_hash) as db:
            cur = await db.execute(
                "UPDATE commands SET pid=? WHERE hash=? AND status='running'", (pid, cmd_hash)
            )
            await db.commit()
        return cur.rowcount == 1

    async def finish_running(self, cmd_hash, status, exit_code=None, error=None):
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("finish_running requires a terminal status")
        finished_at = utc_text()
        async with self._connect(
            "finish_command",
            command_hash=cmd_hash,
            execution_outcome=status,
            durable_finalization_outcome="failed",
        ) as db:
            cur = await db.execute(
                "UPDATE commands SET status=?,exit_code=?,error=?,finished_at=? "
                "WHERE hash=? AND status='running'",
                (status, exit_code, error, finished_at, cmd_hash),
            )
            await db.commit()
        return cur.rowcount == 1

    async def cancel_stale_running(self, cmd_hash, expected_pid, exit_code=None):
        """Atomically repair a running row that has no runtime-owned process."""
        finished_at = utc_text()
        async with self._connect("cancel_stale_running", command_hash=cmd_hash) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return None, False

            command = Command(*row)
            if command.status != "running" or command.pid != expected_pid:
                await db.commit()
                return command, False

            cur = await db.execute(
                "UPDATE commands SET status='cancelled',"
                "exit_code=COALESCE(?,exit_code),error=NULL,finished_at=? "
                "WHERE hash=? AND status='running' "
                "AND ((pid IS NULL AND ? IS NULL) OR pid=?)",
                (exit_code, finished_at, cmd_hash, expected_pid, expected_pid),
            )
            if cur.rowcount != 1:
                await db.rollback()
                row = await (
                    await db.execute(
                        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                        (cmd_hash,),
                    )
                ).fetchone()
                return (Command(*row) if row is not None else None), False

            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
            await db.commit()
        return Command(*row), True

    async def cancel_if_queued(self, cmd_hash):
        """Linearize queued cancellation against worker claim in one write transaction.

        Returns (command, cancelled_before_start). A successful pre-start cancellation
        leaves claimed_at/started_at unset. If claim already won, the observed running
        command is returned unchanged so the caller can perform running cancellation.
        """
        finished_at = utc_text()
        async with self._connect("cancel_if_queued", command_hash=cmd_hash) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return None, False
            command = Command(*row)
            if command.status != "queued":
                await db.commit()
                return command, False

            cur = await db.execute(
                "UPDATE commands SET status='cancelled',error=NULL,finished_at=? "
                "WHERE hash=? AND status='queued'",
                (finished_at, cmd_hash),
            )
            if cur.rowcount != 1:
                await db.rollback()
                row = await (
                    await db.execute(
                        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                        (cmd_hash,),
                    )
                ).fetchone()
                return (Command(*row) if row is not None else None), False

            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
            await db.commit()
        return Command(*row), True

    async def cancel_queued(self, cmd_hash):
        _command, cancelled_before_start = await self.cancel_if_queued(cmd_hash)
        return cancelled_before_start

    async def queue_position(self, cmd_hash):
        async with self._connect("queue_position") as db:
            row = await (
                await db.execute(
                    "SELECT queue_id,queue_sequence,status FROM commands WHERE hash=?", (cmd_hash,)
                )
            ).fetchone()
            if row is None:
                return None
            queue_id, sequence, status = row
            if status != "queued" or queue_id is None or sequence is None:
                return None
            count = await (
                await db.execute(
                    "SELECT COUNT(*) FROM commands WHERE queue_id=? AND status='queued' AND queue_sequence<=?",
                    (queue_id, sequence),
                )
            ).fetchone()
        return int(count[0])

    async def queue_loads(self, queue_count):
        loads = {queue_id: 0 for queue_id in range(1, queue_count + 1)}
        async with self._connect("queue_loads") as db:
            rows = await (
                await db.execute(
                    "SELECT queue_id,COUNT(*) FROM commands WHERE status IN ('queued','running') "
                    "AND queue_id IS NOT NULL GROUP BY queue_id"
                )
            ).fetchall()
        for queue_id, count in rows:
            if queue_id in loads:
                loads[queue_id] = int(count)
        return loads

    async def queue_snapshot(self, queue_count):
        result = {
            queue_id: {"queue_id": queue_id, "running": None, "queued": 0}
            for queue_id in range(1, queue_count + 1)
        }
        async with self._connect("queue_snapshot") as db:
            queued = await (
                await db.execute(
                    "SELECT queue_id,COUNT(*) FROM commands WHERE status='queued' AND queue_id IS NOT NULL GROUP BY queue_id"
                )
            ).fetchall()
            running = await (
                await db.execute(
                    "SELECT queue_id,hash FROM commands WHERE status='running' AND queue_id IS NOT NULL ORDER BY claimed_at"
                )
            ).fetchall()
        for queue_id, count in queued:
            if queue_id in result:
                result[queue_id]["queued"] = int(count)
        for queue_id, cmd_hash in running:
            if queue_id in result and result[queue_id]["running"] is None:
                result[queue_id]["running"] = cmd_hash
        return [result[i] for i in sorted(result)]

    async def list_running(self, queue_id=None):
        async with self._connect("running_commands") as db:
            if queue_id is None:
                rows = await (
                    await db.execute(
                        f"SELECT {_COMMAND_COLUMNS} FROM commands "
                        "WHERE status='running' ORDER BY claimed_at,rowid"
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        f"SELECT {_COMMAND_COLUMNS} FROM commands "
                        "WHERE status='running' AND queue_id=? ORDER BY claimed_at,rowid",
                        (queue_id,),
                    )
                ).fetchall()
        return [Command(*row) for row in rows]

    async def command_queue_ids(self, hashes):
        values = list(dict.fromkeys(hashes))
        if not values:
            return {}
        result = {}
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            async with self._connect("command_queue_ids") as db:
                rows = await (
                    await db.execute(
                        f"SELECT hash,queue_id FROM commands WHERE hash IN ({marks})", chunk
                    )
                ).fetchall()
            result.update(rows)
        return result

    async def delete_command(self, cmd_hash):
        await self.output.delete_command(cmd_hash)
        async with self._connect("delete_command", command_hash=cmd_hash) as db:
            await db.execute(
                "DELETE FROM command_agent_attribution WHERE command_hash=?", (cmd_hash,)
            )
            await db.execute("DELETE FROM command_output_state WHERE command_hash=?", (cmd_hash,))
            await db.execute("DELETE FROM commands WHERE hash=?", (cmd_hash,))
            await db.commit()

    async def get(self, cmd_hash):
        async with self._connect("get_command", command_hash=cmd_hash) as db:
            row = await (
                await db.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE hash=?", (cmd_hash,)
                )
            ).fetchone()
        return Command(*row) if row else None

    async def persistent_attribution(self, cmd_hash):
        async with self._connect("persistent_attribution", command_hash=cmd_hash) as db:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,work_session_id,session_epoch "
                    "FROM command_agent_attribution WHERE command_hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return {
            "logical_agent_id": row[0],
            "work_session_id": row[1],
            "session_epoch": int(row[2]) if row[2] is not None else None,
        }

    async def persistent_commands(
        self,
        logical_agent_id,
        *,
        work_session_id=None,
        session_epoch=None,
        statuses=("queued", "running"),
    ):
        where = ["a.logical_agent_id=?"]
        params = [logical_agent_id]
        if work_session_id is not None:
            where.append("a.work_session_id=?")
            params.append(work_session_id)
        if session_epoch is not None:
            where.append("a.session_epoch=?")
            params.append(session_epoch)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            where.append(f"c.status IN ({marks})")
            params.extend(statuses)
        columns = (
            "c.hash,c.cmd,c.status,c.pid,c.exit_code,c.error,c.started_at,c.finished_at,"
            "c.queue_id,c.queue_sequence,c.enqueued_at,c.claimed_at"
        )
        async with self._connect("persistent_commands") as db:
            rows = await (
                await db.execute(
                    f"SELECT {columns} FROM commands c JOIN command_agent_attribution a "
                    f"ON a.command_hash=c.hash WHERE {' AND '.join(where)} "
                    "ORDER BY c.enqueued_at,c.hash",
                    params,
                )
            ).fetchall()
        return [Command(*row) for row in rows]

    async def _scrub_pruned_command_bodies(self, db, command_hashes=None):
        terminal = ("completed", "failed", "cancelled")
        if command_hashes is None:
            await db.execute(
                "UPDATE commands SET cmd=? "
                "WHERE status IN (?,?,?) AND cmd<>? AND hash IN ("
                "SELECT command_hash FROM command_output_state WHERE pruned_at IS NOT NULL)",
                (_SCRUBBED_COMMAND_BODY, *terminal, _SCRUBBED_COMMAND_BODY),
            )
            return
        hashes = tuple(dict.fromkeys(str(value) for value in command_hashes if value))
        if not hashes:
            return
        marks = ",".join("?" for _ in hashes)
        await db.execute(
            f"UPDATE commands SET cmd=? WHERE hash IN ({marks}) "
            "AND status IN (?,?,?) AND cmd<>?",
            (_SCRUBBED_COMMAND_BODY, *hashes, *terminal, _SCRUBBED_COMMAND_BODY),
        )

    async def mark_output_truncated(self, cmd_hash):
        async with self._connect("mark_output_truncated", command_hash=cmd_hash) as db:
            await db.execute(
                "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,1,NULL) "
                "ON CONFLICT(command_hash) DO UPDATE SET truncated=1",
                (cmd_hash,),
            )
            await db.commit()

    async def append_lines(self, cmd_hash, texts):
        result = await self.output.append_lines(cmd_hash, list(texts))
        if result["truncated"]:
            await self.mark_output_truncated(cmd_hash)
        return result

    async def append_replayed_lines(self, cmd_hash, texts, replay_offset):
        result = await self.output.append_lines(cmd_hash, list(texts), replay_offset=replay_offset)
        if result["truncated"]:
            await self.mark_output_truncated(cmd_hash)
        return result

    async def append_line(self, cmd_hash, text):
        return await self.append_lines(cmd_hash, [text])

    async def output_status(self, cmd_hash):
        meta = await self.output.command_meta(cmd_hash)
        async with self._connect("output_status", command_hash=cmd_hash) as db:
            state = await (
                await db.execute(
                    "SELECT truncated,pruned_at FROM command_output_state WHERE command_hash=?",
                    (cmd_hash,),
                )
            ).fetchone()
        durable_truncated = bool(state[0]) if state else False
        pruned_at = state[1] if state else None
        return {
            "output_truncated": durable_truncated or bool(meta and meta["truncated"]),
            "output_retained": pruned_at is None,
            "output_pruned_at": pruned_at,
            "output_bytes": int(meta["stored_bytes"]) if meta else 0,
        }

    async def prune_output_cache(self):
        async with self._connect("output_active_hashes") as db:
            rows = await (
                await db.execute("SELECT hash FROM commands WHERE status IN ('queued','running')")
            ).fetchall()
        pruned = await self.output.prune({row[0] for row in rows})
        if pruned:
            stamp = utc_text()
            async with self._connect("mark_output_pruned") as db:
                await db.executemany(
                    "INSERT INTO command_output_state(command_hash,truncated,pruned_at) VALUES(?,0,?) "
                    "ON CONFLICT(command_hash) DO UPDATE SET pruned_at=excluded.pruned_at",
                    [(cmd_hash, stamp) for cmd_hash in pruned],
                )
                await self._scrub_pruned_command_bodies(db, pruned)
                await db.commit()
        return pruned

    async def output_cache_stats(self):
        return await self.output.stats()

    async def count_lines(self, cmd_hash):
        return await self.output.count_lines(cmd_hash)

    async def read_command_lines(self, cmd_hash, limit, offset):
        return await self.output.read_command_lines(cmd_hash, limit, offset)

    async def read_global_after_cursor(self, limit, cursor):
        return await self.output.read_global_after_cursor(limit, cursor)

    async def read_global_tail(self, limit, distance_from_end=None):
        return await self.output.read_global_tail(limit, distance_from_end)

    async def read_lines(self, cmd_hash, limit, offset):
        if cmd_hash:
            return await self.read_command_lines(cmd_hash, limit, offset)
        return await self.read_global_after_cursor(limit, offset)

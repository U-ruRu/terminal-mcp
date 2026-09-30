from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from terminal_mcp.fleet.protocol import MAX_RECENT_TERMINAL_COMMANDS
from terminal_mcp.host_resources import collect_host_resources

CURRENT_SCOPE_VERSION = 1
MAX_PAGE_LIMIT = 100
MAX_PAGE_BYTES = 512 * 1024
RESOURCE_SAMPLE_TTL_SECONDS = 2.0


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _enc(values: tuple[Any, ...]) -> str:
    raw = json.dumps(list(values), ensure_ascii=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _dec(value: str | None, width: int) -> tuple[Any, ...] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        result = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid keyset cursor") from exc
    if not isinstance(result, list) or len(result) != width:
        raise ValueError("invalid keyset cursor shape")
    return tuple(result)


def _snapshot_id(generation: str, barrier: int, scope: str) -> str:
    return _enc((generation, int(barrier), scope, CURRENT_SCOPE_VERSION))


def _decode_snapshot(value: str) -> tuple[str, int, str, int]:
    values = _dec(value, 4)
    if values is None:
        raise ValueError("invalid recovery snapshot_id")
    try:
        return str(values[0]), int(values[1]), str(values[2]), int(values[3])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery snapshot_id") from exc


@dataclass(frozen=True)
class CurrentScope:
    entity_type: str
    sql: str
    keys: tuple[str, ...] = ("k1",)


CURRENT_SCOPES = {
    "logical_agents": CurrentScope(
        "logical_agent",
        """
        SELECT logical_agent_id AS k1,display_name,state,authority_node_id,authority_epoch,
               slot_revision,selector_generation,auth_generation,created_at,updated_at,
               deleted_at,tombstone_reason
        FROM logical_agents WHERE deleted_at IS NULL""",
    ),
    "work_sessions": CurrentScope(
        "work_session",
        """
        SELECT work_session_id AS k1,logical_agent_id,session_epoch,authority_node_id,
               authority_epoch,started_at,hard_expires_at,auth_generation,state,
               origin_instance_id,ended_at,end_reason
        FROM logical_agent_work_sessions WHERE state IN ('active','stopping')""",
    ),
    "node_attachments": CurrentScope(
        "node_attachment",
        """
        SELECT node_attachment_id AS k1,logical_agent_id,work_session_id,session_epoch,
               node_instance_id,authority_epoch,attached_at,hard_expires_at,revoked_at
        FROM logical_agent_node_attachments WHERE revoked_at IS NULL""",
    ),
    "attachment_presence": CurrentScope(
        "attachment_presence",
        """
        SELECT p.node_attachment_id AS k1,p.logical_agent_id,p.work_session_id,p.session_epoch,
               p.node_instance_id,p.task_summary,p.intent,p.work_scope_json,p.details_json,
               p.current_step,p.intent_updated_at,p.last_activity_at
        FROM persistent_attachment_presence p
        JOIN logical_agent_node_attachments a ON a.node_attachment_id=p.node_attachment_id
        WHERE a.revoked_at IS NULL""",
    ),
    "message_obligations": CurrentScope(
        "message_obligation",
        """
        SELECT message_ref AS k1,logical_agent_id,sender_agent_id,text,require_reply,alert,
               gate_revision,created_at,resolved_at,resolution
        FROM persistent_message_obligations WHERE resolved_at IS NULL""",
    ),
    "work_claims": CurrentScope(
        "work_claim",
        """
        SELECT id AS k1,namespace,task_id,agent_id,claimed_at,released_at,claim_intent,
               owner_kind,owner_id FROM work_claims WHERE released_at IS NULL""",
    ),
    "tasks": CurrentScope(
        "task",
        """
        SELECT namespace AS k1,task_id AS k2,title,lane,priority,state,cooperative,revision,
               state_changed_at,updated_at,archived_at,ready_since,tags_json
        FROM work_items WHERE archived_at IS NULL AND state != 'done'""",
        ("k1", "k2"),
    ),
    "commands": CurrentScope(
        "command",
        f"""
        SELECT c.hash AS k1,c.status,c.exit_code,c.queue_id,c.started_at,c.finished_at,
               c.enqueued_at,c.claimed_at,c.error,a.logical_agent_id,a.work_session_id,
               a.session_epoch,s.truncated,s.pruned_at
        FROM commands c
        LEFT JOIN command_agent_attribution a ON a.command_hash=c.hash
        LEFT JOIN command_output_state s ON s.command_hash=c.hash
        WHERE c.status IN ('queued','running')
           OR c.hash IN (
               SELECT recent.hash
               FROM commands recent
               WHERE recent.status IN ('completed','failed','cancelled')
               ORDER BY COALESCE(
                   recent.finished_at,recent.started_at,recent.claimed_at,recent.enqueued_at,''
               ) DESC,recent.hash DESC
               LIMIT {MAX_RECENT_TERMINAL_COMMANDS}
           )""",
    ),
    "fleet_gates": CurrentScope(
        "fleet_gate",
        """
        SELECT logical_agent_id AS k1,gate_revision,blocked,reason,updated_at
        FROM persistent_fleet_gates""",
    ),
    "contexts": CurrentScope(
        "context",
        """
        SELECT id AS k1,summary,is_primary FROM instance_context""",
    ),
}


@dataclass(frozen=True)
class QueryResource:
    sql: str
    key_columns: tuple[str, ...]
    key_aliases: tuple[str, ...]
    filters: dict[str, str]
    search: tuple[str, ...] = ()
    time_column: str | None = None
    seq_column: str | None = None
    output_db: bool = False


QUERY_RESOURCES = {
    "tasks": QueryResource(
        "SELECT namespace AS k1,task_id AS k2,title,lane,priority,state,description,"
        "next_action,isolation_hint,resource_json,reviews_json,cooperative,checkpoint_json,"
        "candidate_ref,result_json,tags_json,state_changed_at,ready_since,archived_at,"
        "archive_note,revision,created_at,updated_at FROM work_items",
        ("namespace", "task_id"),
        ("k1", "k2"),
        {"namespace": "namespace", "state": "state", "lane": "lane", "priority": "priority"},
        ("namespace", "task_id", "title", "description", "next_action"),
        "updated_at",
    ),
    "task_history": QueryResource(
        "SELECT id AS k1,namespace,task_id,event_type,agent_id,payload_json,created_at,"
        "logical_agent_id,work_session_id,session_epoch FROM work_events",
        ("id",),
        ("k1",),
        {"namespace": "namespace", "task_id": "task_id", "event_type": "event_type"},
        ("namespace", "task_id", "event_type", "agent_id"),
        "created_at",
        "id",
    ),
    "activity": QueryResource(
        "SELECT id AS k1,agent_id,timestamp,tool,command_hash FROM agent_activity_events",
        ("id",),
        ("k1",),
        {"agent_id": "agent_id", "tool": "tool"},
        ("agent_id", "tool", "command_hash"),
        "timestamp",
        "id",
    ),
    "audit": QueryResource(
        "SELECT id AS k1,logical_agent_id,event_type,principal_id,work_session_id,"
        "session_epoch,payload_json,created_at FROM persistent_agent_audit",
        ("id",),
        ("k1",),
        {
            "logical_agent_id": "logical_agent_id",
            "event_type": "event_type",
            "work_session_id": "work_session_id",
        },
        ("logical_agent_id", "event_type", "principal_id", "work_session_id"),
        "created_at",
        "id",
    ),
    "messages": QueryResource(
        "SELECT message_hash AS k1,sender_agent_id,target_name,text,created_at,require_reply,"
        "alert,task_namespace,task_id FROM coordination_messages",
        ("message_hash",),
        ("k1",),
        {
            "sender_agent_id": "sender_agent_id",
            "target_name": "target_name",
            "namespace": "task_namespace",
            "task_id": "task_id",
        },
        ("message_hash", "sender_agent_id", "target_name", "text"),
        "created_at",
    ),
    "message_receipts": QueryResource(
        "SELECT message_hash AS k1,recipient_agent_id AS k2,delivered_at,first_seen_at,"
        "last_seen_at,seen_count,read_at,replied_at,reply_message_hash "
        "FROM coordination_message_recipients",
        ("message_hash", "recipient_agent_id"),
        ("k1", "k2"),
        {"message_hash": "message_hash", "recipient_agent_id": "recipient_agent_id"},
        ("message_hash", "recipient_agent_id", "reply_message_hash"),
        "delivered_at",
    ),
    "slots": QueryResource(
        "SELECT logical_agent_id AS k1,display_name,state,authority_node_id,authority_epoch,"
        "slot_revision,selector_generation,auth_generation,created_at,updated_at,deleted_at,"
        "tombstone_reason FROM logical_agents",
        ("logical_agent_id",),
        ("k1",),
        {"state": "state", "authority_node_id": "authority_node_id"},
        ("logical_agent_id", "display_name", "authority_node_id"),
        "updated_at",
    ),
    "sessions": QueryResource(
        "SELECT work_session_id AS k1,logical_agent_id,session_epoch,authority_node_id,"
        "authority_epoch,started_at,hard_expires_at,auth_principal_id,auth_generation,state,"
        "origin_instance_id,ended_at,end_reason FROM logical_agent_work_sessions",
        ("work_session_id",),
        ("k1",),
        {
            "logical_agent_id": "logical_agent_id",
            "state": "state",
            "authority_node_id": "authority_node_id",
        },
        ("work_session_id", "logical_agent_id", "authority_node_id"),
        "started_at",
    ),
    "legacy_sessions": QueryResource(
        "SELECT agent_id AS k1,registered_at,last_activity_at,task_summary,intent,work_scope,"
        "state,details,current_step,ended_at,end_reason,preferred_queue_id,source_instance_id,"
        "global_expires_at,intent_scopes FROM agent_sessions",
        ("agent_id",),
        ("k1",),
        {"state": "state", "source_instance_id": "source_instance_id"},
        ("agent_id", "task_summary", "intent", "source_instance_id"),
        "last_activity_at",
    ),
    "contexts": QueryResource(
        "SELECT id AS k1,summary,content,is_primary FROM instance_context",
        ("id",),
        ("k1",),
        {"is_primary": "is_primary"},
        ("summary", "content"),
    ),
    "commands": QueryResource(
        "SELECT c.hash AS k1,c.cmd,c.status,c.pid,c.exit_code,c.error,c.started_at,c.finished_at,"
        "c.queue_id,c.queue_sequence,c.enqueued_at,c.claimed_at,a.agent_id,a.command_type,"
        "a.command_preview,a.logical_agent_id,a.work_session_id,a.session_epoch,s.truncated,"
        "s.pruned_at FROM commands c LEFT JOIN command_agent_attribution a "
        "ON a.command_hash=c.hash LEFT JOIN command_output_state s ON s.command_hash=c.hash",
        ("c.hash",),
        ("k1",),
        {
            "status": "c.status",
            "agent_id": "a.agent_id",
            "logical_agent_id": "a.logical_agent_id",
            "work_session_id": "a.work_session_id",
        },
        ("c.hash", "c.cmd", "a.agent_id", "a.command_preview", "a.logical_agent_id"),
        "COALESCE(c.finished_at,c.started_at,c.claimed_at,c.enqueued_at)",
    ),
    "operator_attention": QueryResource(
        "SELECT message_ref AS k1,logical_agent_id,sender_agent_id,text,require_reply,alert,"
        "gate_revision,created_at,resolved_at,resolution FROM persistent_message_obligations",
        ("message_ref",),
        ("k1",),
        {
            "logical_agent_id": "logical_agent_id",
            "require_reply": "require_reply",
            "alert": "alert",
        },
        ("message_ref", "logical_agent_id", "sender_agent_id", "text"),
        "created_at",
    ),
    "events": QueryResource(
        "SELECT seq AS k1,event_type,entity_type,entity_id,actor_id,payload_json,created_at "
        "FROM instance_events",
        ("seq",),
        ("k1",),
        {"event_type": "event_type", "entity_type": "entity_type", "entity_id": "entity_id"},
        ("event_type", "entity_type", "entity_id", "actor_id"),
        "created_at",
        "seq",
    ),
    "output_metadata": QueryResource(
        "SELECT hash AS k1,stored_bytes,stored_lines,truncated,last_seq FROM output_meta",
        ("hash",),
        ("k1",),
        {},
        ("hash",),
        output_db=True,
    ),
}


class FleetSourceQueryPlane:
    def __init__(self, runtime_db_path, *, output_db_path=None, metrics=None, events=None):
        self.runtime_db_path = Path(runtime_db_path)
        self.output_db_path = Path(output_db_path) if output_db_path else None
        self.metrics = metrics
        self.events = events
        self._resource_sample = None
        self._resource_sample_at = 0.0

    @staticmethod
    def scope_names():
        return sorted(CURRENT_SCOPES)

    def _observe(self, operation, started, *, rows=0, size=0, reason="ok"):
        elapsed = time.monotonic() - started
        labels = (("operation", operation), ("reason", reason))
        if self.metrics:
            self.metrics.observe(
                "terminal_mcp_fleet_source_stage_duration_seconds", elapsed, labels
            )
            self.metrics.observe("terminal_mcp_fleet_source_rows", rows, labels)
            self.metrics.observe("terminal_mcp_fleet_source_bytes", size, labels)
        if self.events:
            self.events.emit(
                "fleet_source_read",
                operation=operation,
                reason=reason,
                rows=int(rows),
                bytes=int(size),
                duration_ms=round(elapsed * 1000),
            )

    def _sqlite_error(self, operation):
        if self.metrics:
            self.metrics.inc(
                "terminal_mcp_fleet_source_sqlite_errors_total", (("operation", operation),)
            )

    async def sampled_resources(self):
        now = time.monotonic()
        if (
            self._resource_sample is None
            or now - self._resource_sample_at >= RESOURCE_SAMPLE_TTL_SECONDS
        ):
            self._resource_sample = collect_host_resources(self.runtime_db_path.parent)
            self._resource_sample_at = now
        return {
            **self._resource_sample,
            "sample_age_ms": round((time.monotonic() - self._resource_sample_at) * 1000),
        }

    async def recovery_page(
        self, *, scope, generation, barrier, high_water, snapshot_id=None, cursor=None, limit=100
    ):
        started = time.monotonic()
        spec = CURRENT_SCOPES.get(scope)
        if spec is None:
            raise ValueError(f"unknown current recovery scope: {scope}")
        limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        if snapshot_id:
            snap_generation, snap_barrier, snap_scope, version = _decode_snapshot(snapshot_id)
            if snap_generation != generation:
                raise ValueError("recovery source_stream_generation changed")
            if snap_scope != scope or version != CURRENT_SCOPE_VERSION:
                raise ValueError("recovery snapshot scope changed")
            barrier = snap_barrier
        else:
            snapshot_id = _snapshot_id(generation, barrier, scope)
        decoded = _dec(cursor, len(spec.keys))
        sql = f"SELECT * FROM ({spec.sql}) scoped"
        params = []
        if decoded:
            marks = ",".join("?" for _ in spec.keys)
            sql += f" WHERE ({','.join(spec.keys)}) > ({marks})"
            params.extend(decoded)
        sql += f" ORDER BY {','.join(spec.keys)} LIMIT ?"
        params.append(limit + 1)
        try:
            async with aiosqlite.connect(self.runtime_db_path, timeout=1.0) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(sql, params)).fetchall()
        except aiosqlite.Error:
            self._sqlite_error(f"recovery:{scope}")
            raise
        has_more = len(rows) > limit
        rows = rows[:limit]
        entities, oversize, page_bytes = [], [], 0
        consumed = decoded
        for row in rows:
            key = tuple(row[name] for name in spec.keys)
            entity = self._current_entity(scope, spec, row, barrier)
            size = len(json.dumps(entity, ensure_ascii=False, separators=(",", ":")).encode())
            consumed = key
            if page_bytes + size > MAX_PAGE_BYTES:
                oversize.append(_enc(key))
                continue
            entities.append(entity)
            page_bytes += size
        self._observe(
            f"recovery:{scope}", started, rows=len(entities), size=page_bytes, reason="page"
        )
        return {
            "snapshot_id": snapshot_id,
            "source_stream_generation": generation,
            "scope": scope,
            "scope_version": CURRENT_SCOPE_VERSION,
            "barrier_source_seq": int(barrier),
            "high_water_source_seq": int(high_water),
            "replace_scope": True,
            "page_complete": not has_more,
            "next_cursor": _enc(consumed) if has_more and consumed else None,
            "entities": entities,
            "oversize_entity_keys": oversize,
            "page_rows": len(entities),
            "page_bytes": page_bytes,
            "replay_after_source_seq": int(barrier),
        }

    async def current_entity(self, *, scope, entity_id, barrier):
        started = time.monotonic()
        spec = CURRENT_SCOPES.get(scope)
        if spec is None:
            raise ValueError(f"unknown current recovery scope: {scope}")
        if len(spec.keys) == 1:
            values = (entity_id,)
        elif scope == "tasks" and "/" in entity_id:
            values = tuple(entity_id.split("/", 1))
        else:
            decoded = _dec(entity_id, len(spec.keys))
            if decoded is None:
                raise ValueError("current entity key is required")
            values = decoded
        if len(values) != len(spec.keys):
            raise ValueError("current entity key shape changed")
        marks = ",".join("?" for _ in spec.keys)
        sql = (
            f"SELECT * FROM ({spec.sql}) scoped "
            f"WHERE ({','.join(spec.keys)})=({marks}) LIMIT 1"
        )
        try:
            async with aiosqlite.connect(self.runtime_db_path, timeout=1.0) as db:
                db.row_factory = aiosqlite.Row
                row = await (await db.execute(sql, list(values))).fetchone()
                if row is None and scope == "commands":
                    row = await (
                        await db.execute(
                            """
                            SELECT c.hash AS k1,c.status,c.exit_code,c.queue_id,c.started_at,
                                   c.finished_at,c.enqueued_at,c.claimed_at,c.error,
                                   a.logical_agent_id,a.work_session_id,a.session_epoch,
                                   s.truncated,s.pruned_at
                            FROM commands c
                            LEFT JOIN command_agent_attribution a ON a.command_hash=c.hash
                            LEFT JOIN command_output_state s ON s.command_hash=c.hash
                            WHERE c.hash=?
                            LIMIT 1
                            """,
                            [entity_id],
                        )
                    ).fetchone()
        except aiosqlite.Error:
            self._sqlite_error(f"current:{scope}")
            raise
        entity = self._current_entity(scope, spec, row, barrier) if row else None
        size = (
            len(json.dumps(entity, ensure_ascii=False, separators=(",", ":")).encode())
            if entity
            else 0
        )
        self._observe(
            f"current:{scope}",
            started,
            rows=1 if entity else 0,
            size=size,
            reason="materialize",
        )
        return entity

    def _current_entity(self, scope, spec, row, barrier):
        data = dict(row)
        keys = tuple(data.pop(name) for name in spec.keys)
        revision = max(1, int(barrier))
        authority_node_id = authority_epoch = None
        if scope == "logical_agents":
            entity_id = str(keys[0])
            revision = int(data["slot_revision"])
            authority_node_id = data.get("authority_node_id")
            authority_epoch = int(data["authority_epoch"])
            data["logical_agent_id"] = entity_id
        elif scope == "tasks":
            entity_id = f"{keys[0]}/{keys[1]}"
            revision = int(data["revision"])
            data["namespace"], data["task_id"] = keys
            data["tags"] = _loads(data.pop("tags_json"), [])
            data["cooperative"] = bool(data["cooperative"])
        elif scope == "work_claims":
            entity_id = str(keys[0])
            data["claim_id"] = int(keys[0])
        elif scope == "message_obligations":
            entity_id = str(keys[0])
            revision = int(data["gate_revision"])
            data["message_ref"] = entity_id
            data["require_reply"], data["alert"] = bool(data["require_reply"]), bool(data["alert"])
        elif scope == "attachment_presence":
            entity_id = str(keys[0])
            data["node_attachment_id"] = entity_id
            data["work_scope"] = _loads(data.pop("work_scope_json"), [])
            data["details"] = _loads(data.pop("details_json"), [])
        elif scope == "node_attachments":
            entity_id = str(keys[0])
            data["node_attachment_id"] = entity_id
            authority_epoch = int(data["authority_epoch"])
        elif scope == "work_sessions":
            entity_id = str(keys[0])
            data["work_session_id"] = entity_id
            authority_node_id = data.get("authority_node_id")
            authority_epoch = int(data["authority_epoch"])
        elif scope == "commands":
            entity_id = str(keys[0])
            data["command_hash"] = entity_id
            data["output_truncated"] = bool(data.pop("truncated") or 0)
        elif scope == "fleet_gates":
            entity_id = str(keys[0])
            revision = int(data["gate_revision"])
            data["logical_agent_id"], data["blocked"] = entity_id, bool(data["blocked"])
        elif scope == "contexts":
            entity_id = str(keys[0])
            data["context_id"] = int(keys[0])
            data["primary"] = bool(data.pop("is_primary"))
        else:
            entity_id = "/".join(str(value) for value in keys)
        result = {
            "entity_type": spec.entity_type,
            "entity_id": entity_id,
            "entity_revision": revision,
            "payload_version": 2,
            "payload": data,
        }
        if authority_node_id is not None:
            result["authority_node_id"] = authority_node_id
        if authority_epoch is not None:
            result["authority_epoch"] = authority_epoch
        return result

    def _query_parts(self, spec, *, cursor, q, filters, as_of, through_seq):
        clauses, params = [], []
        for name, value in (filters or {}).items():
            if value is None:
                continue
            column = spec.filters.get(name)
            if column is None:
                raise ValueError(f"unsupported query filter: {name}")
            clauses.append(f"{column}=?")
            params.append(value)
        if q:
            q = q.strip()
            if len(q) > 200:
                raise ValueError("query text is too long")
            if spec.search:
                clauses.append("(" + " OR ".join(f"{col} LIKE ?" for col in spec.search) + ")")
                params.extend([f"%{q}%"] * len(spec.search))
        if as_of:
            if not spec.time_column:
                raise ValueError("as_of is not supported for this resource")
            clauses.append(f"{spec.time_column}<=?")
            params.append(as_of)
        if through_seq is not None:
            through_seq = int(through_seq)
            if through_seq < 0 or not spec.seq_column:
                raise ValueError("through_seq is not supported for this resource")
            clauses.append(f"{spec.seq_column}<=?")
            params.append(through_seq)
        filter_clauses, filter_params = list(clauses), list(params)
        decoded = _dec(cursor, len(spec.key_columns))
        if decoded:
            marks = ",".join("?" for _ in spec.key_columns)
            clauses.append(f"({','.join(spec.key_columns)}) > ({marks})")
            params.extend(decoded)
        return clauses, params, filter_clauses, filter_params

    async def query(
        self,
        resource,
        *,
        cursor=None,
        limit=100,
        q=None,
        filters=None,
        as_of=None,
        through_seq=None,
        include_count=False,
        include_facets=False,
    ):
        started = time.monotonic()
        spec = QUERY_RESOURCES.get(resource)
        if spec is None:
            raise ValueError(f"unknown query resource: {resource}")
        limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        clauses, params, filter_clauses, filter_params = self._query_parts(
            spec, cursor=cursor, q=q, filters=filters, as_of=as_of, through_seq=through_seq
        )
        sql = spec.sql + ((" WHERE " + " AND ".join(clauses)) if clauses else "")
        sql += f" ORDER BY {','.join(spec.key_columns)} LIMIT ?"
        filtered = spec.sql + ((" WHERE " + " AND ".join(filter_clauses)) if filter_clauses else "")
        db_path = self.output_db_path if spec.output_db else self.runtime_db_path
        if db_path is None:
            raise ValueError("output metadata is unavailable")
        try:
            async with aiosqlite.connect(db_path, timeout=1.0) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(sql, [*params, limit + 1])).fetchall()
                count = None
                if include_count:
                    count = int(
                        (
                            await (
                                await db.execute(
                                    f"SELECT COUNT(*) FROM ({filtered}) counted", filter_params
                                )
                            ).fetchone()
                        )[0]
                    )
                facets = await self._facets(db, resource) if include_facets else None
        except aiosqlite.Error:
            self._sqlite_error(f"query:{resource}")
            raise
        has_more = len(rows) > limit
        items, oversize, page_bytes = [], [], 0
        consumed = _dec(cursor, len(spec.key_aliases))
        for row in rows[:limit]:
            key = tuple(row[name] for name in spec.key_aliases)
            item = self._normalize_query_row(resource, dict(row))
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
            consumed = key
            if page_bytes + size > MAX_PAGE_BYTES:
                oversize.append(_enc(key))
                continue
            items.append(item)
            page_bytes += size
        self._observe(
            f"query:{resource}",
            started,
            rows=len(items),
            size=page_bytes,
            reason="search" if q else "page",
        )
        result = {
            "resource": resource,
            "items": items,
            "next_cursor": _enc(consumed) if has_more and consumed else None,
            "page_rows": len(items),
            "page_bytes": page_bytes,
            "oversize_item_keys": oversize,
            "complete": not has_more,
        }
        if count is not None:
            result["count"] = count
        if facets is not None:
            result["facets"] = facets
        return result

    async def detail(self, resource, entity_id):
        spec = QUERY_RESOURCES.get(resource)
        if spec is None:
            raise ValueError(f"unknown query resource: {resource}")
        if len(spec.key_columns) == 1:
            keys = (entity_id,)
        elif resource == "tasks" and "/" in entity_id:
            keys = tuple(entity_id.split("/", 1))
        else:
            keys = _dec(entity_id, len(spec.key_columns))
            if keys is None:
                raise ValueError("detail key is required")
        marks = ",".join("?" for _ in spec.key_columns)
        sql = spec.sql + f" WHERE ({','.join(spec.key_columns)})=({marks}) LIMIT 1"
        db_path = self.output_db_path if spec.output_db else self.runtime_db_path
        if db_path is None:
            raise ValueError("output metadata is unavailable")
        async with aiosqlite.connect(db_path, timeout=1.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(sql, list(keys))).fetchone()
        return self._normalize_query_row(resource, dict(row)) if row else None

    async def namespaces(self, *, cursor=None, limit=100, q=None):
        limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        decoded = _dec(cursor, 1)
        clauses, params = [], []
        if decoded:
            clauses.append("namespace>?")
            params.append(decoded[0])
        if q:
            clauses.append("namespace LIKE ?")
            params.append(f"%{q.strip()[:200]}%")
        sql = "SELECT DISTINCT namespace AS k1 FROM work_items"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY namespace LIMIT ?"
        async with aiosqlite.connect(self.runtime_db_path, timeout=1.0) as db:
            rows = await (await db.execute(sql, [*params, limit + 1])).fetchall()
        has_more = len(rows) > limit
        values = [str(row[0]) for row in rows[:limit]]
        return {
            "namespaces": values,
            "next_cursor": _enc((values[-1],)) if has_more and values else None,
            "complete": not has_more,
        }

    async def task_graph(self, *, namespace, task_id, depth=2):
        depth = max(0, min(int(depth), 8))
        sql = """
        WITH RECURSIVE graph(namespace,task_id,depth) AS (
            SELECT ?,?,0
            UNION
            SELECT d.dependency_namespace,d.dependency_task_id,g.depth+1
            FROM graph g JOIN work_dependencies d
              ON d.namespace=g.namespace AND d.task_id=g.task_id WHERE g.depth < ?
            UNION
            SELECT r.related_namespace,r.related_task_id,g.depth+1
            FROM graph g JOIN work_relations r
              ON r.namespace=g.namespace AND r.task_id=g.task_id WHERE g.depth < ?
        )
        SELECT DISTINCT g.namespace,g.task_id,g.depth,w.title,w.state,w.lane,w.priority,w.revision
        FROM graph g LEFT JOIN work_items w
          ON w.namespace=g.namespace AND w.task_id=g.task_id
        ORDER BY g.depth,g.namespace,g.task_id
        """
        async with aiosqlite.connect(self.runtime_db_path, timeout=1.0) as db:
            db.row_factory = aiosqlite.Row
            nodes = [
                dict(row)
                for row in await (
                    await db.execute(sql, (namespace, task_id, depth, depth))
                ).fetchall()
            ]
        return {
            "root": {"namespace": namespace, "task_id": task_id},
            "depth": depth,
            "nodes": nodes,
        }

    async def _facets(self, db, resource):
        definitions = {
            "tasks": (
                ("state", "state"),
                ("lane", "lane"),
                ("priority", "priority"),
                ("namespace", "k1"),
            ),
            "commands": (("status", "status"),),
            "sessions": (("state", "state"), ("authority_node_id", "authority_node_id")),
            "slots": (("state", "state"), ("authority_node_id", "authority_node_id")),
            "messages": (("require_reply", "require_reply"), ("alert", "alert")),
            "operator_attention": (("require_reply", "require_reply"), ("alert", "alert")),
        }
        spec = QUERY_RESOURCES[resource]
        result = {}
        for name, column in definitions.get(resource, ()):
            rows = await (
                await db.execute(
                    f"SELECT {column},COUNT(*) FROM ({spec.sql}) faceted "
                    f"GROUP BY {column} ORDER BY COUNT(*) DESC,{column} LIMIT 100"
                )
            ).fetchall()
            result[name] = [{"value": row[0], "count": int(row[1])} for row in rows]
        return result

    @staticmethod
    def _normalize_query_row(resource, item):
        keys = {key: item.pop(key) for key in list(item) if key.startswith("k")}
        if resource == "tasks":
            for field, default in (
                ("resource_json", {}),
                ("reviews_json", []),
                ("checkpoint_json", {}),
                ("result_json", None),
                ("tags_json", []),
            ):
                item[field.removesuffix("_json")] = _loads(item.pop(field), default)
            item["cooperative"] = bool(item["cooperative"])
            item["namespace"], item["task_id"] = keys["k1"], keys["k2"]
        elif resource in {"task_history", "audit", "events"}:
            if "payload_json" in item:
                item["payload"] = _loads(item.pop("payload_json"), {})
            item["id"] = int(keys["k1"])
        elif resource == "activity":
            item["id"] = int(keys["k1"])
        elif resource == "message_receipts":
            item["message_hash"], item["recipient_agent_id"] = keys["k1"], keys["k2"]
        elif resource == "contexts":
            item["id"], item["is_primary"] = int(keys["k1"]), bool(item["is_primary"])
        elif resource in {"messages", "operator_attention"}:
            item["message_hash" if resource == "messages" else "message_ref"] = keys["k1"]
            item["require_reply"], item["alert"] = bool(item["require_reply"]), bool(item["alert"])
        elif resource == "output_metadata":
            item["hash"], item["truncated"] = keys["k1"], bool(item["truncated"])
        else:
            item[
                {
                    "slots": "logical_agent_id",
                    "sessions": "work_session_id",
                    "legacy_sessions": "agent_id",
                    "commands": "hash",
                }.get(resource, "id")
            ] = keys["k1"]
            if resource == "legacy_sessions":
                item["work_scope"] = _loads(item.get("work_scope"), [])
                item["details"] = _loads(item.get("details"), [])
                item["intent_scopes"] = _loads(item.get("intent_scopes"), {})
            if resource == "commands" and item.get("truncated") is not None:
                item["truncated"] = bool(item["truncated"])
        return item

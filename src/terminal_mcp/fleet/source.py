from __future__ import annotations

import json
from typing import Any

import aiosqlite

from terminal_mcp.fleet.protocol import (
    FLEET_PROTOCOL_MAJOR,
    MAX_SOURCE_REPLAY_LIMIT,
    SOURCE_CAPABILITIES,
    SourceEvent,
    canonical_event_id,
)
from terminal_mcp.fleet.source_query import CURRENT_SCOPE_VERSION, FleetSourceQueryPlane
from terminal_mcp.storage.events import EventJournalStore
from terminal_mcp.version import __version__

_CANONICAL_ENTITY_TYPES = {
    "logical_agent",
    "work_session",
    "node_attachment",
    "attachment_presence",
    "message_obligation",
    "work_claim",
    "task",
    "command",
    "context",
}


class FleetSourceService:
    """Canonical read-only Fleet v1 source over durable local runtime truth."""

    def __init__(
        self,
        runtime_db_path,
        journal: EventJournalStore,
        meta_store,
        *,
        runtime_health_provider=None,
        output_db_path=None,
        auth_db_path=None,
        metrics=None,
        events=None,
    ):
        self.runtime_db_path = runtime_db_path
        self.journal = journal
        self.meta_store = meta_store
        self.runtime_health_provider = runtime_health_provider
        self.query_plane = FleetSourceQueryPlane(
            runtime_db_path,
            output_db_path=output_db_path,
            auth_db_path=auth_db_path,
            metrics=metrics,
            events=events,
        )

    async def _current_meta(self):
        high_water = await self.journal.high_water_seq()
        meta, rotated = await self.meta_store.observe_journal(high_water)
        return meta, high_water, rotated

    async def manifest(self) -> dict[str, Any]:
        meta, high_water, rotated = await self._current_meta()
        bounds = await self.journal.read(since=high_water, limit=1)
        return {
            "protocol_major": FLEET_PROTOCOL_MAJOR,
            "fleet_id": meta.fleet_id,
            "node_id": meta.node_id,
            "capabilities": sorted(SOURCE_CAPABILITIES),
            "source_stream_generation": meta.source_stream_generation,
            "oldest_source_seq": bounds["oldest_seq"],
            "high_water_source_seq": high_water,
            "generation_rotated": rotated,
        }

    @staticmethod
    def _event_authority(raw: dict[str, Any]) -> tuple[str | None, int | None]:
        payload = raw.get("payload") or {}
        authority_node_id = payload.get("authority_node_id")
        authority_epoch = payload.get("authority_epoch")
        if authority_node_id is not None:
            authority_node_id = str(authority_node_id)
        if authority_epoch is not None:
            try:
                authority_epoch = int(authority_epoch)
            except (TypeError, ValueError):
                authority_epoch = None
        return authority_node_id, authority_epoch

    def _canonical_event(self, raw: dict[str, Any], meta) -> SourceEvent | None:
        entity_type = str(raw["entity_type"])
        if entity_type not in _CANONICAL_ENTITY_TYPES:
            return None
        seq = int(raw["seq"])
        payload = dict(raw.get("payload") or {})
        if entity_type == "attachment_presence":
            for encoded_key, decoded_key in (
                ("work_scope_json", "work_scope"),
                ("details_json", "details"),
            ):
                encoded = payload.pop(encoded_key, "[]")
                try:
                    payload[decoded_key] = json.loads(encoded)
                except (TypeError, ValueError):
                    payload[decoded_key] = []
        native_revision = payload.get("slot_revision") if entity_type == "logical_agent" else None
        try:
            entity_revision = int(native_revision) if native_revision is not None else seq
        except (TypeError, ValueError):
            entity_revision = seq
        authority_node_id, authority_epoch = self._event_authority(raw)
        return SourceEvent(
            event_id=canonical_event_id(
                meta.fleet_id,
                meta.node_id,
                meta.source_stream_generation,
                seq,
            ),
            fleet_id=meta.fleet_id,
            node_id=meta.node_id,
            source_stream_generation=meta.source_stream_generation,
            source_seq=seq,
            event_type=str(raw["event_type"]),
            entity_type=entity_type,
            entity_id=str(raw["entity_id"]),
            entity_revision=max(1, entity_revision),
            payload_version=1,
            payload=payload,
            created_at=str(raw["created_at"]),
            authority_node_id=authority_node_id,
            authority_epoch=authority_epoch,
        )

    async def events(
        self,
        *,
        since: int = 0,
        limit: int = 100,
        source_stream_generation: str | None = None,
    ) -> dict[str, Any]:
        since = int(since)
        if since < 0:
            raise ValueError("since must be non-negative")
        limit = max(1, min(int(limit), MAX_SOURCE_REPLAY_LIMIT))
        meta, high_water, rotated = await self._current_meta()
        if source_stream_generation and source_stream_generation != meta.source_stream_generation:
            return {
                "protocol_major": FLEET_PROTOCOL_MAJOR,
                "fleet_id": meta.fleet_id,
                "node_id": meta.node_id,
                "source_stream_generation": meta.source_stream_generation,
                "events": [],
                "since": since,
                "next_cursor": 0,
                "high_water_source_seq": high_water,
                "gap": False,
                "reset_required": True,
                "reset_reason": "source_stream_generation_changed",
            }

        page = await self.journal.read(since=since, limit=limit)
        needs_access_identity = any(
            str(raw.get("entity_type") or "") == "logical_agent" for raw in page["events"]
        )
        identities = await self.query_plane.access_identities() if needs_access_identity else {}
        events = []
        for raw in page["events"]:
            event = self._canonical_event(raw, meta)
            if event is not None:
                item = event.to_dict()
                identity = (
                    identities.get(event.entity_id)
                    if event.entity_type == "logical_agent"
                    else None
                )
                if identity:
                    item["payload"] = {**item["payload"], **identity}
                events.append(item)
        return {
            "protocol_major": FLEET_PROTOCOL_MAJOR,
            "fleet_id": meta.fleet_id,
            "node_id": meta.node_id,
            "source_stream_generation": meta.source_stream_generation,
            "events": events,
            "since": since,
            "next_cursor": page["next_cursor"],
            "oldest_source_seq": page["oldest_seq"],
            "high_water_source_seq": high_water,
            "gap": bool(page["gap"]),
            "gap_from_seq": page["gap_from_seq"],
            "gap_to_seq": page["gap_to_seq"],
            "reset_required": bool(rotated or page["gap"]),
            "reset_reason": (
                "runtime_journal_regressed"
                if rotated
                else ("retention_gap" if page["gap"] else None)
            ),
        }

    async def bootstrap(self) -> dict[str, Any]:
        meta, high_water, rotated = await self._current_meta()
        return {
            "protocol_major": FLEET_PROTOCOL_MAJOR,
            "fleet_id": meta.fleet_id,
            "node_id": meta.node_id,
            "source_stream_generation": meta.source_stream_generation,
            "scope_version": CURRENT_SCOPE_VERSION,
            "current_scopes": self.query_plane.scope_names(),
            "barrier_source_seq": high_water,
            "high_water_source_seq": high_water,
            "generation_rotated": rotated,
            "resources": await self.query_plane.sampled_resources(),
        }

    async def current_recovery(
        self,
        scope: str,
        *,
        source_stream_generation: str | None = None,
        snapshot_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        barrier_source_seq: int | None = None,
    ) -> dict[str, Any]:
        meta, high_water, rotated = await self._current_meta()
        if source_stream_generation and source_stream_generation != meta.source_stream_generation:
            return {
                "source_stream_generation": meta.source_stream_generation,
                "scope": scope,
                "entities": [],
                "reset_required": True,
                "reset_reason": "source_stream_generation_changed",
            }
        requested_barrier = high_water if barrier_source_seq is None else int(barrier_source_seq)
        if requested_barrier < 0 or requested_barrier > high_water:
            raise ValueError("invalid recovery barrier_source_seq")
        page = await self.query_plane.recovery_page(
            scope=scope,
            generation=meta.source_stream_generation,
            barrier=requested_barrier,
            high_water=high_water,
            snapshot_id=snapshot_id,
            cursor=cursor,
            limit=limit,
        )
        page["fleet_id"] = meta.fleet_id
        page["node_id"] = meta.node_id
        page["generation_rotated"] = rotated
        page["reset_required"] = False
        return page

    async def current_entity(
        self,
        scope: str,
        entity_id: str,
        *,
        source_stream_generation: str | None = None,
    ) -> dict[str, Any]:
        meta, high_water, rotated = await self._current_meta()
        if source_stream_generation and source_stream_generation != meta.source_stream_generation:
            return {
                "fleet_id": meta.fleet_id,
                "node_id": meta.node_id,
                "source_stream_generation": meta.source_stream_generation,
                "scope": scope,
                "entity_id": entity_id,
                "entity": None,
                "reset_required": True,
                "reset_reason": "source_stream_generation_changed",
            }
        entity = await self.query_plane.current_entity(
            scope=scope,
            entity_id=entity_id,
            barrier=high_water,
        )
        return {
            "fleet_id": meta.fleet_id,
            "node_id": meta.node_id,
            "source_stream_generation": meta.source_stream_generation,
            "scope": scope,
            "entity_id": entity_id,
            "entity": entity,
            "materialized_at_source_seq": high_water,
            "generation_rotated": rotated,
            "reset_required": False,
        }

    async def query(self, resource: str, **kwargs) -> dict[str, Any]:
        return await self.query_plane.query(resource, **kwargs)

    async def detail(self, resource: str, entity_id: str):
        return await self.query_plane.detail(resource, entity_id)

    async def query_namespaces(self, **kwargs) -> dict[str, Any]:
        return await self.query_plane.namespaces(**kwargs)

    async def task_graph(self, **kwargs) -> dict[str, Any]:
        return await self.query_plane.task_graph(**kwargs)

    async def runtime_health(self) -> dict[str, Any]:
        resources = await self.query_plane.sampled_resources()
        if self.runtime_health_provider is None:
            return {
                "application": "terminal-mcp",
                "version": __version__,
                "finalization_pending_commands": [],
                "stale_running_commands": [],
                "unowned_running_commands": [],
                "queues": [],
                "resources": resources,
            }
        value = await self.runtime_health_provider()
        return {
            "application": "terminal-mcp",
            "version": __version__,
            "finalization_pending_commands": list(value.get("finalization_pending_commands") or []),
            "stale_running_commands": list(value.get("stale_running_commands") or []),
            "unowned_running_commands": list(value.get("unowned_running_commands") or []),
            "queues": list(value.get("queues") or []),
            "resources": resources,
        }

    async def snapshot(self) -> dict[str, Any]:
        meta, barrier, rotated = await self._current_meta()
        entities = await self._snapshot_entities(barrier)
        return {
            "protocol_major": FLEET_PROTOCOL_MAJOR,
            "fleet_id": meta.fleet_id,
            "node_id": meta.node_id,
            "source_stream_generation": meta.source_stream_generation,
            "barrier_source_seq": barrier,
            "generation_rotated": rotated,
            "replace": True,
            "complete_entity_types": [
                "logical_agent",
                "work_session",
                "node_attachment",
                "attachment_presence",
                "message_obligation",
                "work_claim",
                "task",
                "command",
            ],
            "entities": entities,
        }

    async def _snapshot_entities(self, barrier: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.runtime_db_path, timeout=1.0) as db:
            logical_agents = await (
                await db.execute(
                    "SELECT logical_agent_id,display_name,state,authority_node_id,authority_epoch,"
                    "slot_revision,selector_generation,auth_generation,created_at,updated_at,"
                    "deleted_at,tombstone_reason FROM logical_agents ORDER BY logical_agent_id"
                )
            ).fetchall()
            work_sessions = await (
                await db.execute(
                    "SELECT work_session_id,logical_agent_id,session_epoch,authority_node_id,"
                    "authority_epoch,started_at,hard_expires_at,auth_generation,state,"
                    "origin_instance_id,ended_at,end_reason "
                    "FROM logical_agent_work_sessions ORDER BY logical_agent_id,session_epoch"
                )
            ).fetchall()
            attachments = await (
                await db.execute(
                    "SELECT node_attachment_id,logical_agent_id,work_session_id,session_epoch,"
                    "node_instance_id,authority_epoch,attached_at,hard_expires_at,revoked_at "
                    "FROM logical_agent_node_attachments "
                    "ORDER BY logical_agent_id,session_epoch,node_instance_id"
                )
            ).fetchall()
            presence = await (
                await db.execute(
                    "SELECT p.node_attachment_id,p.logical_agent_id,p.work_session_id,"
                    "p.session_epoch,p.node_instance_id,p.task_summary,p.intent,"
                    "p.work_scope_json,p.details_json,p.current_step,p.intent_updated_at,"
                    "p.last_activity_at "
                    "FROM persistent_attachment_presence p "
                    "ORDER BY p.logical_agent_id,p.session_epoch,p.node_instance_id"
                )
            ).fetchall()
            obligations = await (
                await db.execute(
                    "SELECT message_ref,logical_agent_id,sender_agent_id,text,require_reply,"
                    "alert,gate_revision,created_at,resolved_at,resolution "
                    "FROM persistent_message_obligations ORDER BY created_at,message_ref"
                )
            ).fetchall()
            claims = await (
                await db.execute(
                    "SELECT id,namespace,task_id,owner_kind,owner_id,claimed_at,released_at "
                    "FROM work_claims WHERE owner_kind='logical_agent' ORDER BY id"
                )
            ).fetchall()
            tasks = await (
                await db.execute(
                    "SELECT namespace,task_id,lane,priority,state,cooperative,revision,"
                    "state_changed_at,updated_at,archived_at,input_refs_json,output_refs_json,"
                    "output_state_id FROM work_items ORDER BY namespace,task_id"
                )
            ).fetchall()
            commands = await (
                await db.execute(
                    "SELECT c.hash,c.status,c.exit_code,c.queue_id,c.started_at,c.finished_at,"
                    "a.logical_agent_id,a.work_session_id,a.session_epoch "
                    "FROM commands c LEFT JOIN command_agent_attribution a "
                    "ON a.command_hash=c.hash "
                    "ORDER BY c.rowid"
                )
            ).fetchall()

        identities = await self.query_plane.access_identities()
        entities: list[dict[str, Any]] = []
        for row in logical_agents:
            entities.append(
                self._snapshot_entity(
                    "logical_agent",
                    row[0],
                    int(row[5]),
                    {
                        "logical_agent_id": row[0],
                        "display_name": row[1],
                        **identities.get(str(row[0]), {}),
                        "state": row[2],
                        "authority_node_id": row[3],
                        "authority_epoch": int(row[4]),
                        "slot_revision": int(row[5]),
                        "selector_generation": int(row[6]),
                        "auth_generation": int(row[7]),
                        "created_at": row[8],
                        "updated_at": row[9],
                        "deleted_at": row[10],
                        "tombstone_reason": row[11],
                    },
                    authority_node_id=row[3],
                    authority_epoch=int(row[4]),
                )
            )
        for row in work_sessions:
            entities.append(
                self._snapshot_entity(
                    "work_session",
                    row[0],
                    max(1, barrier),
                    {
                        "work_session_id": row[0],
                        "logical_agent_id": row[1],
                        "session_epoch": int(row[2]),
                        "authority_node_id": row[3],
                        "authority_epoch": int(row[4]),
                        "started_at": row[5],
                        "hard_expires_at": row[6],
                        "auth_generation": int(row[7]),
                        "state": row[8],
                        "origin_instance_id": row[9],
                        "ended_at": row[10],
                        "end_reason": row[11],
                    },
                    authority_node_id=row[3],
                    authority_epoch=int(row[4]),
                )
            )
        for row in attachments:
            entities.append(
                self._snapshot_entity(
                    "node_attachment",
                    row[0],
                    max(1, barrier),
                    {
                        "node_attachment_id": row[0],
                        "logical_agent_id": row[1],
                        "work_session_id": row[2],
                        "session_epoch": int(row[3]),
                        "node_instance_id": row[4],
                        "authority_epoch": int(row[5]),
                        "attached_at": row[6],
                        "hard_expires_at": row[7],
                        "revoked_at": row[8],
                    },
                    authority_epoch=int(row[5]),
                )
            )
        for row in presence:
            entities.append(
                self._snapshot_entity(
                    "attachment_presence",
                    row[0],
                    max(1, barrier),
                    {
                        "node_attachment_id": row[0],
                        "logical_agent_id": row[1],
                        "work_session_id": row[2],
                        "session_epoch": int(row[3]),
                        "node_instance_id": row[4],
                        "task_summary": row[5],
                        "intent": row[6],
                        "work_scope": json.loads(row[7]),
                        "details": json.loads(row[8]),
                        "current_step": int(row[9]),
                        "intent_updated_at": row[10],
                        "last_activity_at": row[11],
                    },
                )
            )
        for row in obligations:
            entities.append(
                self._snapshot_entity(
                    "message_obligation",
                    row[0],
                    max(1, int(row[6])),
                    {
                        "message_ref": row[0],
                        "logical_agent_id": row[1],
                        "sender_agent_id": row[2],
                        "text": row[3],
                        "require_reply": bool(row[4]),
                        "alert": bool(row[5]),
                        "gate_revision": int(row[6]),
                        "created_at": row[7],
                        "resolved_at": row[8],
                        "resolution": row[9],
                    },
                )
            )
        for row in claims:
            entities.append(
                self._snapshot_entity(
                    "work_claim",
                    str(row[0]),
                    max(1, barrier),
                    {
                        "claim_id": int(row[0]),
                        "namespace": row[1],
                        "task_id": row[2],
                        "owner_kind": row[3],
                        "owner_id": row[4],
                        "claimed_at": row[5],
                        "released_at": row[6],
                    },
                )
            )
        for row in tasks:
            entities.append(
                self._snapshot_entity(
                    "task",
                    f"{row[0]}/{row[1]}",
                    max(1, int(row[6])),
                    {
                        "namespace": row[0],
                        "task_id": row[1],
                        "lane": row[2],
                        "priority": int(row[3]),
                        "state": row[4],
                        "cooperative": bool(row[5]),
                        "revision": int(row[6]),
                        "state_changed_at": row[7],
                        "updated_at": row[8],
                        "archived_at": row[9],
                        "input_refs": json.loads(row[10] or "[]"),
                        "output_refs": json.loads(row[11] or "[]"),
                        "output_state_id": row[12],
                    },
                )
            )
        for row in commands:
            entities.append(
                self._snapshot_entity(
                    "command",
                    row[0],
                    max(1, barrier),
                    {
                        "command_hash": row[0],
                        "status": row[1],
                        "exit_code": row[2],
                        "queue_id": row[3],
                        "started_at": row[4],
                        "finished_at": row[5],
                        "logical_agent_id": row[6],
                        "work_session_id": row[7],
                        "session_epoch": row[8],
                    },
                )
            )
        return entities

    @staticmethod
    def _snapshot_entity(
        entity_type: str,
        entity_id: str,
        entity_revision: int,
        payload: dict[str, Any],
        *,
        authority_node_id: str | None = None,
        authority_epoch: int | None = None,
    ) -> dict[str, Any]:
        item = {
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "entity_revision": max(1, int(entity_revision)),
            "payload_version": 1,
            "payload": payload,
        }
        if authority_node_id is not None:
            item["authority_node_id"] = authority_node_id
        if authority_epoch is not None:
            item["authority_epoch"] = int(authority_epoch)
        return item

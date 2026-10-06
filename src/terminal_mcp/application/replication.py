"""Transport-independent Fleet replication, source query and projection use cases.

Authorization headers and HTTP error statuses never cross this boundary. Existing
source and replication services remain the authoritative domain implementations.
"""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError
from terminal_mcp.fleet.identity import SignedAgentIdentity


class ReplicationApplication:
    """Actor-aware application boundary over existing Fleet domain services."""

    def __init__(self, replication):
        self._replication = replication

    async def receive_identity(self, actor: ActorContext, *, payload: dict):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                envelope = SignedAgentIdentity.from_dict(payload)
                status = await self._replication.receive(
                    envelope, authenticated_peer_id=actor.peer_node_id
                )
            except (TypeError, ValueError) as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            return {"ok": True, "status": status}

    async def update_session(self, actor: ActorContext, *, payload: dict):
        MeshApplication._require_peer(actor)
        with actor.bind():
            agent_id = payload.get("agent_id")
            source_instance_id = payload.get("source_instance_id")
            activity_at = payload.get("activity_at")
            intent = payload.get("intent")
            step = payload.get("step")
            intent_updated_at = payload.get("intent_updated_at")
            if not all(
                isinstance(value, str) and value
                for value in (agent_id, source_instance_id, activity_at)
            ):
                raise MeshApplicationError(
                    "invalid_request", "session update payload is incomplete"
                )
            if intent is not None and (not isinstance(intent, str)):
                raise MeshApplicationError(
                    "invalid_request", "session update intent must be a string"
                )
            if step is not None and (not isinstance(step, int)):
                raise MeshApplicationError(
                    "invalid_request", "session update step must be an integer"
                )
            if intent_updated_at is not None and (not isinstance(intent_updated_at, str)):
                raise MeshApplicationError(
                    "invalid_request", "session update intent_updated_at must be a string"
                )
            try:
                changed = await self._replication.receive_session_update(
                    agent_id,
                    source_instance_id,
                    activity_at,
                    intent=intent,
                    step=step,
                    intent_updated_at=intent_updated_at,
                    authenticated_peer_id=actor.peer_node_id,
                )
            except (TypeError, ValueError) as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            return {"ok": True, "changed": changed}

    async def finish_session(self, actor: ActorContext, *, payload: dict):
        MeshApplication._require_peer(actor)
        with actor.bind():
            agent_id = payload.get("agent_id")
            ended_at = payload.get("ended_at")
            reason = payload.get("reason")
            if not all(isinstance(value, str) and value for value in (agent_id, ended_at, reason)):
                raise MeshApplicationError("invalid_request", "finish payload is incomplete")
            try:
                changed = await self._replication.receive_finish(
                    agent_id, ended_at, reason, authenticated_peer_id=actor.peer_node_id
                )
            except (TypeError, ValueError) as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            return {"ok": True, "changed": changed}

    async def read_identities(
        self, actor: ActorContext, *, agent_id: str | None = None, limit: int = 500
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            identities = await self._replication.list_identities(agent_id=agent_id, limit=limit)
            return {"ok": True, "identities": identities}


class FleetSourceApplication:
    """Actor-aware application boundary over existing Fleet domain services."""

    def __init__(self, source):
        self._source = source

    async def manifest(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.manifest()

    async def events(
        self,
        actor: ActorContext,
        *,
        since: int = 0,
        limit: int = 100,
        source_stream_generation: str | None = None,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                return await self._source.events(
                    since=since, limit=limit, source_stream_generation=source_stream_generation
                )
            except ValueError as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc

    async def runtime_health(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.runtime_health()

    async def snapshot(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.snapshot()

    async def bootstrap(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.bootstrap()

    async def current_recovery(
        self,
        actor: ActorContext,
        *,
        scope: str,
        source_stream_generation: str | None = None,
        snapshot_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        barrier_source_seq: int | None = None,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                return await self._source.current_recovery(
                    scope,
                    source_stream_generation=source_stream_generation,
                    snapshot_id=snapshot_id,
                    cursor=cursor,
                    limit=limit,
                    barrier_source_seq=barrier_source_seq,
                )
            except ValueError as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc

    async def current_entity(
        self,
        actor: ActorContext,
        *,
        scope: str,
        entity_id: str,
        source_stream_generation: str | None = None,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                return await self._source.current_entity(
                    scope, entity_id, source_stream_generation=source_stream_generation
                )
            except ValueError as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc

    async def query(
        self,
        actor: ActorContext,
        *,
        resource: str,
        cursor: str | None = None,
        limit: int = 100,
        q: str | None = None,
        namespace: str | None = None,
        task_id: str | None = None,
        state: str | None = None,
        lane: str | None = None,
        priority: int | None = None,
        status: str | None = None,
        agent_id: str | None = None,
        logical_agent_id: str | None = None,
        work_session_id: str | None = None,
        event_type: str | None = None,
        as_of: str | None = None,
        through_seq: int | None = None,
        include_count: bool = False,
        include_facets: bool = False,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            filters = {
                "namespace": namespace,
                "task_id": task_id,
                "state": state,
                "lane": lane,
                "priority": priority,
                "status": status,
                "agent_id": agent_id,
                "logical_agent_id": logical_agent_id,
                "work_session_id": work_session_id,
                "event_type": event_type,
            }
            try:
                return await self._source.query(
                    resource,
                    cursor=cursor,
                    limit=limit,
                    q=q,
                    filters={key: value for key, value in filters.items() if value is not None},
                    as_of=as_of,
                    through_seq=through_seq,
                    include_count=include_count,
                    include_facets=include_facets,
                )
            except ValueError as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc

    async def detail(self, actor: ActorContext, *, resource: str, entity_id: str):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                item = await self._source.detail(resource, entity_id)
            except ValueError as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            if item is None:
                raise MeshApplicationError("not_found", "entity not found")
            return item

    async def namespaces(
        self,
        actor: ActorContext,
        *,
        cursor: str | None = None,
        limit: int = 100,
        q: str | None = None,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.query_namespaces(cursor=cursor, limit=limit, q=q)

    async def task_graph(
        self, actor: ActorContext, *, namespace: str, task_id: str, depth: int = 2
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            return await self._source.task_graph(namespace=namespace, task_id=task_id, depth=depth)


class FleetProjectionApplication:
    """Actor-aware application boundary over existing Fleet domain services."""

    def __init__(self, projection, projection_service=None):
        self._projection = projection
        self._projection_service = projection_service

    async def manifest(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            result = await self._projection.meta()
            result["capabilities"] = (
                self._projection_service.capabilities if self._projection_service else []
            )
            return result

    async def snapshot(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            try:
                return await self._projection.replica_snapshot()
            except Exception as exc:
                raise MeshApplicationError("conflict", str(exc)) from exc

    async def events(
        self,
        actor: ActorContext,
        *,
        since: int = 0,
        projection_epoch: int | None = None,
        limit: int = 100,
    ):
        MeshApplication._require_peer(actor)
        with actor.bind():
            meta = await self._projection.meta()
            if projection_epoch is not None and projection_epoch != meta["projection_epoch"]:
                return {
                    "projection_epoch": meta["projection_epoch"],
                    "projection_seq": meta["projection_seq"],
                    "events": [],
                    "reset_required": True,
                }
            result = await self._projection.events(since=since, limit=limit)
            result["reset_required"] = False
            return result

    async def runtime_overlays(self, actor: ActorContext):
        MeshApplication._require_peer(actor)
        with actor.bind():
            snapshot = await self._projection.snapshot()
            return {
                "projection_epoch": snapshot["projection_epoch"],
                "projection_seq": snapshot["projection_seq"],
                "sources": snapshot["sources"],
                "scope_statuses": snapshot.get("scope_statuses") or [],
                "runtime_overlays": snapshot["runtime_overlays"],
            }

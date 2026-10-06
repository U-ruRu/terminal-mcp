from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplicationError
from terminal_mcp.application.replication import (
    FleetProjectionApplication,
    FleetSourceApplication,
)
from terminal_mcp.fleet.protocol import MAX_SOURCE_REPLAY_LIMIT

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}


def build_fleet_v1_source_router(source, replication, *, application=None) -> APIRouter:
    router = APIRouter()
    target = application if application is not None else FleetSourceApplication(source)

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return ActorContext(
            transport="mesh",
            endpoint_role="mesh",
            node_id=str(getattr(getattr(replication, "config", None), "instance_id", "") or ""),
            peer_node_id=peer.instance_id,
        )

    @router.get("/internal/fleet/v1/source/manifest", include_in_schema=False)
    async def manifest(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.manifest(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/events", include_in_schema=False)
    async def events(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=MAX_SOURCE_REPLAY_LIMIT),
        source_stream_generation: str | None = Query(default=None),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.events(
                actor, since=since, limit=limit, source_stream_generation=source_stream_generation
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/runtime-health", include_in_schema=False)
    async def runtime_health(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.runtime_health(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/snapshot", include_in_schema=False)
    async def snapshot(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.snapshot(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/bootstrap", include_in_schema=False)
    async def bootstrap(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.bootstrap(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/current/{scope}", include_in_schema=False)
    async def current_recovery(
        scope: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        source_stream_generation: str | None = Query(default=None),
        snapshot_id: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        barrier_source_seq: int | None = Query(default=None, ge=0),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.current_recovery(
                actor,
                scope=scope,
                source_stream_generation=source_stream_generation,
                snapshot_id=snapshot_id,
                cursor=cursor,
                limit=limit,
                barrier_source_seq=barrier_source_seq,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/current/{scope}/entity", include_in_schema=False)
    async def current_entity(
        scope: str,
        entity_id: str = Query(min_length=1, max_length=260),
        source_stream_generation: str | None = Query(default=None),
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.current_entity(
                actor,
                scope=scope,
                entity_id=entity_id,
                source_stream_generation=source_stream_generation,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/query/{resource}", include_in_schema=False)
    async def query(
        resource: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        q: str | None = Query(default=None),
        namespace: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
        state: str | None = Query(default=None),
        lane: str | None = Query(default=None),
        priority: int | None = Query(default=None),
        status: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        logical_agent_id: str | None = Query(default=None),
        work_session_id: str | None = Query(default=None),
        event_type: str | None = Query(default=None),
        as_of: str | None = Query(default=None),
        through_seq: int | None = Query(default=None, ge=0),
        include_count: bool = Query(default=False),
        include_facets: bool = Query(default=False),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.query(
                actor,
                resource=resource,
                cursor=cursor,
                limit=limit,
                q=q,
                namespace=namespace,
                task_id=task_id,
                state=state,
                lane=lane,
                priority=priority,
                status=status,
                agent_id=agent_id,
                logical_agent_id=logical_agent_id,
                work_session_id=work_session_id,
                event_type=event_type,
                as_of=as_of,
                through_seq=through_seq,
                include_count=include_count,
                include_facets=include_facets,
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/detail/{resource}", include_in_schema=False)
    async def detail(
        resource: str,
        entity_id: str = Query(...),
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.detail(actor, resource=resource, entity_id=entity_id)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/namespaces", include_in_schema=False)
    async def namespaces(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        q: str | None = Query(default=None),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.namespaces(actor, cursor=cursor, limit=limit, q=q)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/source/task-graph", include_in_schema=False)
    async def task_graph(
        namespace: str,
        task_id: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        depth: int = Query(default=2, ge=0, le=8),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.task_graph(actor, namespace=namespace, task_id=task_id, depth=depth)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router


def build_fleet_v1_projection_router(
    projection, replication, projection_service=None, *, application=None
) -> APIRouter:
    router = APIRouter()
    target = (
        application
        if application is not None
        else FleetProjectionApplication(projection, projection_service)
    )

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return ActorContext(
            transport="mesh",
            endpoint_role="mesh",
            node_id=str(getattr(getattr(replication, "config", None), "instance_id", "") or ""),
            peer_node_id=peer.instance_id,
        )

    @router.get("/internal/fleet/v1/projection/manifest", include_in_schema=False)
    async def manifest(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.manifest(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/projection/snapshot", include_in_schema=False)
    async def snapshot(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.snapshot(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/projection/events", include_in_schema=False)
    async def events(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        since: int = Query(default=0, ge=0),
        projection_epoch: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.events(
                actor, since=since, projection_epoch=projection_epoch, limit=limit
            )
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/v1/projection/runtime-overlays", include_in_schema=False)
    async def runtime_overlays(
        x_terminal_mcp_peer: str = Header(default=""), authorization: str = Header(default="")
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.runtime_overlays(actor)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from terminal_mcp.fleet.protocol import MAX_SOURCE_REPLAY_LIMIT


def build_fleet_v1_source_router(source, replication) -> APIRouter:
    router = APIRouter()

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return peer

    @router.get("/internal/fleet/v1/source/manifest", include_in_schema=False)
    async def manifest(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.manifest()

    @router.get("/internal/fleet/v1/source/events", include_in_schema=False)
    async def events(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=MAX_SOURCE_REPLAY_LIMIT),
        source_stream_generation: str | None = Query(default=None),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await source.events(
                since=since,
                limit=limit,
                source_stream_generation=source_stream_generation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/internal/fleet/v1/source/runtime-health", include_in_schema=False)
    async def runtime_health(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.runtime_health()

    @router.get("/internal/fleet/v1/source/snapshot", include_in_schema=False)
    async def snapshot(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.snapshot()

    @router.get("/internal/fleet/v1/source/bootstrap", include_in_schema=False)
    async def bootstrap(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.bootstrap()

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
        authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await source.current_recovery(
                scope,
                source_stream_generation=source_stream_generation,
                snapshot_id=snapshot_id,
                cursor=cursor,
                limit=limit,
                barrier_source_seq=barrier_source_seq,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get(
        "/internal/fleet/v1/source/current/{scope}/entity",
        include_in_schema=False,
    )
    async def current_entity(
        scope: str,
        entity_id: str = Query(min_length=1, max_length=260),
        source_stream_generation: str | None = Query(default=None),
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await source.current_entity(
                scope,
                entity_id,
                source_stream_generation=source_stream_generation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        authenticate(x_terminal_mcp_peer, authorization)
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
            return await source.query(
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/internal/fleet/v1/source/detail/{resource}", include_in_schema=False)
    async def detail(
        resource: str,
        entity_id: str = Query(...),
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        try:
            item = await source.detail(resource, entity_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return item

    @router.get("/internal/fleet/v1/source/namespaces", include_in_schema=False)
    async def namespaces(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
        q: str | None = Query(default=None),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.query_namespaces(cursor=cursor, limit=limit, q=q)

    @router.get("/internal/fleet/v1/source/task-graph", include_in_schema=False)
    async def task_graph(
        namespace: str,
        task_id: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        depth: int = Query(default=2, ge=0, le=8),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.task_graph(namespace=namespace, task_id=task_id, depth=depth)

    return router


def build_fleet_v1_projection_router(projection, replication, projection_service=None) -> APIRouter:
    router = APIRouter()

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return peer

    @router.get("/internal/fleet/v1/projection/manifest", include_in_schema=False)
    async def manifest(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        result = await projection.meta()
        result["capabilities"] = (
            projection_service.capabilities if projection_service else []
        )
        return result

    @router.get("/internal/fleet/v1/projection/snapshot", include_in_schema=False)
    async def snapshot(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await projection.replica_snapshot()
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/internal/fleet/v1/projection/events", include_in_schema=False)
    async def events(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        since: int = Query(default=0, ge=0),
        projection_epoch: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        meta = await projection.meta()
        if projection_epoch is not None and projection_epoch != meta["projection_epoch"]:
            return {
                "projection_epoch": meta["projection_epoch"],
                "projection_seq": meta["projection_seq"],
                "events": [],
                "reset_required": True,
            }
        result = await projection.events(since=since, limit=limit)
        result["reset_required"] = False
        return result

    @router.get(
        "/internal/fleet/v1/projection/runtime-overlays",
        include_in_schema=False,
    )
    async def runtime_overlays(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        snapshot = await projection.snapshot()
        return {
            "projection_epoch": snapshot["projection_epoch"],
            "projection_seq": snapshot["projection_seq"],
            "runtime_overlays": snapshot["runtime_overlays"],
        }

    return router

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

    return router



def build_fleet_v1_projection_router(projection, replication) -> APIRouter:
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
        return await projection.meta()

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

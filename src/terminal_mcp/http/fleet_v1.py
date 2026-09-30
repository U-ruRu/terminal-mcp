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

    @router.get("/internal/fleet/v1/source/snapshot", include_in_schema=False)
    async def snapshot(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        return await source.snapshot()

    return router

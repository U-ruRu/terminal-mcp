from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplicationError
from terminal_mcp.application.replication import (
    ReplicationApplication,
)

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
}


def build_fleet_router(replication, *, application=None) -> APIRouter:
    router = APIRouter()
    target = application if application is not None else ReplicationApplication(replication)

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

    @router.post("/internal/fleet/identities", include_in_schema=False)
    async def receive_identity(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.receive_identity(actor, payload=payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/session-update", include_in_schema=False)
    async def update_session(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.update_session(actor, payload=payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/session-finish", include_in_schema=False)
    async def finish_session(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.finish_session(actor, payload=payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/identities", include_in_schema=False)
    async def read_identities(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        agent_id: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.read_identities(actor, agent_id=agent_id, limit=limit)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router

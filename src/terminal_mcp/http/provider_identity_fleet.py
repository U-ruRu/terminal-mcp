from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplicationError

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "unavailable": 503,
}


def build_provider_identity_fleet_router(replication, bridge, *, application) -> APIRouter:
    router = APIRouter()

    def authenticate(peer_id: str, authorization: str) -> ActorContext:
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return ActorContext(
            transport="mesh",
            endpoint_role="mesh",
            node_id=bridge.config.instance_id,
            peer_node_id=peer.instance_id,
        )

    @router.post(
        "/internal/fleet/persistent/access/provider-resolve", include_in_schema=False
    )
    async def provider_resolve(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await application.provider_resolve(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post(
        "/internal/fleet/persistent/access/provider-bind", include_in_schema=False
    )
    async def provider_bind(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await application.provider_bind(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router

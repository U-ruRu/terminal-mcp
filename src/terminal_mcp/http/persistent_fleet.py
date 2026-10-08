from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.mesh import MeshApplication, MeshApplicationError

_ERROR_STATUS = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "unavailable": 503,
    "authority_unavailable": 503,
    "wrong_authority": 409,
    "recovery_required": 409,
}


def build_persistent_fleet_router(
    replication, bridge, backend=None, *, application=None
) -> APIRouter:
    """Adapt authenticated peer requests to the canonical Mesh capability.

    The optional capability is injected by the composition root; direct router
    callers retain their legacy construction signature for compatibility.
    """
    router = APIRouter()
    target = application if application is not None else MeshApplication(bridge, backend)

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

    @router.post("/internal/fleet/persistent/access/resolve", include_in_schema=False)
    async def access_resolve(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_resolve(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/list", include_in_schema=False)
    async def access_list(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_list(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/by-name", include_in_schema=False)
    async def access_by_name(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_by_name(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/get", include_in_schema=False)
    async def access_get(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_get(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/display", include_in_schema=False)
    async def access_display(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_display(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/register", include_in_schema=False)
    async def access_register(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_register(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/rotate", include_in_schema=False)
    async def access_rotate(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_rotate(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/access/retire", include_in_schema=False)
    async def access_retire(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.access_retire(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/unified-session/status-name", include_in_schema=False)
    async def unified_session_status_name(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.unified_session_status_name(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/unified-session/status-batch", include_in_schema=False)
    async def unified_session_status_batch(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.unified_session_status_batch(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/unified-session/status", include_in_schema=False)
    async def unified_session_status(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.unified_session_status(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/unified-session/start", include_in_schema=False)
    async def unified_session_start(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.unified_session_start(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/unified-session/{operation}", include_in_schema=False)
    async def unified_session_stop(
        operation: str,
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.unified_session_stop(actor, operation, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/permit", include_in_schema=False)
    async def issue_permit(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.issue_permit(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.get("/internal/fleet/persistent/route/{logical_agent_id}", include_in_schema=False)
    async def route(
        logical_agent_id: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.route(actor, logical_agent_id)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/materialize", include_in_schema=False)
    async def materialize(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.materialize(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/presence", include_in_schema=False)
    async def presence(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.presence(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/detach", include_in_schema=False)
    async def detach(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.detach(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-list", include_in_schema=False)
    async def obligation_list(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_list(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-inbox", include_in_schema=False)
    async def obligation_inbox(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_inbox(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-surface", include_in_schema=False)
    async def obligation_surface(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_surface(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-create", include_in_schema=False)
    async def obligation_create(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_create(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-delivery", include_in_schema=False)
    async def obligation_delivery(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_delivery(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/obligation-receipt", include_in_schema=False)
    async def obligation_receipt(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.obligation_receipt(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/drain", include_in_schema=False)
    async def drain(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.drain(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    @router.post("/internal/fleet/persistent/revoke", include_in_schema=False)
    async def revoke(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        actor = authenticate(x_terminal_mcp_peer, authorization)
        try:
            return await target.revoke(actor, payload)
        except MeshApplicationError as exc:
            raise HTTPException(status_code=_ERROR_STATUS[exc.kind], detail=exc.detail) from exc

    return router

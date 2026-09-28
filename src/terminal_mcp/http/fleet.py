from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from terminal_mcp.fleet.identity import SignedAgentIdentity


def build_fleet_router(replication) -> APIRouter:
    router = APIRouter()

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return peer

    @router.post("/internal/fleet/identities", include_in_schema=False)
    async def receive_identity(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        try:
            envelope = SignedAgentIdentity.from_dict(payload)
            status = await replication.receive(
                envelope,
                authenticated_peer_id=peer.instance_id,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "status": status}

    @router.post("/internal/fleet/session-finish", include_in_schema=False)
    async def finish_session(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        agent_id = payload.get("agent_id")
        ended_at = payload.get("ended_at")
        reason = payload.get("reason")
        if not all(isinstance(value, str) and value for value in (agent_id, ended_at, reason)):
            raise HTTPException(status_code=400, detail="finish payload is incomplete")
        try:
            changed = await replication.receive_finish(
                agent_id,
                ended_at,
                reason,
                authenticated_peer_id=peer.instance_id,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "changed": changed}

    @router.get("/internal/fleet/identities", include_in_schema=False)
    async def read_identities(
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
        agent_id: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        identities = await replication.list_identities(agent_id=agent_id, limit=limit)
        return {"ok": True, "identities": identities}

    return router

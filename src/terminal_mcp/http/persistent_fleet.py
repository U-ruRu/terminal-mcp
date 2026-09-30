from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.storage.persistent_agents import PersistentStoreError


def build_persistent_fleet_router(replication, bridge) -> APIRouter:
    router = APIRouter()

    def authenticate(peer_id: str, authorization: str):
        peer = replication.authenticate(peer_id, authorization)
        if peer is None:
            raise HTTPException(status_code=401, detail="invalid fleet peer")
        return peer

    @router.post("/internal/fleet/persistent/permit", include_in_schema=False)
    async def issue_permit(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        try:
            permit = await bridge.issue_permit(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                requesting_instance_id=peer.instance_id,
                scope=str(payload.get("scope") or ""),
                principal_id=str(payload.get("principal_id") or ""),
                operation=str(payload.get("operation") or payload.get("scope") or ""),
                request_id=str(payload.get("request_id") or "") or None,
            )
        except PersistentStoreError as exc:
            status = (
                404
                if exc.code in {"slot_not_found", "session_not_found", "authority_unavailable"}
                else 403
                if exc.code == "persistent_auth_required"
                else 409
            )
            raise HTTPException(status_code=status, detail=exc.code) from exc
        return {"ok": True, "permit": permit.as_dict()}

    @router.post("/internal/fleet/persistent/revoke", include_in_schema=False)
    async def revoke(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("authority_node_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="authority instance mismatch")
        try:
            blockers = await bridge.receive_revoke(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                reason=str(payload.get("reason") or "authority_revoke"),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": not blockers, "blockers": blockers}

    return router

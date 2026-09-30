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

    def raise_store_error(exc: PersistentStoreError):
        status = (
            404
            if exc.code
            in {
                "slot_not_found",
                "session_not_found",
                "authority_unavailable",
                "message_not_found",
            }
            else 403
            if exc.code == "persistent_auth_required"
            else 409
        )
        detail = {"code": exc.code}
        if exc.blockers:
            detail["blockers"] = exc.blockers
        raise HTTPException(status_code=status, detail=detail) from exc

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
            raise_store_error(exc)
        return {"ok": True, "permit": permit.as_dict()}

    @router.get(
        "/internal/fleet/persistent/route/{logical_agent_id}",
        include_in_schema=False,
    )
    async def route(
        logical_agent_id: str,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        authenticate(x_terminal_mcp_peer, authorization)
        value = await bridge.route_info(logical_agent_id)
        if value is None:
            raise HTTPException(status_code=404, detail="route not found")
        return {"ok": True, "route": value}

    @router.post("/internal/fleet/persistent/materialize", include_in_schema=False)
    async def materialize(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        try:
            result = await bridge.materialize_session(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                requesting_instance_id=peer.instance_id,
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, **result}

    @router.post("/internal/fleet/persistent/presence", include_in_schema=False)
    async def presence(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        try:
            result = await bridge.update_attachment_presence(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                requesting_instance_id=peer.instance_id,
                task_summary=str(payload.get("task_summary") or ""),
                intent=str(payload.get("intent") or ""),
                work_scope=list(payload.get("work_scope") or []),
                details=list(payload.get("details") or []),
                current_step=int(payload.get("current_step") or 1),
                intent_updated_at=payload.get("intent_updated_at"),
                last_activity_at=payload.get("last_activity_at"),
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, "presence": result}

    @router.post("/internal/fleet/persistent/detach", include_in_schema=False)
    async def detach(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        try:
            result = await bridge.detach_session(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                requesting_instance_id=peer.instance_id,
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, **result}

    @router.post(
        "/internal/fleet/persistent/obligation-delivery",
        include_in_schema=False,
    )
    async def obligation_delivery(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        try:
            item = await bridge.receive_obligation_delivery(
                payload,
                authenticated_home_node_id=peer.instance_id,
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, "obligation": item}

    @router.post(
        "/internal/fleet/persistent/obligation-receipt",
        include_in_schema=False,
    )
    async def obligation_receipt(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        try:
            result = await bridge.receive_obligation_receipt(
                message_ref=str(payload.get("message_ref") or ""),
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                attachment_node_id=peer.instance_id,
                seen_at=payload.get("seen_at"),
                read_at=payload.get("read_at"),
                replied_at=payload.get("replied_at"),
                reply_message_ref=payload.get("reply_message_ref"),
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, **result}

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

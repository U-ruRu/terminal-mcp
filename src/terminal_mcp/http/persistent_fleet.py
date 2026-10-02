from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from terminal_mcp.core.orchestration import parse_utc, utc_now
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    reset_admission_context,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError


def build_persistent_fleet_router(replication, bridge, backend=None) -> APIRouter:
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

    @router.post("/internal/fleet/persistent/access/resolve", include_in_schema=False)
    async def access_resolve(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        try:
            access = await bridge.resolve_access_code(str(payload.get("access_code") or ""))
        except (PersistentStoreError, ValueError) as exc:
            code = exc.code if isinstance(exc, PersistentStoreError) else "access_denied"
            return {"ok": False, "code": code}
        return {"ok": True, "access": access}

    @router.post("/internal/fleet/persistent/access/list", include_in_schema=False)
    async def access_list(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        return {"ok": True, "access": await bridge.access_authority.access_slots()}

    @router.post("/internal/fleet/persistent/access/by-name", include_in_schema=False)
    async def access_by_name(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        access = await bridge.access_authority.access_slot_by_public_name(
            str(payload.get("public_name") or "")
        )
        return {"ok": True, "access": access}

    @router.post("/internal/fleet/persistent/access/get", include_in_schema=False)
    async def access_get(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        access = await bridge.access_authority.access_slot(
            str(payload.get("logical_agent_id") or "")
        )
        return {"ok": True, "access": access}

    @router.post("/internal/fleet/persistent/access/display", include_in_schema=False)
    async def access_display(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        try:
            access = await bridge.access_authority.update_access_display_suffix(
                str(payload.get("logical_agent_id") or ""), payload.get("display_suffix")
            )
        except Exception:
            return {"ok": False, "code": "access_update_failed"}
        return {"ok": True, "access": access}

    @router.post("/internal/fleet/persistent/access/register", include_in_schema=False)
    async def access_register(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge.access_authority.reserve_access_codes(
                list(payload.get("forbidden_codes") or [])
            )
            slot = await bridge.access_authority.register_access_slot(
                logical_agent_id,
                str(payload.get("authority_node_id") or ""),
                slot_kind=str(payload.get("slot_kind") or "persistent"),
                display_suffix=payload.get("display_suffix"),
            )
            access = (
                await bridge.access_authority.issue_access_code(logical_agent_id)
                if int(slot["access_generation"]) == 0
                else slot
            )
        except Exception:
            return {"ok": False, "code": "access_registration_failed"}
        return {"ok": True, "access": {**slot, **access}}

    @router.post("/internal/fleet/persistent/access/rotate", include_in_schema=False)
    async def access_rotate(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge.access_authority.reserve_access_codes(
                list(payload.get("forbidden_codes") or [])
            )
            access = await bridge.access_authority.issue_access_code(logical_agent_id)
        except Exception:
            return {"ok": False, "code": "access_rotation_failed"}
        return {"ok": True, "access": access}

    @router.post("/internal/fleet/persistent/access/retire", include_in_schema=False)
    async def access_retire(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if (
            bridge.access_authority is None
            or bridge._access_control_node_id() != bridge.config.instance_id
        ):
            return {"ok": False, "code": "authority_unavailable"}
        try:
            access = await bridge.access_authority.retire_access_slot(
                str(payload.get("logical_agent_id") or "")
            )
        except Exception:
            return {"ok": False, "code": "access_retire_failed"}
        return {"ok": True, "access": access}

    def _forwarded_admission(payload: dict):
        raw = payload.get("forwarded_admission") or {}
        if not raw:
            return None
        return VerifiedAdmissionContext(
            principal_id=str(raw.get("principal_id") or ""),
            credential_id=str(raw.get("credential_id") or ""),
            scopes=frozenset(str(item) for item in (raw.get("scopes") or [])),
            auth_generation=int(raw.get("auth_generation") or 0),
            transport=str(raw.get("transport") or "fleet-forward"),
            auth_mode=str(raw.get("auth_mode") or "none"),
        )

    async def _unified_session_guard(payload: dict, peer):
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if backend is None:
            return None, {"ok": False, "code": "authority_unavailable"}
        try:
            access = await backend._resolve_access(str(payload.get("access_code") or ""))
        except PersistentStoreError as exc:
            return None, {"ok": False, "code": exc.code}
        if access["authority_node_id"] != bridge.config.instance_id:
            return None, {"ok": False, "code": "authority_unavailable"}
        return access, None

    @router.post("/internal/fleet/persistent/unified-session/status-name", include_in_schema=False)
    async def unified_session_status_name(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        if backend is None:
            return {"ok": False, "code": "authority_unavailable"}
        try:
            access = await backend._resolve_access_name(str(payload.get("public_name") or ""))
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code}
        if access["authority_node_id"] != bridge.config.instance_id:
            return {"ok": False, "code": "authority_unavailable"}
        token = None
        forwarded = _forwarded_admission(payload)
        if forwarded is not None:
            token = bind_admission_context(forwarded)
        try:
            session = await backend._local_access_session(access)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code}
        finally:
            if token is not None:
                reset_admission_context(token)
        return {
            "ok": True,
            "result": {
                **backend._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
            },
        }

    @router.post("/internal/fleet/persistent/unified-session/status", include_in_schema=False)
    async def unified_session_status(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        access, failure = await _unified_session_guard(payload, peer)
        if failure:
            return failure
        token = None
        forwarded = _forwarded_admission(payload)
        if forwarded is not None:
            token = bind_admission_context(forwarded)
        try:
            session = await backend._local_access_session(access, access_code_verified=True)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code}
        finally:
            if token is not None:
                reset_admission_context(token)
        result = {
            **backend._session_result(access, session),
            "logical_agent_id": access["logical_agent_id"],
            "work_session_id": session.work_session_id,
        }
        return {"ok": True, "result": result}

    @router.post("/internal/fleet/persistent/unified-session/start", include_in_schema=False)
    async def unified_session_start(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        access, failure = await _unified_session_guard(payload, peer)
        if failure:
            return failure
        token = None
        forwarded = _forwarded_admission(payload)
        if forwarded is not None:
            token = bind_admission_context(forwarded)
        try:
            result = await backend._local_session_start_resolved(access)
        except PersistentStoreError as exc:
            return {"ok": False, "code": exc.code}
        finally:
            if token is not None:
                reset_admission_context(token)
        return {"ok": True, "result": result}

    @router.post("/internal/fleet/persistent/unified-session/{operation}", include_in_schema=False)
    async def unified_session_stop(
        operation: str,
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        if operation not in {"end", "interrupt"}:
            raise HTTPException(status_code=404, detail="unknown operation")
        peer = authenticate(x_terminal_mcp_peer, authorization)
        _access, failure = await _unified_session_guard(payload, peer)
        if failure:
            return failure
        token = None
        forwarded = _forwarded_admission(payload)
        if forwarded is not None:
            token = bind_admission_context(forwarded)
        try:
            result = await backend.access_session_stop(
                str(payload.get("access_code") or ""), interrupt=operation == "interrupt"
            )
        finally:
            if token is not None:
                reset_admission_context(token)
        if not result.get("ok"):
            return {"ok": False, "code": result.get("code", "session_stop_failed")}
        return {"ok": True, "result": result}

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
                access_code=(
                    str(payload.get("access_code"))
                    if payload.get("access_code") is not None
                    else None
                ),
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
        "/internal/fleet/persistent/obligation-list",
        include_in_schema=False,
    )
    async def obligation_list(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge._guard_local_authority(logical_agent_id)
            session = await bridge.store.assert_session_authority(
                logical_agent_id,
                str(payload.get("work_session_id") or ""),
                int(payload.get("session_epoch") or 0),
            )
            if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                raise PersistentStoreError("session_not_active")
            obligations = await bridge.store.open_message_obligations(logical_agent_id)
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, "obligations": obligations}

    @router.post(
        "/internal/fleet/persistent/obligation-inbox",
        include_in_schema=False,
    )
    async def obligation_inbox(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge._guard_local_authority(logical_agent_id)
            session = await bridge.store.assert_session_authority(
                logical_agent_id,
                str(payload.get("work_session_id") or ""),
                int(payload.get("session_epoch") or 0),
            )
            if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                raise PersistentStoreError("session_not_active")
            messages = await bridge.store.message_inbox(
                logical_agent_id,
                recent_cutoff=payload.get("recent_cutoff"),
                show_all=bool(payload.get("show_all")),
                limit=int(payload.get("limit") or 50),
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, "messages": messages}

    @router.post(
        "/internal/fleet/persistent/obligation-surface",
        include_in_schema=False,
    )
    async def obligation_surface(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge._guard_local_authority(logical_agent_id)
            session = await bridge.store.assert_session_authority(
                logical_agent_id,
                str(payload.get("work_session_id") or ""),
                int(payload.get("session_epoch") or 0),
            )
            if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                raise PersistentStoreError("session_not_active")
            await bridge.store.surface_message_obligations(
                logical_agent_id,
                list(payload.get("message_refs") or []),
                peer.instance_id,
                retention_calls=int(payload.get("retention_calls") or 5),
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True}

    @router.post(
        "/internal/fleet/persistent/obligation-create",
        include_in_schema=False,
    )
    async def obligation_create(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("requesting_instance_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="requesting instance mismatch")
        logical_agent_id = str(payload.get("logical_agent_id") or "")
        try:
            await bridge._guard_local_authority(logical_agent_id)
            session = await bridge.store.active_session_for_slot(logical_agent_id)
            if (
                session is None
                or session.state != "active"
                or utc_now() >= parse_utc(session.hard_expires_at)
            ):
                raise PersistentStoreError("recipient_not_active")
            obligation = await bridge.create_obligation(
                logical_agent_id=logical_agent_id,
                sender_agent_id=str(payload.get("sender_agent_id") or ""),
                text=str(payload.get("text") or ""),
                require_reply=bool(payload.get("require_reply")),
                alert=bool(payload.get("alert")),
            )
        except PersistentStoreError as exc:
            raise_store_error(exc)
        return {"ok": True, "obligation": obligation}

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

    @router.post("/internal/fleet/persistent/drain", include_in_schema=False)
    async def drain(
        payload: dict,
        x_terminal_mcp_peer: str = Header(default=""),
        authorization: str = Header(default=""),
    ):
        peer = authenticate(x_terminal_mcp_peer, authorization)
        if payload.get("authority_node_id") != peer.instance_id:
            raise HTTPException(status_code=400, detail="authority instance mismatch")
        try:
            blockers = await bridge.receive_drain(
                logical_agent_id=str(payload.get("logical_agent_id") or ""),
                work_session_id=str(payload.get("work_session_id") or ""),
                session_epoch=int(payload.get("session_epoch") or 0),
                hard_expires_at=str(payload.get("hard_expires_at") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": not blockers, "blockers": blockers}

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

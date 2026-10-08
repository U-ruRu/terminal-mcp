"""Persistent Mesh use cases, independent of HTTP and its authentication headers.

Peer authentication belongs to an inbound adapter. Authority selection, forwarded
admission, fencing and durable messaging belong here and to the existing domain
services. The latter are compatibility repositories for Architecture A: no second
store or new commit boundary is introduced by this extraction.
"""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.session_gate import SessionGate
from terminal_mcp.core.orchestration import parse_utc, utc_now
from terminal_mcp.core.persistent_admission import (
    VerifiedAdmissionContext,
    bind_admission_context,
    current_admission_context,
    reset_admission_context,
)
from terminal_mcp.storage.persistent_agents import PersistentStoreError


class MeshApplicationError(Exception):
    """Transport-neutral rejection; adapters choose their own wire status."""

    def __init__(self, kind: str, detail: str | dict):
        self.kind = kind
        self.detail = detail
        super().__init__(str(detail))


class MeshApplication:
    """Canonical persistent Mesh capability over the existing authority bridge."""

    def __init__(self, bridge, backend=None, *, session_gate=None):
        self._bridge = bridge
        self._backend = backend
        self._session_gate = session_gate or SessionGate(backend=backend)

    @staticmethod
    def _effective_actor(actor: ActorContext) -> ActorContext:
        return ActorContext.from_admission(
            current_admission_context(),
            transport=actor.transport,
            node_id=actor.node_id,
            endpoint_role=actor.endpoint_role,
            contract_version=actor.contract_version,
            peer_node_id=actor.peer_node_id,
            request_id=actor.request_id,
            provider=actor.provider,
        )

    @staticmethod
    def _require_peer(actor: ActorContext) -> None:
        if actor.endpoint_role != "mesh" or not actor.peer_node_id:
            raise MeshApplicationError("unauthorized", "invalid fleet peer")

    @staticmethod
    def raise_store_error(exc: PersistentStoreError):
        kind = (
            "not_found"
            if exc.code
            in {
                "slot_not_found",
                "session_not_found",
                "authority_unavailable",
                "message_not_found",
            }
            else "forbidden"
            if exc.code == "persistent_auth_required"
            else "conflict"
        )
        detail = {"code": exc.code}
        if exc.blockers:
            detail["blockers"] = exc.blockers
        raise MeshApplicationError(kind, detail) from exc

    async def access_resolve(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                access = await self._bridge.resolve_access_code(
                    str(payload.get("access_code") or "")
                )
            except (PersistentStoreError, ValueError) as exc:
                code = exc.code if isinstance(exc, PersistentStoreError) else "access_denied"
                return {"ok": False, "code": code}
            return {"ok": True, "access": access}

    async def access_list(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            raw_limit = payload.get("limit")
            limit = max(1, int(raw_limit)) if raw_limit is not None else None
            offset = max(0, int(payload.get("offset") or 0))
            return {
                "ok": True,
                "access": await self._bridge.access_authority.access_slots(
                    limit=limit, offset=offset
                ),
            }

    async def access_by_name(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            access = await self._bridge.access_authority.access_slot_by_public_name(
                str(payload.get("public_name") or "")
            )
            return {"ok": True, "access": access}

    async def access_get(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            access = await self._bridge.access_authority.access_slot(
                str(payload.get("logical_agent_id") or "")
            )
            return {"ok": True, "access": access}

    async def provider_resolve(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                logical_agent_id = await self._bridge.resolve_provider_binding(
                    str(payload.get("provider") or ""),
                    str(payload.get("binding_key") or ""),
                )
            except (PersistentStoreError, ValueError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, PersistentStoreError)
                    else "identity_metadata_invalid"
                )
                return {"ok": False, "code": code}
            return {"ok": True, "logical_agent_id": logical_agent_id}

    async def provider_bind(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                bootstrap = payload.get("bootstrap") is True
                logical_agent_id = await self._bridge.bind_provider_binding(
                    str(payload.get("provider") or ""),
                    str(payload.get("binding_key") or ""),
                    str(payload.get("logical_agent_id") or ""),
                    access_code=(
                        str(payload.get("access_code"))
                        if payload.get("access_code") is not None
                        else None
                    ),
                    principal_id=(
                        str(payload.get("principal_id"))
                        if payload.get("principal_id") is not None
                        else None
                    ),
                    bootstrap=bootstrap,
                    bootstrap_authority_node_id=actor.peer_node_id if bootstrap else None,
                )
            except (PersistentStoreError, ValueError) as exc:
                code = (
                    exc.code
                    if isinstance(exc, PersistentStoreError)
                    else "identity_metadata_invalid"
                )
                return {"ok": False, "code": code}
            return {"ok": True, "logical_agent_id": logical_agent_id}

    async def access_display(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            try:
                access = await self._bridge.access_authority.update_access_display_suffix(
                    str(payload.get("logical_agent_id") or ""), payload.get("display_suffix")
                )
            except Exception:
                return {"ok": False, "code": "access_update_failed"}
            return {"ok": True, "access": access}

    async def access_register(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge.access_authority.reserve_access_codes(
                    list(payload.get("forbidden_codes") or [])
                )
                slot = await self._bridge.access_authority.register_access_slot(
                    logical_agent_id,
                    str(payload.get("authority_node_id") or ""),
                    slot_kind=str(payload.get("slot_kind") or "persistent"),
                    display_suffix=payload.get("display_suffix"),
                )
                access = (
                    await self._bridge.access_authority.issue_access_code(logical_agent_id)
                    if int(slot["access_generation"]) == 0
                    else slot
                )
            except Exception:
                return {"ok": False, "code": "access_registration_failed"}
            return {"ok": True, "access": {**slot, **access}}

    async def access_rotate(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge.access_authority.reserve_access_codes(
                    list(payload.get("forbidden_codes") or [])
                )
                access = await self._bridge.access_authority.issue_access_code(logical_agent_id)
            except Exception:
                return {"ok": False, "code": "access_rotation_failed"}
            return {"ok": True, "access": access}

    async def access_retire(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if (
                self._bridge.access_authority is None
                or self._bridge._access_control_node_id() != self._bridge.config.instance_id
            ):
                return {"ok": False, "code": "authority_unavailable"}
            try:
                access = await self._bridge.access_authority.retire_access_slot(
                    str(payload.get("logical_agent_id") or "")
                )
            except Exception:
                return {"ok": False, "code": "access_retire_failed"}
            return {"ok": True, "access": access}

    def _forwarded_admission(self, payload: dict):
        raw = payload.get("forwarded_admission") or {}
        if not raw:
            return None
        return VerifiedAdmissionContext(
            principal_id=str(raw.get("principal_id") or ""),
            credential_id=str(raw.get("credential_id") or ""),
            scopes=frozenset(str(item) for item in raw.get("scopes") or []),
            auth_generation=int(raw.get("auth_generation") or 0),
            transport=str(raw.get("transport") or "fleet-forward"),
            auth_mode=str(raw.get("auth_mode") or "none"),
        )

    async def _unified_session_guard(self, payload: dict, actor: ActorContext):
        if payload.get("requesting_instance_id") != actor.peer_node_id:
            raise MeshApplicationError("invalid_request", "requesting instance mismatch")
        if self._backend is None:
            return (None, {"ok": False, "code": "authority_unavailable"})
        try:
            access = await self._backend._resolve_access(str(payload.get("access_code") or ""))
        except PersistentStoreError as exc:
            return (None, {"ok": False, "code": exc.code})
        if access["authority_node_id"] != self._bridge.config.instance_id:
            return (None, {"ok": False, "code": "authority_unavailable"})
        return (access, None)

    async def unified_session_status_name(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if self._backend is None:
                return {"ok": False, "code": "authority_unavailable"}
            try:
                access = await self._backend._resolve_access_name(
                    str(payload.get("public_name") or "")
                )
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code}
            if access["authority_node_id"] != self._bridge.config.instance_id:
                return {"ok": False, "code": "authority_unavailable"}
            token = None
            forwarded = self._forwarded_admission(payload)
            if forwarded is not None:
                token = bind_admission_context(forwarded)
            try:
                session = await self._session_gate.peer_session(
                    self._effective_actor(actor), access
                )
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code}
            finally:
                if token is not None:
                    reset_admission_context(token)
            result = {
                **self._backend._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
                "authority_node_id": session.authority_node_id,
                "authority_epoch": session.authority_epoch,
            }
            last_seen = getattr(self._backend.lifecycle.store, "provider_last_seen", None)
            if callable(last_seen):
                result["last_active_at"] = await last_seen(access["logical_agent_id"])
            return {"ok": True, "result": result}

    async def unified_session_status_batch(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            if self._backend is None:
                return {"ok": False, "code": "authority_unavailable"}
            raw_access = payload.get("access")
            if not isinstance(raw_access, list) or len(raw_access) > 64:
                raise MeshApplicationError(
                    "invalid_request", "access batch must contain at most 64 items"
                )
            access = []
            for item in raw_access:
                if not isinstance(item, dict):
                    raise MeshApplicationError(
                        "invalid_request", "access batch items must be objects"
                    )
                logical_agent_id = str(item.get("logical_agent_id") or "")
                authority_node_id = str(item.get("authority_node_id") or "")
                if not logical_agent_id or authority_node_id != self._bridge.config.instance_id:
                    raise MeshApplicationError("invalid_request", "access batch authority mismatch")
                access.append(
                    {
                        "logical_agent_id": logical_agent_id,
                        "authority_node_id": authority_node_id,
                    }
                )
            token = None
            forwarded = self._forwarded_admission(payload)
            if forwarded is not None:
                token = bind_admission_context(forwarded)
            try:
                sessions = await self._backend.access_active_statuses(access)
            finally:
                if token is not None:
                    reset_admission_context(token)
            return {"ok": True, "result": {"sessions": sessions}}

    async def unified_session_status(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            access, failure = await self._unified_session_guard(payload, actor)
            if failure:
                return failure
            token = None
            forwarded = self._forwarded_admission(payload)
            if forwarded is not None:
                token = bind_admission_context(forwarded)
            try:
                session = await self._session_gate.peer_session(
                    self._effective_actor(actor), access, access_code_verified=True
                )
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code}
            finally:
                if token is not None:
                    reset_admission_context(token)
            result = {
                **self._backend._session_result(access, session),
                "logical_agent_id": access["logical_agent_id"],
                "work_session_id": session.work_session_id,
                "authority_node_id": session.authority_node_id,
                "authority_epoch": session.authority_epoch,
            }
            return {"ok": True, "result": result}

    async def unified_session_start(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            access, failure = await self._unified_session_guard(payload, actor)
            if failure:
                return failure
            token = None
            forwarded = self._forwarded_admission(payload)
            if forwarded is not None:
                token = bind_admission_context(forwarded)
            try:
                result = await self._session_gate.start_resolved(
                    self._effective_actor(actor), access
                )
            except PersistentStoreError as exc:
                return {"ok": False, "code": exc.code}
            finally:
                if token is not None:
                    reset_admission_context(token)
            return {"ok": True, "result": result}

    async def unified_session_managed(self, actor: ActorContext, payload: dict):
        """Home-authority managed operations from an authenticated Fleet peer.

        Forwarded provider evidence is resolved again at home. Peer authentication
        transports the original OAuth admission; it never grants mesh/operator role.
        """
        self._require_peer(actor)
        if payload.get("requesting_instance_id") != actor.peer_node_id:
            raise MeshApplicationError("invalid_request", "requesting instance mismatch")
        from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError
        from terminal_mcp.core.persistent_admission import PersistentAdmissionError

        try:
            admission = self._forwarded_admission(payload)
            if admission is None or admission.auth_mode != "oauth":
                raise ManagedSessionError("persistent_auth_required")
            # AuthFoundation models these external OAuth principals as their client id.
            client_id = admission.credential_id.removeprefix("oauth:")
            if not client_id or admission.principal_id != client_id:
                raise ManagedSessionError("persistent_auth_required")
            role = payload.get("endpoint_role")
            version = payload.get("contract_version")
            if role not in {"executor", "coordinator"} or type(version) is not int or version < 1:
                raise ManagedSessionError("capability_not_allowed")
            home = self._bridge.config.instance_id
            forwarded = ActorContext.from_admission(
                admission,
                node_id=home,
                endpoint_role=role,
                contract_version=version,
                provider=payload.get("provider"),
                provider_metadata=payload.get("provider_metadata"),
                logical_agent_id=payload.get("logical_agent_id"),
                authority_node_id=home,
            )
            # No implicit remote bootstrap: the globally bound identity must exist.
            resolved = await self._session_gate._resolve_provider_actor(forwarded)
            if resolved.authority_node_id != home or not resolved.logical_agent_id:
                raise ManagedSessionError("authority_unavailable")
            action = payload.get("action")
            with resolved.bind():
                if action == "start":
                    result = await self._session_gate.start(resolved, mode=None)
                elif action in {"end", "interrupt"}:
                    result = await self._session_gate.stop(
                        resolved, None, interrupt=action == "interrupt"
                    )
                elif action == "authorize":
                    operation = ManagedOperation(payload.get("managed_operation"))
                    if operation.value.startswith("operator."):
                        raise ManagedSessionError("capability_not_allowed")
                    resolution = await self._session_gate._managed_resolution(resolved, operation)
                    result = resolution.failure or resolution.identity
                else:
                    raise ManagedSessionError("operation_not_allowed")
            # The envelope signals successful RPC delivery. Application failures,
            # including lifecycle repair hints, retain their complete payload.
            return {"ok": True, "result": result}
        except (ManagedSessionError, PersistentStoreError, PersistentAdmissionError) as exc:
            return {"ok": True, "result": {"ok": False, "code": exc.code, "error": exc.code}}
        except (TypeError, ValueError) as exc:
            raise MeshApplicationError(
                "invalid_request", "invalid managed session request"
            ) from exc

    async def unified_session_stop(self, actor: ActorContext, operation: str, payload: dict):
        if operation == "managed":
            return await self.unified_session_managed(actor, payload)
        self._require_peer(actor)
        with actor.bind():
            if operation not in {"end", "interrupt"}:
                raise MeshApplicationError("not_found", "unknown operation")
            _access, failure = await self._unified_session_guard(payload, actor)
            if failure:
                return failure
            token = None
            forwarded = self._forwarded_admission(payload)
            if forwarded is not None:
                token = bind_admission_context(forwarded)
            try:
                result = await self._session_gate.stop(
                    self._effective_actor(actor),
                    str(payload.get("access_code") or ""),
                    interrupt=operation == "interrupt",
                    resolved_access=_access,
                )
            finally:
                if token is not None:
                    reset_admission_context(token)
            if not result.get("ok"):
                return {"ok": False, "code": result.get("code", "session_stop_failed")}
            return {"ok": True, "result": result}

    async def issue_permit(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                permit = await self._bridge.issue_permit(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    requesting_instance_id=actor.peer_node_id,
                    scope=str(payload.get("scope") or ""),
                    principal_id=str(payload.get("principal_id") or ""),
                    access_code=str(payload.get("access_code"))
                    if payload.get("access_code") is not None
                    else None,
                    operation=str(payload.get("operation") or payload.get("scope") or ""),
                    request_id=str(payload.get("request_id") or "") or None,
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "permit": permit.as_dict()}

    async def route(self, actor: ActorContext, logical_agent_id: str):
        self._require_peer(actor)
        with actor.bind():
            value = await self._bridge.route_info(logical_agent_id)
            if value is None:
                raise MeshApplicationError("not_found", "route not found")
            return {"ok": True, "route": value}

    async def materialize(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                result = await self._bridge.materialize_session(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    requesting_instance_id=actor.peer_node_id,
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, **result}

    async def presence(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                result = await self._bridge.update_attachment_presence(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    requesting_instance_id=actor.peer_node_id,
                    task_summary=str(payload.get("task_summary") or ""),
                    intent=str(payload.get("intent") or ""),
                    work_scope=list(payload.get("work_scope") or []),
                    details=list(payload.get("details") or []),
                    current_step=int(payload.get("current_step") or 1),
                    intent_updated_at=payload.get("intent_updated_at"),
                    last_activity_at=payload.get("last_activity_at"),
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "presence": result}

    async def detach(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            try:
                result = await self._bridge.detach_session(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    requesting_instance_id=actor.peer_node_id,
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, **result}

    async def obligation_list(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge._guard_local_authority(logical_agent_id)
                session = await self._bridge.store.assert_session_authority(
                    logical_agent_id,
                    str(payload.get("work_session_id") or ""),
                    int(payload.get("session_epoch") or 0),
                )
                if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                    raise PersistentStoreError("session_not_active")
                obligations = await self._bridge.store.open_message_obligations(logical_agent_id)
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "obligations": obligations}

    async def obligation_inbox(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge._guard_local_authority(logical_agent_id)
                session = await self._bridge.store.assert_session_authority(
                    logical_agent_id,
                    str(payload.get("work_session_id") or ""),
                    int(payload.get("session_epoch") or 0),
                )
                if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                    raise PersistentStoreError("session_not_active")
                messages = await self._bridge.store.message_inbox(
                    logical_agent_id,
                    recent_cutoff=payload.get("recent_cutoff"),
                    show_all=bool(payload.get("show_all")),
                    limit=int(payload.get("limit") or 50),
                    offset=max(0, int(payload.get("offset") or 0)),
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "messages": messages}

    async def obligation_surface(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge._guard_local_authority(logical_agent_id)
                session = await self._bridge.store.assert_session_authority(
                    logical_agent_id,
                    str(payload.get("work_session_id") or ""),
                    int(payload.get("session_epoch") or 0),
                )
                if session.state != "active" or utc_now() >= parse_utc(session.hard_expires_at):
                    raise PersistentStoreError("session_not_active")
                await self._bridge.store.surface_message_obligations(
                    logical_agent_id,
                    list(payload.get("message_refs") or []),
                    actor.peer_node_id,
                    retention_calls=int(payload.get("retention_calls") or 5),
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True}

    async def obligation_create(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            if payload.get("requesting_instance_id") != actor.peer_node_id:
                raise MeshApplicationError("invalid_request", "requesting instance mismatch")
            logical_agent_id = str(payload.get("logical_agent_id") or "")
            try:
                await self._bridge._guard_local_authority(logical_agent_id)
                session = await self._bridge.store.active_session_for_slot(logical_agent_id)
                if (
                    session is None
                    or session.state != "active"
                    or utc_now() >= parse_utc(session.hard_expires_at)
                ):
                    raise PersistentStoreError("recipient_not_active")
                obligation = await self._bridge.create_obligation(
                    logical_agent_id=logical_agent_id,
                    sender_agent_id=str(payload.get("sender_agent_id") or ""),
                    text=str(payload.get("text") or ""),
                    require_reply=bool(payload.get("require_reply")),
                    alert=bool(payload.get("alert")),
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "obligation": obligation}

    async def obligation_delivery(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            try:
                item = await self._bridge.receive_obligation_delivery(
                    payload, authenticated_home_node_id=actor.peer_node_id
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, "obligation": item}

    async def obligation_receipt(self, actor: ActorContext, payload: dict):
        self._require_peer(actor)
        with actor.bind():
            try:
                result = await self._bridge.receive_obligation_receipt(
                    message_ref=str(payload.get("message_ref") or ""),
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    attachment_node_id=actor.peer_node_id,
                    seen_at=payload.get("seen_at"),
                    read_at=payload.get("read_at"),
                    replied_at=payload.get("replied_at"),
                    reply_message_ref=payload.get("reply_message_ref"),
                )
            except PersistentStoreError as exc:
                self.raise_store_error(exc)
            return {"ok": True, **result}

    async def _verify_session_authority(self, actor: ActorContext, payload: dict) -> None:
        self._require_peer(actor)
        agent, session, epoch = (
            payload.get(key) for key in ("logical_agent_id", "work_session_id", "session_epoch")
        )
        if (
            not isinstance(agent, str)
            or not agent
            or not isinstance(session, str)
            or not session
            or type(epoch) is not int
            or epoch < 1
        ):
            raise MeshApplicationError("invalid_request", "provide an exact session identity")
        if payload.get("authority_node_id") != actor.peer_node_id:
            raise MeshApplicationError("invalid_request", "authority instance mismatch")
        route = await self._bridge.route_info(agent)
        if route is None:
            raise MeshApplicationError(
                "authority_unavailable", "session authority route unavailable"
            )
        if route.get("authority_node_id") != actor.peer_node_id:
            raise MeshApplicationError("wrong_authority", "peer is not the session authority")
        if route.get("state") == "recovery_required":
            raise MeshApplicationError("recovery_required", "authority route needs recovery")
        supplied = payload.get("authority_epoch")
        # Previous protocol peers omit the epoch; actual home identity is always checked.
        if supplied is not None and (
            type(supplied) is not int or supplied < 1 or supplied != route.get("authority_epoch")
        ):
            raise MeshApplicationError("wrong_authority", "stale authority epoch")

    async def drain(self, actor: ActorContext, payload: dict):
        await self._verify_session_authority(actor, payload)
        with actor.bind():
            try:
                blockers = await self._bridge.receive_drain(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    hard_expires_at=str(payload.get("hard_expires_at") or ""),
                )
            except (TypeError, ValueError) as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            return {"ok": not blockers, "blockers": blockers}

    async def revoke(self, actor: ActorContext, payload: dict):
        await self._verify_session_authority(actor, payload)
        with actor.bind():
            try:
                blockers = await self._bridge.receive_revoke(
                    logical_agent_id=str(payload.get("logical_agent_id") or ""),
                    work_session_id=str(payload.get("work_session_id") or ""),
                    session_epoch=int(payload.get("session_epoch") or 0),
                    reason=str(payload.get("reason") or "authority_revoke"),
                )
            except (TypeError, ValueError) as exc:
                raise MeshApplicationError("invalid_request", str(exc)) from exc
            return {"ok": not blockers, "blockers": blockers}

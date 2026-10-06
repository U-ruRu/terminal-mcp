"""Shared authority-backed Session Gate and command coordination policy.

Authority resolution remains behind the existing persistent compatibility port;
local and Mesh identities are never guessed from agent-supplied identifiers.
This gate is independent of MCP/HTTP and can be called by future role adapters.
"""

from __future__ import annotations

from dataclasses import dataclass

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedOperation, ManagedSessionError
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleError
from terminal_mcp.storage.persistent_agents import PersistentStoreError


def failure(code: str) -> dict:
    return {"ok": False, "code": code, "error": code}


@dataclass(frozen=True)
class SessionResolution:
    actor: ActorContext
    identity: dict | None = None
    failure: dict | None = None
    managed: bool = False


class SessionGate:
    def __init__(
        self, service=None, *, backend=None, managed_identity=None, managed_sessions=None
    ):
        self.service = service
        self._explicit_backend = backend
        self.managed_identity = managed_identity
        self.managed_sessions = managed_sessions

    @property
    def backend(self):
        if self._explicit_backend is not None:
            return self._explicit_backend
        return getattr(self.service, "persistent", None)

    @staticmethod
    def _managed_failure(exc: ManagedSessionError) -> dict:
        result = {"ok": False, "code": exc.code, "error": exc.code}
        if exc.return_to_chat:
            result["return_to_chat"] = True
        return result

    def _has_provider_identity(self, actor: ActorContext) -> bool:
        return bool(
            self.managed_identity is not None
            and self.managed_sessions is not None
            and actor.provider
            and actor.provider_metadata
        )

    async def _resolve_provider_actor(self, actor: ActorContext) -> ActorContext:
        return await self.managed_identity.resolve(
            actor, actor.provider, actor.provider_metadata
        )

    async def _bind_provider_from_access_code(
        self, actor: ActorContext, code: str
    ) -> ActorContext | None:
        backend = self.backend
        if backend is None or not self._has_provider_identity(actor):
            return None
        with actor.bind():
            access = await backend._resolve_access(code)
        if access.get("slot_kind") != "persistent":
            return None
        return await self.managed_identity.bind_existing(
            actor,
            actor.provider,
            actor.provider_metadata,
            str(access["logical_agent_id"]),
        )

    async def _managed_resolution(
        self, actor: ActorContext, operation: ManagedOperation
    ) -> SessionResolution | None:
        if not self._has_provider_identity(actor):
            return None
        try:
            resolved = await self._resolve_provider_actor(actor)
            admitted = await self.managed_sessions.authorize_operation(resolved, operation)
        except ManagedSessionError as exc:
            return SessionResolution(actor, failure=self._managed_failure(exc), managed=True)
        session = admitted.snapshot.session
        return SessionResolution(
            admitted.actor,
            identity={
                "ok": True,
                "logical_agent_id": session.logical_agent_id,
                "work_session_id": session.work_session_id,
                "session_epoch": session.session_epoch,
                "authority_node_id": session.authority_node_id,
                "public_name": admitted.public_name,
                "hard_expires_at": session.hard_expires_at,
            },
            managed=True,
        )

    async def resolve(
        self, actor: ActorContext, code: str | None, operation: ManagedOperation | None = None
    ) -> SessionResolution:
        if operation is not None:
            managed = await self._managed_resolution(actor, operation)
            if managed is not None and managed.failure is not None and code and (
                managed.failure.get("code") == "identity_not_bound"
            ):
                try:
                    bound = await self._bind_provider_from_access_code(actor, code)
                except (ManagedSessionError, PersistentStoreError) as exc:
                    code_value = getattr(exc, "code", "identity_binding_failed")
                    return SessionResolution(actor, failure=failure(code_value))
                if bound is not None:
                    managed = await self._managed_resolution(bound, operation)
            if managed is not None:
                # A compatibility code remains an independent authorization path
                # while AuthFoundation grants are being migrated. Without a code,
                # the managed authorization result is authoritative.
                if managed.failure is None or not code:
                    return managed
        backend = self.backend
        if backend is None:
            return SessionResolution(actor, failure=failure("policy_incompatible"))
        if not code:
            return SessionResolution(actor, failure=failure("access_code_required"))
        with actor.bind():
            identity = await backend.access_identity(code)
        if not identity.get("ok"):
            return SessionResolution(actor, failure=identity)
        authority = identity.get("authority_node_id") or getattr(
            getattr(backend, "lifecycle", None),
            "authority_node_id",
            actor.node_id or None,
        )
        resolved_actor = actor.with_identity({**identity, "authority_node_id": authority})
        return SessionResolution(resolved_actor, identity=identity)

    async def identity(
        self, actor: ActorContext, code: str | None, operation: ManagedOperation | None = None
    ):
        result = await self.resolve(actor, code, operation)
        return result.identity, result.failure

    async def command_state(self, actor: ActorContext, identity: dict, action: str):
        try:
            with actor.with_identity(identity).bind():
                state = await self.backend.message_state(
                    logical_agent_id=identity["logical_agent_id"],
                    work_session_id=identity["work_session_id"],
                    session_epoch=identity["session_epoch"],
                    surface=True,
                )
        except Exception as exc:
            return None, {"ok": False, "code": "message_state_unavailable", "error": str(exc)}
        if state.get("alert_pending") and action != "cancel":
            return None, {
                "ok": False,
                "code": "coordination_alert",
                "error": "coordination_alert: reply to the pending alert before continuing",
                **state,
            }
        if state.get("ack_required_pending") and action == "run":
            return None, {
                "ok": False,
                "code": "coordination_ack_required",
                "error": "coordination_ack_required: acknowledge the pending message before run",
                **state,
            }
        return state, None

    async def resolve_message_actor(
        self,
        actor: ActorContext,
        sender: str,
        code: str | None,
        operation: ManagedOperation | None = None,
    ) -> SessionResolution:
        if operation is not None:
            managed = await self._managed_resolution(actor, operation)
            if managed is not None and managed.failure is not None and code and (
                managed.failure.get("code") == "identity_not_bound"
            ):
                try:
                    bound = await self._bind_provider_from_access_code(actor, code)
                except (ManagedSessionError, PersistentStoreError) as exc:
                    code_value = getattr(exc, "code", "identity_binding_failed")
                    return SessionResolution(actor, failure=failure(code_value))
                if bound is not None:
                    managed = await self._managed_resolution(bound, operation)
            if managed is not None and (managed.failure is None or code is None):
                return managed
        if code is not None:
            return await self.resolve(actor, code)
        if self.backend is None:
            return SessionResolution(actor, failure=failure("policy_incompatible"))
        with actor.bind():
            identity = await self.backend.access_sender_identity(sender)
        if not identity.get("ok"):
            return SessionResolution(actor, failure=identity)
        return SessionResolution(actor.with_identity(identity), identity=identity)

    async def message(self, resolution: SessionResolution, sender: str, **kwargs) -> dict:
        if resolution.failure is not None:
            return resolution.failure
        if resolution.identity is None:
            return failure("session_not_found")
        effective_sender = (
            str(resolution.identity["public_name"])
            if resolution.managed and resolution.identity is not None
            else sender
        )
        with resolution.actor.bind():
            return await self.backend.access_message(
                effective_sender,
                _resolved_identity=resolution.identity,
                **kwargs,
            )

    async def surface_message_page(
        self,
        resolution: SessionResolution,
        sender: str,
        **kwargs,
    ) -> dict:
        if resolution.failure is not None:
            return resolution.failure
        effective_sender = (
            str(resolution.identity["public_name"])
            if resolution.managed and resolution.identity is not None
            else sender
        )
        with resolution.actor.bind():
            return await self.backend.surface_message_page(
                effective_sender,
                _resolved_identity=resolution.identity,
                **kwargs,
            )

    async def start(
        self,
        actor: ActorContext,
        *,
        mode: str | None,
        code: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        backend = self.backend
        if backend is None:
            return failure("policy_incompatible")
        if self._has_provider_identity(actor):
            try:
                resolved = await self._resolve_provider_actor(actor)
            except ManagedSessionError as exc:
                if exc.code != "identity_not_bound":
                    return self._managed_failure(exc)
                resolved = None
                if mode == "persistent" and code:
                    try:
                        resolved = await self._bind_provider_from_access_code(actor, code)
                    except (ManagedSessionError, PersistentStoreError) as bind_exc:
                        code_value = getattr(bind_exc, "code", "identity_binding_failed")
                        return failure(code_value)
            if resolved is not None:
                try:
                    started = await self.managed_sessions.start(resolved)
                except ManagedSessionError as exc:
                    if not code:
                        return self._managed_failure(exc)
                else:
                    receipt = started.receipt()
                    return {
                        "ok": True,
                        "mode": "persistent",
                        "public_name": receipt["public_name"],
                        "session_ref": receipt["work_session_id"],
                        "session_epoch": receipt["session_epoch"],
                        "session_state": receipt["state"],
                        "hard_expires_at": receipt["hard_expires_at"],
                        "remaining_seconds": receipt["remaining_seconds"],
                        "role": receipt["role"],
                        "contract_version": receipt["contract_version"],
                    }
        if mode is None:
            return failure("mode_required")
        if mode not in {"persistent", "legacy"}:
            return failure("invalid_mode")
        if mode == "legacy" and code is not None:
            return failure("legacy_code_not_allowed")
        if mode == "persistent" and not code:
            return failure("access_code_required")
        try:
            with actor.bind():
                access = await backend._resolve_access(code) if mode == "persistent" else None
                return await backend.access_session_start(
                    mode=mode,
                    access_code=code,
                    display_name=display_name,
                    _resolved_access=access,
                )
        except PersistentStoreError as exc:
            return failure(exc.code)
        except PersistentLifecycleError as exc:
            return backend._error(exc)

    async def start_resolved(self, actor: ActorContext, access: dict) -> dict:
        """Authenticated Mesh start after its requesting-node/authority check."""
        with actor.bind():
            return await self.backend._local_session_start_resolved(access)

    async def peer_session(self, actor: ActorContext, access: dict, *, access_code_verified=False):
        """Canonical peer session check; the domain still enforces leases/epochs."""
        with actor.bind():
            return await self.backend._local_access_session(
                access,
                access_code_verified=access_code_verified,
            )

    async def stop(
        self,
        actor: ActorContext,
        code: str | None,
        *,
        interrupt=False,
        resolved_access: dict | None = None,
    ) -> dict:
        if self._has_provider_identity(actor):
            try:
                resolved = await self._resolve_provider_actor(actor)
                ended = await self.managed_sessions.end(
                    resolved, reason="session_interrupt" if interrupt else "session_end"
                )
            except ManagedSessionError as exc:
                if exc.code != "identity_not_bound":
                    return self._managed_failure(exc)
            else:
                return {
                    "ok": True,
                    "mode": "persistent",
                    "public_name": ended.get("public_name"),
                    "session_ref": ended.get("work_session_id"),
                    "stopping": ended.get("session_state") == "stopping",
                }
        backend = self.backend
        if backend is None:
            return failure("policy_incompatible")
        if not code:
            return failure("access_code_required")
        try:
            with actor.bind():
                access = (
                    resolved_access
                    if resolved_access is not None
                    else await backend._resolve_access(code)
                )
                if access["authority_node_id"] != backend.lifecycle.authority_node_id:
                    return await backend.access_session_stop(
                        code,
                        interrupt=interrupt,
                        _resolved_access=access,
                    )
                async with backend.lifecycle.operation_guard(access["logical_agent_id"]):
                    session = await backend._local_access_session(
                        access,
                        allow_stopping=True,
                        access_code_verified=True,
                    )
                    resolved_actor = actor.with_identity(
                        {
                            "logical_agent_id": access["logical_agent_id"],
                            "work_session_id": session.work_session_id,
                            "session_epoch": session.session_epoch,
                            "authority_node_id": access["authority_node_id"],
                        }
                    )
                    with resolved_actor.bind():
                        return await backend._stop_resolved_session(
                            access, session, interrupt=interrupt
                        )
        except PersistentStoreError as exc:
            return failure(exc.code)
        except PersistentLifecycleError as exc:
            return backend._error(exc)

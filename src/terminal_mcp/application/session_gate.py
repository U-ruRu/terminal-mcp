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
    def __init__(self, service=None, *, backend=None, managed_identity=None, managed_sessions=None):
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
        return await self.managed_identity.resolve(actor, actor.provider, actor.provider_metadata)

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
            access_code=code,
        )

    async def _legacy_compatibility_allowed(
        self, actor: ActorContext, managed: SessionResolution
    ) -> bool:
        failure_payload = managed.failure or {}
        code = failure_payload.get("code")
        if code == "identity_not_bound":
            return True
        if code not in {"access_denied", "persistent_auth_required"}:
            return False
        try:
            resolved = await self._resolve_provider_actor(actor)
        except ManagedSessionError:
            return False
        logical_agent_id = resolved.logical_agent_id
        if not logical_agent_id:
            return False
        return not await self.managed_sessions.has_managed_window(logical_agent_id)

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
                "session_lifecycle": {
                    "state": admitted.lifecycle.phase.value,
                    "remaining_seconds": admitted.lifecycle.remaining_seconds,
                    "hard_expires_at": session.hard_expires_at,
                    "return_to_chat": admitted.lifecycle.return_to_chat,
                },
            },
            managed=True,
        )

    async def resolve(
        self, actor: ActorContext, code: str | None, operation: ManagedOperation | None = None
    ) -> SessionResolution:
        if operation is not None:
            managed = await self._managed_resolution(actor, operation)
            if (
                managed is not None
                and managed.failure is not None
                and code
                and (managed.failure.get("code") == "identity_not_bound")
            ):
                try:
                    bound = await self._bind_provider_from_access_code(actor, code)
                except (ManagedSessionError, PersistentStoreError) as exc:
                    code_value = getattr(exc, "code", "identity_binding_failed")
                    return SessionResolution(actor, failure=failure(code_value))
                if bound is not None:
                    managed = await self._managed_resolution(bound, operation)
            if managed is not None:
                if managed.failure is None or not code:
                    return managed
                if not await self._legacy_compatibility_allowed(actor, managed):
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

    async def provider_identity(
        self, actor: ActorContext, operation: ManagedOperation
    ) -> SessionResolution:
        """Resolve provider-bound identity for reads that do not require a live WorkSession."""
        if not self._has_provider_identity(actor):
            return SessionResolution(actor, failure=failure("identity_not_bound"), managed=True)
        try:
            resolved = await self._resolve_provider_actor(actor)
            grant = await self.managed_sessions._authorize(resolved, operation)
        except ManagedSessionError as exc:
            return SessionResolution(actor, failure=self._managed_failure(exc), managed=True)
        identity = {
            "ok": True,
            "logical_agent_id": grant.logical_agent_id,
            "authority_node_id": grant.authority_node_id,
            "public_name": grant.public_name,
        }
        return SessionResolution(resolved, identity=identity, managed=True)

    async def touch_provider(self, actor: ActorContext) -> None:
        """Best-effort activity touch after a successful public MCP call."""
        if not self._has_provider_identity(actor):
            return
        try:
            await self._resolve_provider_actor(actor)
        except ManagedSessionError:
            return

    async def current_state(self, actor: ActorContext) -> dict:
        """Return compact server-resolved managed identity/window/session state."""
        if not self._has_provider_identity(actor):
            return failure("identity_not_bound")
        try:
            resolved = await self._resolve_provider_actor(actor)
            admitted = await self.managed_sessions.authorize_operation(
                resolved, ManagedOperation.OBSERVE
            )
        except ManagedSessionError as exc:
            return self._managed_failure(exc)
        snapshot = admitted.snapshot
        window = snapshot.window
        session = snapshot.session
        binding = snapshot.binding
        return {
            "ok": True,
            "logical_agent": {
                "logical_agent_id": session.logical_agent_id,
                "public_name": admitted.public_name,
                "authority_node_id": session.authority_node_id,
            },
            "work_window": {
                "work_window_id": window.work_window_id,
                "state": admitted.lifecycle.phase.value,
                "opened_at": window.opened_at.isoformat(),
                "hard_expires_at": window.hard_expires_at.isoformat(),
                "remaining_seconds": admitted.lifecycle.remaining_seconds,
                "revision": window.window_revision,
            },
            "work_session": {
                "work_session_id": session.work_session_id,
                "session_epoch": session.session_epoch,
                "state": session.state.value,
                "started_at": session.started_at,
                "hard_expires_at": session.hard_expires_at,
                "role": binding.role,
                "contract_version": binding.contract_version,
            },
        }

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
            if (
                managed is not None
                and managed.failure is not None
                and code
                and (managed.failure.get("code") == "identity_not_bound")
            ):
                try:
                    bound = await self._bind_provider_from_access_code(actor, code)
                except (ManagedSessionError, PersistentStoreError) as exc:
                    code_value = getattr(exc, "code", "identity_binding_failed")
                    return SessionResolution(actor, failure=failure(code_value))
                if bound is not None:
                    managed = await self._managed_resolution(bound, operation)
            if managed is not None:
                if managed.failure is None or code is None:
                    return managed
                if not await self._legacy_compatibility_allowed(actor, managed):
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
            str(resolution.identity.get("public_name") or sender)
            if resolution.identity is not None
            else sender
        )
        with resolution.actor.bind():
            result = await self.backend.access_message(
                effective_sender,
                _resolved_identity=resolution.identity,
                **kwargs,
            )
        if result.get("ok") and resolution.identity is not None:
            lifecycle = resolution.identity.get("session_lifecycle")
            if isinstance(lifecycle, dict):
                result["session_lifecycle"] = lifecycle
        return result

    async def surface_message_page(
        self,
        resolution: SessionResolution,
        sender: str,
        **kwargs,
    ) -> dict:
        if resolution.failure is not None:
            return resolution.failure
        effective_sender = (
            str(resolution.identity.get("public_name") or sender)
            if resolution.identity is not None
            else sender
        )
        with resolution.actor.bind():
            return await self.backend.surface_message_page(
                effective_sender,
                _resolved_identity=resolution.identity,
                **kwargs,
            )

    async def _bootstrap_provider_actor(self, actor: ActorContext) -> ActorContext:
        if actor.endpoint_role not in {"executor", "coordinator"}:
            raise ManagedSessionError("identity_not_bound")
        admission = actor.admission()
        if admission is None or not actor.principal_id:
            raise ManagedSessionError("persistent_auth_required")
        admission.require("terminal:execute")
        backend = self.backend
        if backend is None or self.managed_identity is None or self.managed_sessions is None:
            raise ManagedSessionError("policy_incompatible")
        display_name = f"{actor.provider or 'provider'} {actor.endpoint_role}"
        with actor.bind():
            created = await backend.slot_create(display_name)
        if not created.get("ok"):
            raise ManagedSessionError(str(created.get("code") or "identity_binding_failed"))
        slot = created.get("slot") or {}
        access = created.get("access") or {}
        logical_agent_id = str(slot.get("logical_agent_id") or "")
        access_code = access.get("access_code")
        if not logical_agent_id or not isinstance(access_code, str):
            raise ManagedSessionError("identity_binding_failed")
        bound = await self.managed_identity.bind_existing(
            actor,
            actor.provider or "",
            actor.provider_metadata,
            logical_agent_id,
            access_code=access_code,
        )
        await self.managed_sessions.ensure_agent_grant(bound, logical_agent_id)
        return bound

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
            bootstrapped = False
            try:
                resolved = await self._resolve_provider_actor(actor)
            except ManagedSessionError as exc:
                if exc.code != "identity_not_bound":
                    return self._managed_failure(exc)
                resolved = None
                if actor.endpoint_role in {"executor", "coordinator"} and code is None:
                    try:
                        resolved = await self._bootstrap_provider_actor(actor)
                        bootstrapped = True
                    except (ManagedSessionError, PersistentStoreError) as bind_exc:
                        code_value = getattr(bind_exc, "code", "identity_binding_failed")
                        return failure(code_value)
                elif mode == "persistent" and code:
                    try:
                        resolved = await self._bind_provider_from_access_code(actor, code)
                    except (ManagedSessionError, PersistentStoreError) as bind_exc:
                        code_value = getattr(bind_exc, "code", "identity_binding_failed")
                        return failure(code_value)
            if resolved is not None:
                try:
                    if (
                        actor.endpoint_role in {"executor", "coordinator"}
                        and code is None
                        and not bootstrapped
                        and resolved.logical_agent_id
                    ):
                        await self.managed_sessions.ensure_agent_grant(
                            resolved, resolved.logical_agent_id
                        )
                    started = await self.managed_sessions.start(resolved)
                except ManagedSessionError as exc:
                    if not code:
                        return self._managed_failure(exc)
                    managed_failure = SessionResolution(
                        resolved, failure=self._managed_failure(exc), managed=True
                    )
                    if not await self._legacy_compatibility_allowed(resolved, managed_failure):
                        return managed_failure.failure
                else:
                    receipt = started.receipt()
                    return {
                        "ok": True,
                        "mode": "persistent",
                        "public_name": receipt["public_name"],
                        "session_ref": receipt["work_session_id"],
                        "work_session_id": receipt["work_session_id"],
                        "session_epoch": receipt["session_epoch"],
                        "session_state": receipt["state"],
                        "state": receipt["state"],
                        "started_at": receipt["started_at"],
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
        if code is None and self._has_provider_identity(actor):
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

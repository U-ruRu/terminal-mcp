"""Shared authority-backed Session Gate and command coordination policy.

Authority resolution remains behind the existing persistent compatibility port;
local and Mesh identities are never guessed from agent-supplied identifiers.
This gate is independent of MCP/HTTP and can be called by future role adapters.
"""

from __future__ import annotations

from dataclasses import dataclass

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.persistent_lifecycle import PersistentLifecycleError
from terminal_mcp.storage.persistent_agents import PersistentStoreError


def failure(code: str) -> dict:
    return {"ok": False, "code": code, "error": code}


@dataclass(frozen=True)
class SessionResolution:
    actor: ActorContext
    identity: dict | None = None
    failure: dict | None = None


class SessionGate:
    def __init__(self, service=None, *, backend=None):
        self.service = service
        self._explicit_backend = backend

    @property
    def backend(self):
        if self._explicit_backend is not None:
            return self._explicit_backend
        return getattr(self.service, "persistent", None)

    async def resolve(self, actor: ActorContext, code: str | None) -> SessionResolution:
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

    async def identity(self, actor: ActorContext, code: str | None):
        result = await self.resolve(actor, code)
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
    ) -> SessionResolution:
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
        with resolution.actor.bind():
            return await self.backend.access_message(
                sender,
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
        with resolution.actor.bind():
            return await self.backend.surface_message_page(
                sender,
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

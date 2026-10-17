"""Current-namespace Access attachment gate shared by every agent capability."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.managed_sessions import ManagedOperation


def failure(code: str) -> dict:
    return {"ok": False, "code": code, "error": code}


@dataclass(frozen=True)
class SessionResolution:
    actor: ActorContext
    identity: dict | None = None
    failure: dict | None = None
    managed: bool = True
    identity_unbound: bool = False


class SessionGate:
    def __init__(self, service=None, *, backend=None, managed_identity=None, managed_sessions=None):
        self.service = service
        self._explicit_backend = backend

    @property
    def access_mesh(self):
        return getattr(self.service, "access_mesh", None)

    @property
    def backend(self):
        return self._explicit_backend or getattr(self.service, "persistent", None)

    async def _mesh_resolution(self, actor, operation):
        try:
            identity = await self.access_mesh.resolve(actor, operation)
        except AccessMeshError as exc:
            return SessionResolution(actor, failure=failure(exc.code), managed=True)
        identity["node_attachment_id"] = self.access_mesh.connection_key(actor)
        resolved = (
            actor.with_identity(identity)
            if identity.get("work_session_id")
            else actor.with_agent(identity["logical_agent_id"], identity["authority_node_id"])
        )
        resolved = replace(
            resolved,
            issuer_node_id=identity["issuer_node_id"],
            slot_id=identity["slot_id"],
            node_attachment_id=identity["node_attachment_id"],
        )
        identity.pop("node_attachment_id", None)
        return SessionResolution(resolved, identity=identity, managed=True)

    async def resolve(self, actor: ActorContext, code: str | None = None,
                      operation: ManagedOperation | None = None) -> SessionResolution:
        if self.access_mesh is None:
            return SessionResolution(actor, failure=failure("session_attach_required"))
        if code is not None or actor.endpoint_role not in {"executor", "coordinator"}:
            return SessionResolution(actor, failure=failure("session_attach_required"))
        return await self._mesh_resolution(actor, operation or ManagedOperation.SESSION_STATUS)

    async def identity(self, actor: ActorContext, code: str | None = None,
                       operation: ManagedOperation | None = None):
        result = await self.resolve(actor, code, operation)
        return result.identity, result.failure

    async def provider_identity(self, actor: ActorContext,
                                operation: ManagedOperation) -> SessionResolution:
        return await self.resolve(actor, None, operation)

    async def touch_provider(self, actor: ActorContext) -> None:
        if self.access_mesh is None or actor.endpoint_role not in {"executor", "coordinator"}:
            return
        try:
            async with asyncio.timeout(0.5):
                await asyncio.to_thread(self.access_mesh.store.touch,
                                        self.access_mesh.connection_key(actor))
        except Exception:
            logging.getLogger(__name__).warning("Access activity refresh deferred")

    async def current_state(self, actor: ActorContext) -> dict:
        return await self.access_mesh.observe(actor)

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
        for mode, pending_key, code, required_action in (
            ("alert", "alert_pending", "coordination_alert", "reply"),
            ("ack", "ack_required_pending", "coordination_ack_required", "ack"),
        ):
            if not state.get(pending_key):
                continue
            pending = [
                message
                for message in state.get("pending_messages", [])
                if (message.get("mode") == mode or mode == "alert" and bool(message.get("alert")))
            ]
            if not pending:
                pending = list(state.get("pending_messages") or [])
            if (
                mode == "alert"
                and pending
                and all(message.get("sender") == "terminal-mcp-system" for message in pending)
            ):
                required_action = "ack"
            return None, {
                "ok": False,
                "code": code,
                "error": code,
                "details": {
                    "pending_messages": pending[:4],
                    "ack_required_pending": bool(state.get("ack_required_pending")),
                    "alert_pending": bool(state.get("alert_pending")),
                    "required_action": required_action,
                },
            }
        return state, None

    async def resolve_message_actor(self, actor: ActorContext, sender: str,
                                    code: str | None,
                                    operation: ManagedOperation | None = None) -> SessionResolution:
        return await self.resolve(actor, code, operation)

    async def message(self, resolution: SessionResolution, sender: str, **kwargs) -> dict:
        if resolution.failure is not None:
            return resolution.failure
        kwargs.pop("access_code", None)
        if "show_all" in kwargs:
            kwargs["history"] = kwargs.pop("show_all")
        with resolution.actor.bind():
            return await self.service.access_mesh_messages.message(resolution.actor, **kwargs)

    async def surface_message_page(self, resolution: SessionResolution, sender: str,
                                   **kwargs) -> dict:
        if resolution.failure is not None:
            return resolution.failure
        with resolution.actor.bind():
            return await self.backend.surface_message_page(
                sender, _resolved_identity=resolution.identity, **kwargs)

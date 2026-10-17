"""The same Access attachment lifecycle on every node."""

from typing import Literal

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.core.access_mesh_grants import AccessMeshError


class SessionApplication(ApplicationCapability):
    @application_operation("sessions")
    async def session(self, actor: ActorContext, action: Literal["attach", "intent"] = "attach",
                      session_number: str | None = None, intent: str | None = None) -> dict:
        mesh = self.gate.access_mesh
        if mesh is None or actor.endpoint_role not in {"executor", "coordinator"}:
            raise AccessMeshError("session_attach_required")
        if action == "intent":
            if session_number is not None or not intent or not intent.strip():
                raise AccessMeshError("intent_required")
            return await mesh.update_intent(actor, intent)
        if action != "attach" or session_number is None:
            raise AccessMeshError("session_attach_required")
        result = await mesh.attach(actor, session_number=session_number)
        if result.get("ok") and intent is not None:
            await mesh.update_intent(actor, intent, initial_only=True)
        return result

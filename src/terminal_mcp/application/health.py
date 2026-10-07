"""Canonical health application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.core.managed_sessions import ManagedOperation


class HealthApplication(ApplicationCapability):
    @application_operation("health")
    async def health(self, actor: ActorContext) -> dict:
        if actor.endpoint_role == "coordinator":
            resolution = await self.gate.resolve(actor, None, ManagedOperation.HEALTH)
            if resolution.failure is not None:
                return resolution.failure
        return await self.service.health(self.auth_mode, agent_id=None)

"""Canonical health application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation


class HealthApplication(ApplicationCapability):
    @application_operation("health")
    async def health(self, actor: ActorContext) -> dict:
        return await self.service.health(self.auth_mode, agent_id=None)

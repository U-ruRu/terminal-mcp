"""The application and MCP share one native local messaging boundary."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation


class MessagingApplication(ApplicationCapability):
    @application_operation("messages")
    async def message(self, actor: ActorContext, **kwargs) -> dict:
        messages = getattr(self.service, "access_mesh_messages", None)
        if messages is None:
            raise RuntimeError("local messaging must be composed with Access")
        return await messages.message(actor, **kwargs)

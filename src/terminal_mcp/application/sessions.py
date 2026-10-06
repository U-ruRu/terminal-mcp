"""Canonical sessions application capability (transport independent)."""

from typing import Annotated, Literal

from pydantic import Field

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation


class SessionApplication(ApplicationCapability):
    @application_operation("sessions")
    async def session(
        self,
        actor: ActorContext,
        action: Literal["start", "end", "interrupt"],
        mode: Literal["persistent", "legacy"] | None = None,
        code: Annotated[str | None, Field(min_length=4, max_length=4, pattern="^[0-9]{4}$")] = None,
        display_name: Annotated[str | None, Field(max_length=80)] = None,
    ) -> dict:
        if action == "start":
            return await self.gate.start(actor, mode=mode, code=code, display_name=display_name)
        return await self.gate.stop(actor, code, interrupt=action == "interrupt")

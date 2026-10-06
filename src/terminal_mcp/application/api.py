"""Canonical transport-independent Terminal MCP Application API."""

from __future__ import annotations

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy
from terminal_mcp.application.commands import CommandApplication
from terminal_mcp.application.compatibility import ApplicationCompatibility
from terminal_mcp.application.contexts import ContextApplication
from terminal_mcp.application.fleet_control import FleetControlApplication
from terminal_mcp.application.health import HealthApplication
from terminal_mcp.application.messages import MessagingApplication
from terminal_mcp.application.observations import ObservationApplication
from terminal_mcp.application.ports import ApplicationUnitOfWorkPort
from terminal_mcp.application.replication import (
    FleetProjectionApplication,
    FleetSourceApplication,
    ReplicationApplication,
)
from terminal_mcp.application.requests import CmdRequest, ContextRequest
from terminal_mcp.application.session_gate import SessionGate
from terminal_mcp.application.sessions import SessionApplication
from terminal_mcp.application.task_requests import TaskRequest
from terminal_mcp.application.tasks import TaskApplication


class TerminalApplication:
    def __init__(
        self,
        service,
        *,
        auth_mode="none",
        policy_controller=None,
        unit_of_work: ApplicationUnitOfWorkPort | None = None,
        replication=None,
        fleet_source=None,
        fleet_projection=None,
        projection_service=None,
        fleet_control=None,
        managed_identity=None,
        managed_sessions=None,
    ):
        self.service = service
        self.auth_mode = auth_mode
        self.policy_controller = policy_controller
        self.unit_of_work = unit_of_work
        self.policy = CapabilityPolicy()
        self.session_gate = SessionGate(service)
        options = {"auth_mode": auth_mode, "policy": self.policy, "unit_of_work": unit_of_work}
        self.sessions = SessionApplication(service, self.session_gate, **options)
        self.observations = ObservationApplication(service, self.session_gate, **options)
        self.messages = MessagingApplication(service, self.session_gate, **options)
        self.tasks = TaskApplication(service, self.session_gate, **options)
        self.commands = CommandApplication(service, self.session_gate, **options)
        self.contexts = ContextApplication(service, self.session_gate, **options)
        self.health_capability = HealthApplication(service, self.session_gate, **options)
        self.compatibility = ApplicationCompatibility(service)
        self.legacy = self.compatibility
        self.replication = ReplicationApplication(replication) if replication else None
        self.fleet_source = FleetSourceApplication(fleet_source) if fleet_source else None
        self.fleet_projection = (
            FleetProjectionApplication(fleet_projection, projection_service)
            if fleet_projection
            else None
        )
        self.fleet_control = FleetControlApplication(fleet_control) if fleet_control else None
        # Managed identity/session capabilities are composed by the host but stay
        # transport-inactive until the explicit endpoint cutover.
        self.managed_identity = managed_identity
        self.managed_sessions = managed_sessions
        self._operator = None
        self._mesh = None

    @property
    def operator(self):
        if self._operator is None:
            from terminal_mcp.application.operator import OperatorApplication

            self._operator = OperatorApplication(
                self.service, self.policy_controller, self.managed_sessions
            )
        return self._operator

    @property
    def mesh(self):
        if self._mesh is None:
            from terminal_mcp.application.mesh import MeshApplication

            backend = getattr(self.service, "persistent", None)
            bridge = getattr(backend, "fleet_bridge", None)
            self._mesh = MeshApplication(bridge, backend, session_gate=self.session_gate)
        return self._mesh

    async def session(self, actor: ActorContext, **kwargs) -> dict:
        return await self.sessions.session(actor, **kwargs)

    async def observe(self, actor: ActorContext, **kwargs) -> dict:
        return await self.observations.observe(actor, **kwargs)

    async def message(self, actor: ActorContext, **kwargs) -> dict:
        return await self.messages.message(actor, **kwargs)

    async def task(self, actor: ActorContext, request: TaskRequest) -> dict:
        return await self.tasks.task(actor, request)

    async def cmd(self, actor: ActorContext, request: CmdRequest) -> dict:
        return await self.commands.cmd(actor, request)

    async def context(self, actor: ActorContext, request: ContextRequest) -> dict:
        return await self.contexts.context(actor, request)

    async def health(self, actor: ActorContext) -> dict:
        return await self.health_capability.health(actor)


def get_application(service, *, auth_mode=None) -> TerminalApplication:
    """Compatibility composition for callers that still pass TerminalService."""
    if isinstance(service, TerminalApplication):
        return service
    current = getattr(service, "application", None)
    if isinstance(current, TerminalApplication):
        return current
    application = TerminalApplication(service, auth_mode=auth_mode or "none")
    service.application = application
    return application

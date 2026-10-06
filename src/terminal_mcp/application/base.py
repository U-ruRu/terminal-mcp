"""Common capability invocation and explicit server-owned endpoint policy."""

from __future__ import annotations

from functools import wraps

from terminal_mcp.application.actor import ActorContext

CAPABILITIES = frozenset(
    {
        "sessions",
        "observations",
        "messages",
        "tasks",
        "commands",
        "contexts",
        "health",
    }
)
# Role endpoints are introduced in a later phase; the compatibility endpoint
# deliberately retains all existing capabilities. No request field can change
# this role: the inbound adapter chooses it from server endpoint configuration.
ROLE_CAPABILITIES = {
    "legacy": CAPABILITIES,
    "internal": CAPABILITIES,
    "operator": CAPABILITIES,
    "mesh": CAPABILITIES,
    "executor": CAPABILITIES,
    "coordinator": CAPABILITIES - {"commands"},
}


class CapabilityPolicy:
    def allows(self, actor: ActorContext, capability: str) -> bool:
        return capability in ROLE_CAPABILITIES.get(actor.endpoint_role, frozenset())


def application_operation(capability: str):
    """Bind explicit actor identity for the duration of one domain invocation."""
    if capability not in CAPABILITIES:
        raise ValueError("unknown application capability")

    def decorate(function):
        @wraps(function)
        async def invoke(self, actor: ActorContext, *args, **kwargs):
            if not self.policy.allows(actor, capability):
                return {
                    "ok": False,
                    "code": "capability_not_allowed",
                    "error": "capability_not_allowed",
                }
            with actor.bind():
                return await function(self, actor, *args, **kwargs)

        return invoke

    return decorate


class ApplicationCapability:
    def __init__(self, service, gate, *, auth_mode="none", policy=None, unit_of_work=None):
        self.unit_of_work = unit_of_work
        self.service = service
        self.gate = gate
        self.auth_mode = auth_mode
        self.policy = policy or CapabilityPolicy()

    @property
    def backend(self):
        return getattr(self.service, "persistent", None)

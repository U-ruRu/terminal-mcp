"""Common capability invocation and explicit server-owned endpoint policy."""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.public_errors import normalize_public_error, public_error

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



def canonical_application_result(result):
    """Normalize returned domain failures while preserving transaction exceptions."""
    if isinstance(result, Mapping) and result.get("ok") is False:
        return normalize_public_error(result).as_dict()
    return result


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
                return public_error("capability_not_allowed").as_dict()
            with actor.bind():
                result = await function(self, actor, *args, **kwargs)
                return canonical_application_result(result)

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

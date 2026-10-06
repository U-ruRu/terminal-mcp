"""Explicit legacy application facade during the public-surface migration.

Legacy routes retain their published request/response contracts. They invoke
these existing domain use cases with the same server-resolved actor rather than
acquiring direct access to storage or resolving identities in a transport.
"""

from __future__ import annotations

from typing import Literal

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import CapabilityPolicy, canonical_application_result

LegacyOperation = Literal[
    "agent_start",
    "coordinate",
    "message",
    "agents",
    "agent_finish",
    "context",
    "tasks",
    "task",
    "run",
    "recovery",
    "read",
    "cancel",
    "health",
    "console_snapshot",
]
LEGACY_OPERATIONS: frozenset[str] = frozenset(
    {
        "agent_start",
        "coordinate",
        "message",
        "agents",
        "agent_finish",
        "context",
        "tasks",
        "task",
        "run",
        "recovery",
        "read",
        "cancel",
        "health",
        "console_snapshot",
    }
)


class ApplicationCompatibility:
    def __init__(self, service):
        self._service = service

    async def call(self, actor: ActorContext, operation: LegacyOperation, *args, **kwargs):
        if operation not in LEGACY_OPERATIONS:
            raise ValueError("unknown legacy application operation")
        capability = {
            "agent_start": "sessions",
            "agent_finish": "sessions",
            "coordinate": "sessions",
            "agents": "observations",
            "console_snapshot": "observations",
            "run": "commands",
            "read": "commands",
            "cancel": "commands",
            "recovery": "commands",
            "task": "tasks",
            "tasks": "tasks",
            "context": "contexts",
            "message": "messages",
            "health": "health",
        }[operation]
        if not CapabilityPolicy().allows(actor, capability):
            from terminal_mcp.core.public_errors import public_error

            return public_error("capability_not_allowed").as_dict()
        with actor.bind():
            application = getattr(self._service, "application", None)
            action = args[0] if args else kwargs.get("action")
            if operation == "context" and action in {"create", "update", "delete"} and application:
                data = {
                    key: value
                    for key, value in kwargs.items()
                    if key
                    in {
                        "context_id",
                        "summary",
                        "content",
                        "primary",
                        "show_details",
                        "limit",
                        "offset",
                    }
                }
                result = await application.contexts.mutate(actor, action, **data)
                return canonical_application_result(result)
            result = await getattr(self._service, operation)(*args, **kwargs)
            return canonical_application_result(result)


LegacyApplication = ApplicationCompatibility

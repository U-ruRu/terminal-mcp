"""Canonical tasks application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import _with_session_lifecycle
from terminal_mcp.application.task_requests import TaskRequest, task_request_to_backend
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.public_errors import public_error


class TaskApplication(ApplicationCapability):
    @application_operation("tasks")
    async def task(self, actor: ActorContext, request: TaskRequest) -> dict:
        code, namespace, task_id, backend_request = task_request_to_backend(request)
        action = backend_request.pop("action")
        if not self.policy.allows_task_action(actor, action):
            return public_error("capability_not_allowed").as_dict()
        identity, failure = await self.gate.identity(
            actor, code, ManagedOperation(f"task.{action}")
        )
        if failure is not None:
            return failure
        if (
            getattr(self.gate, "command_state", None) is not None
            and getattr(self.backend, "message_state", None) is not None
        ):
            _state, message_failure = await self.gate.command_state(actor, identity, action)
            if message_failure is not None:
                return _with_session_lifecycle(message_failure, identity)
        result = await self.backend.task(
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=code,
            action=action,
            namespace=namespace,
            task_id=task_id,
            idempotency_key=None,
            # Native Mesh cleanup owns the current deadline and release policy.
            # The older window lease would pin a superseded deadline and could
            # release intentionally retained persistent ownership.
            session_scoped_claim=(
                isinstance(identity.get("session_lifecycle"), dict)
                and not (
                    getattr(self.service, "access_mesh", None) is not None
                    and identity.get("issuer_node_id")
                    and identity.get("slot_id")
                )
            ),
            **backend_request,
        )
        return _with_session_lifecycle(result, identity)

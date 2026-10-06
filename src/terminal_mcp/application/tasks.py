"""Canonical tasks application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.task_requests import TaskRequest, task_request_to_backend


class TaskApplication(ApplicationCapability):
    @application_operation("tasks")
    async def task(self, actor: ActorContext, request: TaskRequest) -> dict:
        code, namespace, task_id, backend_request = task_request_to_backend(request)
        action = backend_request.pop("action")
        identity, failure = await self.gate.identity(actor, code)
        if failure is not None:
            return failure
        return await self.backend.task(
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=code,
            action=action,
            namespace=namespace,
            task_id=task_id,
            **backend_request,
        )

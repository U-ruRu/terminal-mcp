"""Canonical tasks application capability (transport independent)."""

import hashlib
import json

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
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
        replay_key = None
        if actor.transport == "mcp" and actor.request_id:
            replay_key = f"{identity['work_session_id']}:{actor.request_id}"
            if actor.request_id == "0":
                # Stateless connector requests reuse zero. Scope semantic task replays
                # by the normalized operation so distinct mutations cannot collide.
                payload = {
                    "action": action,
                    "namespace": namespace,
                    "task_id": task_id,
                    "request": backend_request,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(
                        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
                replay_key += f":{fingerprint}"
        result = await self.backend.task(
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=code,
            action=action,
            namespace=namespace,
            task_id=task_id,
            idempotency_key=replay_key,
            session_scoped_claim=isinstance(identity.get("session_lifecycle"), dict),
            **backend_request,
        )
        lifecycle = identity.get("session_lifecycle")
        if result.get("ok") and isinstance(lifecycle, dict):
            result["session_lifecycle"] = lifecycle
        return result

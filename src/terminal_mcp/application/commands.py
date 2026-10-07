"""Canonical commands application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import _finish_cmd_read_page, _read_error
from terminal_mcp.application.requests import CmdRequest
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.read_contract import (
    DEFAULT_CMD_READ_LINES,
    InvalidCursor,
    decode_cursor,
)


def _with_session_lifecycle(result: dict, identity: dict) -> dict:
    lifecycle = identity.get("session_lifecycle")
    if result.get("ok") and isinstance(lifecycle, dict):
        result["session_lifecycle"] = lifecycle
    return result


class CommandApplication(ApplicationCapability):
    @application_operation("commands")
    async def cmd(self, actor: ActorContext, request: CmdRequest) -> dict:
        if (
            request.action == "read"
            and request.code is None
            and not actor.provider_metadata
            and actor.endpoint_role in {"legacy", "internal"}
        ):
            scope = {
                "kind": "cmd.read",
                "cmd_hash": request.cmd_hash,
                "authorization": "anonymous",
            }
            try:
                start = decode_cursor(request.cursor, scope)
            except InvalidCursor as exc:
                return _read_error("invalid_cursor", str(exc))
            result = await self.service.read(
                cmd_hash=request.cmd_hash,
                lines_count=request.limit,
                offset=start,
                agent_id=None,
            )
            return _finish_cmd_read_page(result, start=start, scope=scope)
        identity, failure = await self.gate.identity(
            actor, request.code, ManagedOperation(f"command.{request.action}")
        )
        if failure is not None:
            return failure
        backend = self.backend
        message_state, failure = await self.gate.command_state(actor, identity, request.action)
        if failure is not None:
            return failure
        if request.action == "read":
            scope = {
                "kind": "cmd.read",
                "cmd_hash": request.cmd_hash,
                "authorization": identity["logical_agent_id"],
            }
            try:
                start = decode_cursor(request.cursor, scope)
            except InvalidCursor as exc:
                return _read_error("invalid_cursor", str(exc))
            result = await self.service.read(
                cmd_hash=request.cmd_hash,
                lines_count=request.limit,
                offset=start,
                agent_id=None,
            )
            result.update(message_state)
            result.update(
                {
                    "logical_agent_id": identity["logical_agent_id"],
                    "work_session_id": identity["work_session_id"],
                    "session_epoch": identity["session_epoch"],
                }
            )
            return _with_session_lifecycle(
                _finish_cmd_read_page(result, start=start, scope=scope), identity
            )
        if request.action == "run":
            result = await backend.run(
                request.command,
                logical_agent_id=identity["logical_agent_id"],
                work_session_id=identity["work_session_id"],
                session_epoch=identity["session_epoch"],
                access_code=request.code,
                queue_id=request.queue_id,
                task_scope=request.task_scope,
            )
            if result.get("ok") and result.get("status") in {"completed", "failed"}:
                scope = {
                    "kind": "cmd.read",
                    "cmd_hash": result["cmd_hash"],
                    "authorization": identity["logical_agent_id"],
                }
                output = await self.service.read(
                    cmd_hash=result["cmd_hash"],
                    lines_count=DEFAULT_CMD_READ_LINES,
                    offset=0,
                    agent_id=None,
                )
                output = _finish_cmd_read_page(output, start=0, scope=scope)
                for key in (
                    "lines",
                    "overall_lines_count",
                    "displayed_lines_count",
                    "next_cursor",
                    "has_more",
                    "output_truncated",
                    "output_retained",
                    "output_pruned_at",
                    "output_bytes",
                    "line_truncated",
                ):
                    if key in output:
                        result[key] = output[key]
            result.update(message_state)
            return _with_session_lifecycle(result, identity)
        if request.action == "cancel":
            result = await backend.cancel(
                request.cmd_hash,
                logical_agent_id=identity["logical_agent_id"],
                work_session_id=identity["work_session_id"],
                session_epoch=identity["session_epoch"],
                access_code=request.code,
            )
            result.update(message_state)
            return _with_session_lifecycle(result, identity)
        result = await backend.recovery(
            request.command,
            logical_agent_id=identity["logical_agent_id"],
            work_session_id=identity["work_session_id"],
            session_epoch=identity["session_epoch"],
            access_code=request.code,
        )
        result["public_name"] = identity["public_name"]
        result["session_ref"] = identity["session_ref"]
        result.update(message_state)
        return _with_session_lifecycle(result, identity)

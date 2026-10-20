"""Canonical commands application capability (transport independent)."""

import asyncio

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import (
    _finish_cmd_read_page,
    _read_error,
    _with_session_lifecycle,
)
from terminal_mcp.application.requests import CmdRequest
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.read_contract import (
    InvalidCursor,
    bounded_page,
    decode_cursor,
    encode_cursor,
)


class CommandApplication(ApplicationCapability):
    @application_operation("commands")
    async def journal(
        self, actor: ActorContext, *, limit: int = 20, cursor: str | None = None
    ) -> dict:
        mesh = getattr(self.gate, "access_mesh", None)
        if mesh is None:
            return _read_error("capability_not_allowed", "Local mesh journal is disabled")
        if actor.provider_metadata and getattr(self.gate, "command_state", None):
            try:
                resolution = await self.gate.resolve(actor, None, ManagedOperation.MESSAGE_READ)
                identity = resolution.identity
                if identity and identity.get("work_session_id"):
                    _state, pending = await self.gate.command_state(actor, identity, "read")
                    if pending is not None:
                        return pending
            except Exception:
                pass
        # Bind the direction here, not in the shared command-output cursor codec.
        # Old ascending journal cursors must not be reinterpreted as backward anchors.
        scope = {
            "kind": "command.journal",
            "node_id": mesh.store.local_node_id,
            "order": "sequence_desc",
        }
        try:
            before = decode_cursor(cursor, scope) if cursor else None
            rows = await asyncio.to_thread(
                mesh.store.command_journal, before=before, limit=limit + 1
            )
            data = [{key: value for key, value in row.items() if key != "sequence"} for row in rows]
            page, _ = bounded_page(data, limit=limit, cursor=None, scope=scope)
        except InvalidCursor as exc:
            return _read_error("invalid_cursor", str(exc))
        more = len(page) < len(rows)
        next_cursor = (
            encode_cursor(rows[len(page) - 1]["sequence"], scope) if more and page else None
        )
        return {
            "ok": True,
            "action": "journal",
            "node_id": mesh.store.local_node_id,
            "commands": page,
            "next_cursor": next_cursor,
        }

    async def _launch(self, actor: ActorContext, identity: dict, request: CmdRequest) -> dict:
        options = {
            "logical_agent_id": identity["logical_agent_id"],
            "work_session_id": identity["work_session_id"],
            "session_epoch": identity["session_epoch"],
        }
        if request.action == "run":
            return await self.backend.run(
                request.command, queue_id=request.queue_id, task_scope=request.task_scope, **options
            )
        return await self.backend.recovery(request.command, **options)

    @application_operation("commands")
    async def cmd(self, actor: ActorContext, request: CmdRequest) -> dict:
        read_message_state = {}
        if (
            request.action == "read"
            and actor.provider_metadata
            and (getattr(self.gate, "command_state", None))
        ):
            try:
                attached = await self.gate.resolve(actor, None, ManagedOperation.MESSAGE_READ)
                if attached.identity and attached.identity.get("work_session_id"):
                    read_message_state, pending = await self.gate.command_state(
                        actor, attached.identity, "read"
                    )
                    if pending is not None:
                        return pending
            except Exception:
                pass
        if (
            request.action == "read"
            and (
                getattr(self.gate, "access_mesh", None) is not None
                or (not actor.provider_metadata and actor.endpoint_role == "internal")
            )
        ):
            scope = {
                "kind": "cmd.read",
                "cmd_hash": request.cmd_hash,
                "authorization": "anonymous",
            }
            if getattr(self.gate, "access_mesh", None) is not None:
                scope["node_id"] = getattr(self.gate, "access_mesh", None).store.local_node_id
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
            result.update(read_message_state or {})
            return _finish_cmd_read_page(result, start=start, scope=scope)
        if request.action == "read" and actor.provider_metadata:
            resolution = await self.gate.provider_identity(actor, ManagedOperation.COMMAND_READ)
            identity, failure = resolution.identity, resolution.failure
        else:
            identity, failure = await self.gate.identity(
                actor, None, ManagedOperation(f"command.{request.action}")
            )
        if failure is not None:
            return failure
        backend = self.backend
        if request.action == "read" and actor.provider_metadata:
            message_state = read_message_state or {}
        else:
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
                    "work_session_id": identity.get("work_session_id"),
                    "session_epoch": identity.get("session_epoch"),
                    "public_name": identity.get("public_name"),
                    "authority_node_id": identity.get("authority_node_id"),
                }
            )
            return _with_session_lifecycle(
                _finish_cmd_read_page(result, start=start, scope=scope), identity
            )
        if request.action == "run":
            result = await self._launch(actor, identity, request)
            result.update(message_state)
            return _with_session_lifecycle(result, identity)
        if request.action == "cancel":
            result = await backend.cancel(
                request.cmd_hash,
                logical_agent_id=identity["logical_agent_id"],
                work_session_id=identity["work_session_id"],
                session_epoch=identity["session_epoch"],
            )
            result.update(message_state)
            return _with_session_lifecycle(result, identity)
        result = await self._launch(actor, identity, request)
        result["public_name"] = identity["public_name"]
        result["session_ref"] = identity["work_session_id"]
        result.update(message_state)
        return _with_session_lifecycle(result, identity)

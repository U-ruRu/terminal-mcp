"""Canonical contexts application capability (transport independent)."""

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import _read_error
from terminal_mcp.application.requests import ContextRequest
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.orchestration import utc_now, utc_text
from terminal_mcp.core.read_contract import (
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
    decode_cursor,
    encode_cursor,
)
from terminal_mcp.core.service import validate_context_request


class ContextApplication(ApplicationCapability):
    @application_operation("contexts")
    async def context(self, actor: ActorContext, request: ContextRequest) -> dict:
        if request.action == "list":
            scope = {"kind": "context.list", "detail": request.detail}
            try:
                raw = await self.service.context(
                    "list",
                    show_details=request.detail == "full",
                    limit=request.limit + 1,
                    offset=decode_cursor(request.cursor, scope),
                )
                if not raw.get("ok"):
                    return raw
                rows = [
                    {**item, "_primary": True}
                    for item in sorted(raw.get("primary") or [], key=lambda value: value["id"])
                ] + [
                    {**item, "_primary": False}
                    for item in sorted(raw.get("additional") or [], key=lambda value: value["id"])
                ]
                page, _unused_cursor = bounded_page(
                    rows,
                    limit=request.limit,
                    cursor=None,
                    scope=scope,
                )
                start = decode_cursor(request.cursor, scope)
                next_cursor = (
                    encode_cursor(start + len(page), scope) if len(page) < len(rows) else None
                )
            except InvalidCursor as exc:
                return _read_error("invalid_cursor", str(exc))
            except OutputItemTooLarge as exc:
                return _read_error("output_item_too_large", str(exc))
            primary = []
            additional = []
            for item in page:
                is_primary = item.pop("_primary")
                (primary if is_primary else additional).append(item)
            return {
                "ok": True,
                "primary": primary,
                "additional": additional,
                "next_cursor": next_cursor,
            }
        data = request.model_dump(exclude={"action", "code"}, exclude_none=True)
        resolution = await self.gate.resolve(
            actor, request.code, ManagedOperation.CONTEXT_WRITE
        )
        if resolution.failure is not None:
            return resolution.failure
        return await self.mutate(resolution.actor, request.action, **data)

    @application_operation("contexts")
    async def mutate(
        self,
        actor: ActorContext,
        action: str,
        *,
        context_id=None,
        summary=None,
        content=None,
        primary=None,
        show_details=False,
        limit=None,
        offset=0,
    ) -> dict:
        """One local transaction owns context data and resolved actor activity.

        The Access caller passes an authority-resolved actor; the legacy HTTP
        facade retains its existing admission rules but shares these semantics.
        No remote authority call or independent legacy-store commit runs inside
        the transaction. Caller cancellation must not leave a partial mutation.
        """
        data = {
            "context_id": context_id,
            "summary": summary,
            "content": content,
            "primary": primary,
            "show_details": show_details,
            "limit": limit,
            "offset": offset,
        }
        if action not in {"create", "update", "delete"}:
            return {"ok": False, "code": "invalid_request"}
        if self.unit_of_work is None:
            return await self.service.context(action, **data)
        if not getattr(self.service, "context_store", None):
            return {"ok": False, "code": "service_unavailable"}
        invalid = validate_context_request(action, **data)
        if invalid:
            return {"ok": False, "code": "invalid_request"}
        try:
            async with self.unit_of_work.transaction() as repositories:
                if action == "create":
                    entry = await repositories.context.create(summary, content, primary)
                    result = {"ok": True, "entry": entry}
                elif action == "update":
                    patch = {
                        key: value
                        for key, value in data.items()
                        if key in {"summary", "content", "primary"} and value is not None
                    }
                    entry = await repositories.context.update(context_id, **patch)
                    result = (
                        {"ok": True, "entry": entry}
                        if entry is not None
                        else {"ok": False, "code": "resource_not_found"}
                    )
                else:
                    deleted = await repositories.context.delete(context_id)
                    result = (
                        {"ok": True, "deleted_id": int(context_id)}
                        if deleted
                        else {"ok": False, "code": "resource_not_found"}
                    )
                if result.get("ok") and actor.logical_agent_id is not None:
                    await repositories.sessions.activity(
                        actor.logical_agent_id,
                        f"context.{action}",
                        utc_text(utc_now()),
                    )
            return result
        except (TypeError, ValueError):
            return {"ok": False, "code": "invalid_request"}

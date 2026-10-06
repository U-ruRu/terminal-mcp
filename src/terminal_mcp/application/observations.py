"""Canonical observations application capability (transport independent)."""

from typing import Annotated, Literal

from pydantic import Field

from terminal_mcp.api_models import TaskLane, TaskOperationalStatus, TaskState
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import _read_error, _task_summary
from terminal_mcp.core.read_contract import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
    decode_cursor,
    encode_cursor,
)


class ObservationApplication(ApplicationCapability):
    @application_operation("observations")
    async def observe(
        self,
        actor: ActorContext,
        subject: Literal["sessions", "tasks", "namespaces"] = "sessions",
        namespace: str | None = None,
        task_id: str | None = None,
        lane: TaskLane | None = None,
        state: TaskState | None = None,
        operational_status: TaskOperationalStatus | None = None,
        tags: list[str] | None = None,
        detail: Literal["summary", "full"] = "summary",
        show_done: bool = False,
        show_archived: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> dict:
        backend = self.backend
        filters = {
            "namespace": namespace,
            "task_id": task_id,
            "lane": lane,
            "state": state,
            "operational_status": operational_status,
            "tags": tags or [],
            "show_done": show_done,
            "show_archived": show_archived,
        }
        scope = {"kind": "observe", "subject": subject, "filters": filters, "detail": detail}
        try:
            offset = decode_cursor(cursor, scope)
        except InvalidCursor as exc:
            return _read_error("invalid_cursor", str(exc))

        if subject == "sessions":
            if backend is None:
                return {
                    "ok": False,
                    "code": "policy_incompatible",
                    "error": "policy_incompatible",
                }
            result = await backend.access_observe_slots(
                limit=limit + 1,
                offset=offset,
            )
            if not result.get("ok"):
                return result
            rows = list(result.get("sessions") or [])
            if detail == "summary":
                rows = [
                    {
                        "public_name": item.get("public_name"),
                        "mode": item.get("mode"),
                        "authority_node_id": item.get("authority_node_id"),
                        "session_state": item.get("session_state", "inactive"),
                    }
                    for item in rows
                ]
            try:
                page, _unused_cursor = bounded_page(rows, limit=limit, cursor=None, scope=scope)
            except InvalidCursor as exc:
                return _read_error("invalid_cursor", str(exc))
            except OutputItemTooLarge as exc:
                return _read_error("output_item_too_large", str(exc))
            has_more = len(page) < len(rows)
            next_cursor = encode_cursor(offset + len(page), scope) if has_more else None
            return {"ok": True, "sessions": page, "next_cursor": next_cursor}

        if subject == "namespaces":
            store = getattr(self.service, "task_store", None)
            if store is None:
                return {"ok": False, "code": "resource_not_found", "error": "resource_not_found"}
            records = await store.list_namespace_records(
                show_archived=show_archived, limit=limit + 1, offset=offset
            )
            priorities = {3: "P0", 2: "P1", 1: "P2", 0: "P3"}
            namespaces = [
                (
                    {"namespace": item["namespace"]}
                    if detail == "summary"
                    else {**item, "priority": priorities.get(int(item["priority"]), "P3")}
                )
                for item in records
            ]
            try:
                page, _unused_cursor = bounded_page(
                    namespaces, limit=limit, cursor=None, scope=scope
                )
            except OutputItemTooLarge as exc:
                return _read_error("output_item_too_large", str(exc))
            has_more = len(page) < len(namespaces)
            next_cursor = encode_cursor(offset + len(page), scope) if has_more else None
            return {
                "ok": True,
                "namespaces": (
                    [item["namespace"] for item in page] if detail == "summary" else page
                ),
                "next_cursor": next_cursor,
            }

        result = await self.service.tasks(
            namespace=namespace,
            task_id=task_id,
            lane=lane,
            state=state,
            operational_status=operational_status,
            tags=tags,
            show_details=detail == "full",
            snapshot=detail == "summary" and task_id is not None,
            show_done=show_done,
            show_archived=show_archived,
            limit=limit,
            cursor=str(offset),
        )
        if namespace is None and task_id is None and result.get("ok"):
            store = getattr(self.service, "task_store", None)
            if store is not None:
                result["namespaces"] = [
                    item["namespace"]
                    for item in await store.list_namespace_records(
                        show_archived=show_archived, limit=limit, offset=0
                    )
                ]
        if not result.get("ok"):
            if result.get("error") == "task not found":
                result["code"] = "resource_not_found"
            return result
        if task_id:
            if result.get("task"):
                try:
                    bounded_page(
                        [result["task"]],
                        limit=1,
                        cursor=None,
                        scope={"kind": "task-detail", "detail": detail},
                    )
                except OutputItemTooLarge as exc:
                    return _read_error("output_item_too_large", str(exc))
            result.pop("namespaces", None)
            return result

        rows = list(result.get("tasks") or [])
        if detail == "summary":
            rows = [_task_summary(item) for item in rows]
        try:
            page, _ignored = bounded_page(
                rows, limit=limit, cursor=None, scope={"kind": "task-page"}
            )
        except OutputItemTooLarge as exc:
            return _read_error("output_item_too_large", str(exc))
        consumed = len(page)
        cursor_positions = list(result.pop("_cursor_positions", []) or [])
        internal_cursor = result.get("next_cursor")
        if consumed < len(rows):
            raw_next_cursor = cursor_positions[consumed - 1] if consumed else offset
        elif internal_cursor is not None:
            raw_next_cursor = int(internal_cursor)
        else:
            raw_next_cursor = None
        next_cursor = encode_cursor(raw_next_cursor, scope) if raw_next_cursor is not None else None
        raw_summary = result.get("summary") or {}
        if detail == "summary":
            summary = {
                "visible": raw_summary.get("visible", len(page)),
                "returned": len(page),
                "claimable_count": raw_summary.get("claimable_count", 0),
                "by_state": raw_summary.get("by_state", {}),
            }
            return {
                "ok": True,
                "summary": summary,
                "tasks": page,
                "next_cursor": next_cursor,
            }
        result["tasks"] = page
        result["next_cursor"] = next_cursor
        if isinstance(result.get("summary"), dict):
            result["summary"]["returned"] = len(page)
        result.pop("tag_counts", None)
        result.pop("recommended", None)
        result.pop("namespaces", None)
        return result

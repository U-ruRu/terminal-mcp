"""Canonical messages application capability (transport independent)."""

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.base import ApplicationCapability, application_operation
from terminal_mcp.application.projections import _read_error
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.read_contract import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    OutputItemTooLarge,
    bounded_page,
    decode_cursor,
    encode_cursor,
    summary_message,
)


def _apply_message_surface_view(rows: list[dict], *, seen_at: str) -> None:
    for item in rows:
        mode_value = item.get("mode")
        if "seen_count" in item:
            item["seen_count"] = int(item.get("seen_count") or 0) + 1
        if seen_at:
            if "first_seen_at" in item:
                item["first_seen_at"] = item.get("first_seen_at") or seen_at
            if "last_seen_at" in item:
                item["last_seen_at"] = seen_at
        if mode_value == "notify":
            item["state"] = "read"
            if "acknowledged" in item:
                item["acknowledged"] = True
            if seen_at and "read_at" in item:
                item["read_at"] = item.get("read_at") or seen_at
        elif item.get("state") == "delivered":
            item["state"] = "seen"


class MessagingApplication(ApplicationCapability):
    @application_operation("messages")
    async def message(
        self,
        actor: ActorContext,
        sender: str,
        code: Annotated[str | None, Field(min_length=4, max_length=4, pattern="^[0-9]{4}$")] = None,
        text: str | None = None,
        target: str | None = None,
        message_hash: str | None = None,
        mode: Literal["notify", "ack", "alert"] | None = None,
        require_reply: bool = False,
        alert: bool = False,
        history: bool = False,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        detail: Literal["summary", "full"] = "summary",
        namespace: str | None = None,
        task_id: str | None = None,
        recipients: bool = False,
        response_preflight: Callable[[dict], dict | None] | None = None,
    ) -> dict:
        backend = self.backend
        if backend is None:
            return {"ok": False, "code": "policy_incompatible", "error": "policy_incompatible"}
        is_read = text is None and message_hash is None
        scope = {
            "kind": "message.recipients" if recipients else "message.read",
            "sender": sender,
            "authorization": code or sender,
            "history": history,
            "detail": detail,
            "namespace": namespace,
            "task_id": task_id,
        }
        offset = 0
        if is_read:
            try:
                offset = decode_cursor(cursor, scope)
            except InvalidCursor as exc:
                return _read_error("invalid_cursor", str(exc))
        operation = (
            ManagedOperation.MESSAGE_READ
            if is_read
            else ManagedOperation.MESSAGE_REPLY
            if text is not None and message_hash is not None
            else ManagedOperation.MESSAGE_ACK
            if message_hash is not None
            else ManagedOperation.MESSAGE_SEND
        )
        resolution = await self.gate.resolve_message_actor(actor, sender, code, operation)
        if resolution.failure is not None:
            return resolution.failure
        if recipients:
            raw = await backend.access_observe_slots(limit=limit + 1, offset=offset)
            if not raw.get("ok"):
                return raw
            rows = list(raw.get("sessions") or [])
            page = rows[:limit]
            next_cursor = encode_cursor(offset + len(page), scope) if len(rows) > limit else None
            return {
                "ok": True,
                "action": "recipients",
                "sender": str((resolution.identity or {}).get("public_name") or sender),
                "recipients": page,
                "next_cursor": next_cursor,
            }
        result = await self.gate.message(
            resolution,
            sender,
            access_code=code,
            text=text,
            target=target,
            message_hash=message_hash,
            require_reply=require_reply,
            alert=alert,
            mode=mode,
            show_all=history,
            limit=min(500, limit + 1) if is_read else limit,
            offset=offset if is_read else 0,
            surface_limit=0 if is_read else None,
            namespace=namespace,
            task_id=task_id,
        )
        if not is_read or not result.get("ok"):
            return result
        raw = list(result.get("messages") or result.get("inbox") or [])
        candidate = raw[:limit]
        rows = [summary_message(item) for item in candidate] if detail == "summary" else candidate
        try:
            page, _ignored = bounded_page(
                rows, limit=limit, cursor=None, scope={"kind": "message-page"}
            )
        except OutputItemTooLarge as exc:
            return _read_error("output_item_too_large", str(exc))
        consumed = len(page)
        has_more = consumed < len(candidate) or len(raw) > consumed
        next_cursor = encode_cursor(offset + consumed, scope) if has_more else None

        result["messages"] = page
        result.pop("inbox", None)
        result["history"] = bool(history)
        result["next_cursor"] = next_cursor
        result.pop("show_all", None)

        if not history and page:
            # Transport adapters may reject the final encoded envelope, but only
            # this application boundary can mutate durable inbox state. Validate
            # a conservative post-surface projection before acknowledging anything.
            if response_preflight is not None:
                preview = dict(result)
                preview_page = [dict(item) for item in page]
                _apply_message_surface_view(preview_page, seen_at="0" * 64)
                preview["messages"] = preview_page
                failure = response_preflight(preview)
                if failure is not None:
                    return failure
            refs = [str(item.get("message_hash") or item.get("message_id") or "") for item in page]
            refs = [ref for ref in refs if ref]
            surface_page = getattr(backend, "surface_message_page", None)
            if refs and callable(surface_page):
                surfaced = await self.gate.surface_message_page(
                    resolution,
                    sender,
                    access_code=code,
                    message_hashes=refs,
                )
                if not surfaced.get("ok"):
                    return surfaced
                _apply_message_surface_view(page, seen_at=str(surfaced.get("seen_at") or ""))

        return result

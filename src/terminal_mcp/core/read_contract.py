from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from typing import Any

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100
DEFAULT_CMD_READ_LINES = 100
MAX_CMD_READ_LINES = 100
READ_RESPONSE_BUDGET_BYTES = 64 * 1024
ITEMS_BUDGET_BYTES = READ_RESPONSE_BUDGET_BYTES - (8 * 1024)
MESSAGE_PREVIEW_CHARS = 512


class InvalidCursor(ValueError):
    pass


class OutputItemTooLarge(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def cursor_scope_fingerprint(scope: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(scope)).hexdigest()[:24]


def encode_cursor(offset: int, scope: dict[str, Any]) -> str:
    payload = {
        "v": 1,
        "o": max(0, int(offset)),
        "q": cursor_scope_fingerprint(scope),
    }
    raw = _canonical(payload)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, scope: dict[str, Any]) -> int:
    if cursor in (None, ""):
        return 0
    text = str(cursor)
    # Internal migration compatibility only. New public responses never emit numeric cursors.
    if text.isdecimal():
        return max(0, int(text))
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise InvalidCursor("cursor is malformed") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or not isinstance(payload.get("o"), int)
        or payload.get("o") < 0
        or payload.get("q") != cursor_scope_fingerprint(scope)
    ):
        raise InvalidCursor("cursor does not match this query")
    return int(payload["o"])


def json_size(value: Any) -> int:
    return len(_canonical(value))


def bounded_page(
    items: Iterable[dict[str, Any]],
    *,
    limit: int,
    cursor: str | None,
    scope: dict[str, Any],
    budget_bytes: int = ITEMS_BUDGET_BYTES,
) -> tuple[list[dict[str, Any]], str | None]:
    rows = list(items)
    offset = decode_cursor(cursor, scope)
    if offset > len(rows):
        raise InvalidCursor("cursor is outside the current result set")
    page: list[dict[str, Any]] = []
    used = 2
    for item in rows[offset : offset + max(1, min(int(limit), MAX_PAGE_LIMIT))]:
        item_size = json_size(item) + 1
        if item_size > budget_bytes:
            if not page:
                raise OutputItemTooLarge("one result item exceeds the response-size budget")
            break
        if page and used + item_size > budget_bytes:
            break
        page.append(item)
        used += item_size
    consumed = len(page)
    next_offset = offset + consumed
    next_cursor = encode_cursor(next_offset, scope) if next_offset < len(rows) else None
    return page, next_cursor


def summary_message(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text") or "")
    preview = text if len(text) <= MESSAGE_PREVIEW_CHARS else text[:MESSAGE_PREVIEW_CHARS] + "…"
    message_id = item.get("message_id") or item.get("message_hash") or item.get("message_ref")
    return {
        key: value
        for key, value in {
            "message_id": message_id,
            "sender": item.get("sender") or item.get("sender_name") or item.get("sender_agent_id"),
            "target": item.get("target"),
            "mode": item.get("mode"),
            "timestamp": item.get("created_at") or item.get("timestamp"),
            "state": item.get("state"),
            "acknowledged": item.get("read_at") is not None or item.get("acknowledged") is True,
            "replied": item.get("replied_at") is not None or item.get("replied") is True,
            "text": preview,
            "truncated": len(text) > len(preview),
            "namespace": item.get("namespace"),
            "task_id": item.get("task_id"),
        }.items()
        if value is not None
    }


def bound_rendered_lines(
    lines: list[str],
    *,
    budget_bytes: int = ITEMS_BUDGET_BYTES,
) -> tuple[list[str], bool]:
    bounded: list[str] = []
    used = 2
    oversized = False
    for line in lines:
        encoded = line.encode("utf-8")
        if len(encoded) + 3 > budget_bytes:
            room = max(0, budget_bytes - used - 64)
            clipped = encoded[:room].decode("utf-8", errors="ignore")
            bounded.append(clipped + " …[truncated]")
            oversized = True
            break
        cost = len(encoded) + 3
        if bounded and used + cost > budget_bytes:
            break
        bounded.append(line)
        used += cost
    return bounded, oversized

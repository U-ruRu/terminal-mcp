"""Central public MCP input budgets shared by transport and application contracts."""

from __future__ import annotations

import json

MAX_IDENTIFIER_CHARS = 120
MAX_PUBLIC_NAME_CHARS = 120
MAX_OPAQUE_CURSOR_CHARS = 512
MAX_HASH_CHARS = 128
MAX_TAG_CHARS = 64
MAX_TAG_ITEMS = 50
MAX_MESSAGE_TEXT_CHARS = 16 * 1024
MAX_COMMAND_CHARS = 32 * 1024
MAX_TASK_SCOPE_CHARS = 256
MAX_CONTEXT_SUMMARY_CHARS = 100
MAX_CONTEXT_CONTENT_CHARS = 32 * 1024
MAX_SQLITE_INTEGER = (1 << 63) - 1
MAX_QUEUE_ID = 65_535

TASK_RESOURCE_CONTEXT_MAX_BYTES = 8 * 1024
TASK_CHECKPOINT_MAX_BYTES = 16 * 1024
TASK_RESULT_MAX_BYTES = 32 * 1024
TASK_REVIEW_EVIDENCE_MAX_BYTES = 16 * 1024
TASK_RESULT_TEXT_MAX_CHARS = 16 * 1024


def serialized_json_size(value: object) -> int:
    """Return deterministic UTF-8 JSON size for an agent-controlled extension value."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded)


def require_serialized_json_limit(value: object, *, field: str, limit: int) -> object:
    try:
        size = serialized_json_size(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain JSON-serializable values") from exc
    if size > limit:
        raise ValueError(f"{field} exceeds {limit} serialized UTF-8 bytes")
    return value

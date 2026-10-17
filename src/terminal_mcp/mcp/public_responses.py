"""Explicit wire projections. User JSON is opaque; this is not a recursive cleaner."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from terminal_mcp.core.public_errors import (
    MAX_COORDINATION_MESSAGES,
    ConflictRepair,
    RetryRepair,
    ValidationRepair,
    normalize_public_error,
)
from terminal_mcp.core.read_contract import CALL_TOOL_RESULT_BUDGET_BYTES, summary_message
from terminal_mcp.core.task_projections import (
    TaskDependency,
    TaskEvent,
    TaskLane,
    TaskOutputState,
    TaskPriority,
    TaskRelation,
    TaskReview,
    TaskState,
    WorkflowWarning,
)

ResponseErrorRepair = ValidationRepair | ConflictRepair | RetryRepair


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IncomingMessage(_Strict):
    id: str = Field(min_length=1)
    sender: str
    text: str
    requirements: Literal["ack", "reply"] | None = None


class OperationNotices(_Strict):
    messages_remaining: int | None = Field(
        default=None,
        gt=0,
        description="Incoming entries omitted from this bounded operation preview; "
        "read message history with its continuation cursor. Not an ACK/delivery counter.",
    )
    messages: list[IncomingMessage] | None = Field(default=None, min_length=1)
    warnings: list[WorkflowWarning] | None = Field(default=None, min_length=1)
    postcommit_warning: str | None = None
    return_to_chat: Literal[True] | None = None
    intent_warning: str | None = None


class PublicTaskClaim(_Strict):
    agent_name: str
    claimed_at: str
    claim_intent: str


class PublicCheckpoint(_Strict):
    checkpoint: JsonValue
    author: str
    created_at: str


class PublicTaskSnapshot(_Strict):
    namespace: str
    task_id: str
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    revision: int
    claim: PublicTaskClaim | None
    next_action: str
    description_preview: str
    description_truncated: bool
    latest_checkpoint: PublicCheckpoint | None = None
    blocking_dependencies: list[TaskDependency] | None = Field(default=None, min_length=1)


class PublicTaskDetail(_Strict):
    namespace: str
    task_id: str
    title: str
    lane: TaskLane
    priority: TaskPriority
    state: TaskState
    revision: int
    next_action: str
    cooperative: bool
    checkpoint: JsonValue
    result: JsonValue
    tags: list[str]
    isolation_hint: str
    claims: list[PublicTaskClaim]
    state_changed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    description: str | None = None
    input_refs: list[str] | None = Field(default=None, min_length=1)
    output_refs: list[str] | None = Field(default=None, min_length=1)
    participants: list[PublicTaskClaim] | None = Field(default=None, min_length=1)
    blocking_dependencies: list[TaskDependency] | None = Field(default=None, min_length=1)
    resource_context: dict[str, JsonValue] | None = Field(default=None, min_length=1)
    relations: list[TaskRelation] | None = Field(default=None, min_length=1)


class PublicTaskEvent(_Strict):
    id: int
    event_type: str
    payload: JsonValue
    created_at: str
    agent_name: str | None = None


class PublicTaskHistory(_Strict):
    namespace: str
    task_id: str
    items: list[PublicTaskEvent | TaskEvent | TaskReview | TaskOutputState]
    next_cursor: str | None = None


def _incoming_sources(raw: dict) -> list[dict]:
    sources = [raw]
    for key in ("coordination", "details"):
        if isinstance(raw.get(key), dict):
            sources.append(raw[key])
    return sources


def incoming_messages(raw: dict) -> list[dict]:
    """Merge the two incoming-obligation aliases without performing receipt writes."""
    sources = _incoming_sources(raw)
    result: dict[str, dict] = {}
    for source in sources:
        for key in ("messages", "pending_messages"):
            for value in source.get(key) or []:
                if not isinstance(value, dict):  # old pending_messages contains rendered strings
                    continue
                identifier = value.get("id") or value.get("message_hash") or value.get("message_id")
                if not identifier:
                    continue
                mode = value.get("mode") or value.get("delivery_mode")
                required = value.get("requirements")
                if required not in {"ack", "reply"}:
                    if (mode == "alert" or value.get("alert")) and not (
                        value.get("replied_at")
                        or value.get("replied")
                        or value.get("state") == "replied"
                    ):
                        required = (
                            "ack" if value.get("sender") == "terminal-mcp-system" else "reply"
                        )
                    elif mode == "ack" and not (
                        value.get("read_at")
                        or value.get("acknowledged")
                        or value.get("state") in {"read", "acknowledged", "replied"}
                    ):
                        required = "ack"
                    elif value.get("require_reply") and not value.get("replied_at"):
                        required = "reply"
                    else:
                        required = None
                item = {
                    "id": identifier,
                    "sender": value.get("sender") or value.get("sender_name") or "",
                    "text": value.get("text", ""),
                }
                if required:
                    item["requirements"] = required
                # Prefer the still-required action when one alias has a stale view.
                old = result.get(identifier)
                if old is None or required and (required == "reply" or "requirements" not in old):
                    result[identifier] = item
    return list(result.values())


def operation_notices(raw: dict) -> dict:
    result = {}
    if isinstance(raw.get("_meta"), dict) and raw["_meta"]:
        result["_meta"] = dict(raw["_meta"])
    incoming = incoming_messages(raw)
    visible = incoming[:MAX_COORDINATION_MESSAGES]
    messages = []
    sources = _incoming_sources(raw)
    truncated = any(
        isinstance(item, dict) and item.get("truncated") is True
        for source in sources
        for key in ("messages", "pending_messages")
        for item in source.get(key) or []
    )
    for item in visible:
        preview = summary_message(item)
        messages.append({**item, "text": preview["text"]})
        truncated = truncated or preview["truncated"]
    # Bound actual UTF-8 JSON, including escaping of control characters. Keep
    # names, IDs and requirements whole; only the explicitly advertised text preview shrinks.
    text_limit = 512
    while (
        len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        > (CALL_TOOL_RESULT_BUDGET_BYTES // 4)
        and text_limit
    ):
        text_limit //= 2
        messages = [{**item, "text": item["text"][:text_limit]} for item in messages]
        truncated = True
    if messages:
        result["messages"] = messages
    remaining = max(
        len(incoming) - len(messages),
        *(int(source.get("messages_remaining") or 0) for source in sources),
    )
    if remaining:
        result["messages_remaining"] = remaining
    if raw.get("warnings"):
        result["warnings"] = list(raw["warnings"])
    if (truncated or remaining) and not any(
        item.get("code") == "incoming_messages_truncated" for item in result.get("warnings", [])
    ):
        count_note = (
            f" {remaining} additional incoming entries are not displayed." if remaining else ""
        )
        result.setdefault("warnings", []).append(
            {
                "code": "incoming_messages_truncated",
                "severity": "warning",
                "message": "Incoming messages are shown as bounded previews."
                + count_note
                + " Find IDs via message(action='history', detail='summary') and next_cursor. "
                "Read full text with message(action='read', message_hash=ID, text_offset=0) and "
                "follow next_text_offset. Compatibility MCP uses read_message_hash=ID instead. "
                "Pending ACK/reply obligations are unchanged.",
            }
        )
    for key in ("postcommit_warning", "intent_warning"):
        if raw.get(key):
            result[key] = raw[key]
    lifecycle = raw.get("session_lifecycle") or {}
    if raw.get("return_to_chat") or lifecycle.get("return_to_chat"):
        result["return_to_chat"] = True
    return result


def error_response(raw: dict) -> dict:
    """Project bounded failure notices once, retaining authoritative operation outcome."""
    canonical = normalize_public_error(raw).as_dict()
    notices = operation_notices({**raw, **canonical})
    if canonical["code"] in {"coordination_alert", "coordination_ack_required"}:
        # The same incoming obligations are now represented only by root messages.
        canonical.pop("details", None)
    receipt = raw.get("cmd_hash")
    if isinstance(receipt, str) and 0 < len(receipt) <= 128:
        canonical["cmd_hash"] = receipt
    return {**canonical, **notices}


def remaining_seconds(raw: dict) -> int | None:
    cycle = raw.get("session_lifecycle") or {}
    remaining = cycle.get("remaining_seconds", raw.get("remaining_seconds"))
    if type(remaining) is int:
        return max(0, remaining)
    expiry = cycle.get("hard_expires_at") or raw.get("hard_expires_at")
    if isinstance(expiry, str):
        deadline = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if deadline.tzinfo is not None:
            return max(0, int((deadline - datetime.now(UTC)).total_seconds()))
    return None


def _claim(raw: dict) -> dict:
    return {key: raw[key] for key in PublicTaskClaim.model_fields if key in raw}


def public_task(raw: dict, detail: str, *, history_kind: str = "events") -> dict:
    if detail == "snapshot":
        item = {key: raw[key] for key in PublicTaskSnapshot.model_fields if key in raw}
        if isinstance(item.get("claim"), dict):
            item["claim"] = _claim(item["claim"])
        checkpoint = raw.get("latest_checkpoint")
        if isinstance(checkpoint, dict):
            item["latest_checkpoint"] = {
                # Never JSON-decode legacy text: a JSON-looking string is still a string.
                "checkpoint": checkpoint.get("checkpoint", checkpoint.get("text")),
                "author": checkpoint["author"],
                "created_at": checkpoint["created_at"],
            }
        if not item.get("blocking_dependencies"):
            item.pop("blocking_dependencies", None)
        return item
    if detail == "history":
        item = {key: raw[key] for key in ("namespace", "task_id", "items") if key in raw}
        if history_kind == "events":
            item["items"] = [
                {key: event[key] for key in PublicTaskEvent.model_fields if key in event}
                for event in raw.get("items", [])
            ]
        if raw.get("next_cursor"):
            item["next_cursor"] = raw["next_cursor"]
        return item
    item = {key: raw[key] for key in PublicTaskDetail.model_fields if key in raw}
    for key in ("claims", "participants"):
        if key in item:
            item[key] = [_claim(claim) for claim in item[key]]
    blocking = {
        (dep["namespace"], dep["task_id"]) for dep in item.get("blocking_dependencies") or []
    }
    if item.get("relations"):
        item["relations"] = [
            relation
            for relation in item["relations"]
            if not (
                relation.get("kind") == "depends_on"
                and relation.get("direction") == "outgoing"
                and (relation["namespace"], relation["task_id"]) in blocking
            )
        ]
    if isinstance(item.get("resource_context"), dict):
        item["resource_context"] = {
            key: value for key, value in item["resource_context"].items() if value is not None
        }
    for key in (
        "input_refs",
        "output_refs",
        "participants",
        "blocking_dependencies",
        "resource_context",
        "relations",
    ):
        if item.get(key) in (None, [], {}):
            item.pop(key, None)
    return item


async def resolve_task_names(
    application, task: dict, detail: str, history_kind: str = "events"
) -> dict:
    """Read the established Access authority; never allocate identities in a projection."""
    mesh = getattr(application.service, "access_mesh", None)
    cache = {}

    async def name(value):
        if not isinstance(value, str) or not value:
            return value
        if value in cache:
            return cache[value]
        slot = await asyncio.to_thread(mesh.store.slot_for_agent, value) if mesh else None
        cache[value] = mesh.store.human_name(slot) if slot else value
        return cache[value]

    result = dict(task)
    for key in ("claims", "participants"):
        if key in result:
            result[key] = [
                {**entry, "agent_name": await name(entry["agent_name"])} for entry in result[key]
            ]
    if isinstance(result.get("claim"), dict):
        result["claim"] = {
            **result["claim"],
            "agent_name": await name(result["claim"]["agent_name"]),
        }
    if isinstance(result.get("latest_checkpoint"), dict):
        result["latest_checkpoint"] = {
            **result["latest_checkpoint"],
            "author": await name(result["latest_checkpoint"]["author"]),
        }
    if detail == "history" and history_kind == "events":
        result["items"] = [
            {
                **entry,
                "agent_name": await name(entry.get("agent_name") or entry.get("logical_agent_id")),
            }
            for entry in result.get("items", [])
        ]
    return result

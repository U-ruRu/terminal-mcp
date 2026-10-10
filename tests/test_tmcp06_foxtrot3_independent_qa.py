"""TMCP-06 independent acceptance edges: diagnostic merge, no live writes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.observations import ObservationApplication
from terminal_mcp.attempt_diagnostics import emit_attempt, result_references
from terminal_mcp.mcp.output_contracts import MessageRecord, _message_record
from terminal_mcp.mcp.role_contracts import ROLE_TOOL_DESCRIPTIONS, TaskGetInput
from terminal_mcp.mcp.roles import _task_get
from terminal_mcp.trace import current_upstream_request_id

mesh_pair = mesh_pair_fixture


class _BrokenEvents:
    def emit(self, *_args, **_kwargs):
        raise RuntimeError("unavailable optional diagnostics")


def test_diagnostic_sink_failure_and_receipt_whitelist_are_independent():
    token = current_upstream_request_id.set("secret-request-id")
    try:
        emit_attempt(_BrokenEvents(), "mcp_tool_finished", cmd_hash="deadbeef")
        refs = result_references(
            {
                "ok": True,
                "command": {"cmd_hash": "deadbeef", "stdout": "private-output"},
                "task": {"task_id": "TASK-6", "revision": 3, "description": "private"},
                "jsonrpc_id": 17,
                "access_code": "REDACT-ME",
            }
        )
    finally:
        current_upstream_request_id.reset(token)
    assert refs == {
        "result_ok": True,
        "cmd_hash": "deadbeef",
        "task_id": "TASK-6",
        "task_revision": 3,
    }
    assert current_upstream_request_id.get() is None


def test_message_delivery_wire_schema_strips_internal_identity():
    item = _message_record(
        {
            "state": "seen",
            "deliveries": [
                {
                    "public_name": "Alpha-3",
                    "state": "seen",
                    "first_seen_at": "2026-10-10T23:00:00Z",
                    "read_at": None,
                    "seen_count": 1,
                    "logical_agent_id": "SECRET_INTERNAL_ID",
                    "recipient_agent_id": "SECRET_RECIPIENT_ID",
                }
            ],
        }
    )
    assert len(item["deliveries"]) == 1
    assert item["deliveries"][0]["public_name"] == "Alpha-3"
    assert item["deliveries"][0]["state"] == "seen"
    assert item["deliveries"][0]["read_at"] is None
    assert "logical_agent_id" not in str(item)
    assert "recipient_agent_id" not in str(item)
    assert "deliveries" in MessageRecord.model_fields


@pytest.mark.asyncio
async def test_broadcast_mixed_reply_ack_aggregates_conservatively(mesh_pair):
    node = mesh_pair.nodes["bacloud"]
    h = (await node.message(actor("bob"), mode="ack", text="independent QA mixed reply"))[
        "message_hash"
    ]
    await node.message(actor("alice"))
    await node.message(actor("alice"), message_hash=h, text="reply before peer ACK")
    await node.message(actor("carol"))
    partially = next(
        item
        for item in (await node.message(actor("bob"), history=True, detail="full"))["messages"]
        if item["message_hash"] == h
    )
    names = {receipt["public_name"]: receipt for receipt in partially["deliveries"]}
    assert names[_public_name(node.mesh.store.slots["alice"])]["state"] == "replied"
    assert names[_public_name(node.mesh.store.slots["carol"])]["state"] == "seen"
    assert partially["state"] == "seen"
    assert partially["read_at"] is None and partially["replied_at"] is None

    await node.message(actor("carol"), message_hash=h)
    after = next(
        item
        for item in (await node.message(actor("bob"), history=True, detail="full"))["messages"]
        if item["message_hash"] == h
    )
    assert after["state"] == "read" and after["read_at"] is not None
    assert after["replied_at"] is None and after["reply_message_hash"] is None


class _Backend:
    def __init__(self):
        self.task = {
            "namespace": "qa-independent",
            "task_id": "T-CURSOR",
            "title": "unchanged content revision",
            "lane": "review",
            "priority": "P2",
            "state": "in_progress",
            "operational_status": "in_progress",
            "revision": 1,
            "events": [
                {
                    "id": i,
                    "event_type": "comment",
                    "payload": {"text": str(i)},
                    "created_at": "2026-10-10T23:00:00Z",
                }
                for i in range(4, 0, -1)
            ],
        }

    async def tasks(self, **_kwargs):
        return {"ok": True, "task": deepcopy(self.task)}


class _Gate:
    async def resolve(self, *_args):
        return SimpleNamespace(failure=None)


@pytest.mark.asyncio
async def test_mutable_state_audit_rejects_cursor_at_same_content_revision():
    backend = _Backend()
    app = ObservationApplication(backend, _Gate())

    async def page(cursor=None):
        return await _task_get(
            app,
            ActorContext(endpoint_role="coordinator"),
            TaskGetInput(
                namespace="qa-independent",
                task_id="T-CURSOR",
                detail="history",
                history_kind="events",
                limit=2,
                cursor=cursor,
            ),
        )

    first = await page()
    assert first["ok"] is True
    assert [e["id"] for e in first["task"]["items"]] == [4, 3]
    old = first["task"]["next_cursor"]
    backend.task["state"] = "done"
    backend.task["operational_status"] = "done"
    backend.task["events"].insert(
        0,
        {
            "id": 5,
            "event_type": "state",
            "payload": {"to": "done"},
            "created_at": "2026-10-10T23:00:01Z",
        },
    )
    assert backend.task["revision"] == 1
    rejected = await page(old)
    assert rejected["ok"] is False and rejected["code"] == "invalid_cursor"
    restarted = await page()
    assert [e["id"] for e in restarted["task"]["items"]] == [5, 4]


def test_merge_surface_retains_both_role_and_message_contracts():
    assert "invalid_cursor" in ROLE_TOOL_DESCRIPTIONS[("coordinator", "task_get")]
    assert "state='done'" in ROLE_TOOL_DESCRIPTIONS[("coordinator", "task_list")]
    assert "deliveries" in MessageRecord.model_fields

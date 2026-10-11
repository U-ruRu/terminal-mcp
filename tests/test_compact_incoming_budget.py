"""Bounded operation previews preserve receipts and never mutate their inputs."""

from copy import deepcopy

import pytest
from test_compact_response_independent import _call, _message, _run, _server, _task

from terminal_mcp.core.public_errors import MAX_COORDINATION_MESSAGES
from terminal_mcp.core.read_contract import MESSAGE_PREVIEW_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["command_run", "task_claim", "command_read"])
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
async def test_large_incoming_keeps_receipt_action_and_original(monkeypatch, operation, mode):
    messages = [_message(mode, text="Ж" * 16384, msg_id=f"test:msg:{i}") for i in range(20)]
    raw = _run()
    raw.update(messages=messages, pending_messages=deepcopy(messages))
    if operation == "task_claim":
        raw.update(action="claim", task=_task(snapshot=True))
        args = {
            "action": "claim",
            "namespace": "acceptance",
            "task_id": "ONE",
            "claim_intent": "bounded preview",
        }
        method = "task"
    elif operation == "command_read":
        args = {"cmd_hash": raw["cmd_hash"]}
        method = "cmd"
    else:
        args = {"command": "printf fixture"}
        method = "cmd"
    before = deepcopy(raw)
    data, _ = await _call(_server(monkeypatch, "executor", **{method: raw}), operation, args)
    assert data["ok"] is True
    if operation == "command_run":
        assert data["cmd_hash"] == before["cmd_hash"] and data["status"] == before["status"]
    assert len(data["messages"]) == MAX_COORDINATION_MESSAGES
    assert data["messages_remaining"] == len(messages) - MAX_COORDINATION_MESSAGES
    assert any(w["code"] == "incoming_messages_truncated" for w in data["warnings"])
    for index, item in enumerate(data["messages"]):
        assert item["id"] == messages[index]["message_hash"]
        assert item["sender"] == messages[index]["sender"]
        assert item["text"] == "Ж" * MESSAGE_PREVIEW_CHARS + "…"
        if mode == "notify":
            assert "requirements" not in item
        else:
            assert item["requirements"] == ("reply" if mode == "alert" else "ack")
    assert raw == before

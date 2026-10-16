"""Bounded operation previews preserve receipts and never mutate their inputs."""

from copy import deepcopy

import pytest
from test_compact_response_independent import _call, _message, _run, _server

from terminal_mcp.core.public_errors import MAX_COORDINATION_MESSAGES
from terminal_mcp.core.read_contract import MESSAGE_PREVIEW_CHARS


@pytest.mark.asyncio
async def test_large_incoming_is_bounded_on_command_run(monkeypatch):
    mode = "notify"
    messages = [_message(mode, text="Ж" * 16384, msg_id=f"test:msg:{i}") for i in range(20)]
    raw = _run()
    raw.update(messages=messages, pending_messages=deepcopy(messages))
    args = {"command": "printf fixture"}
    before = deepcopy(raw)
    data, _ = await _call(_server(monkeypatch, "executor", cmd=raw), "command_run", args)
    assert data["ok"] is True
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

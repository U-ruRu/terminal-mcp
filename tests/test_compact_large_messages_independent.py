"""Independent durable-receipt checks with legal-size incoming notifications.
No shell runs, network requests, live messages, or existing database access.
"""

import json

import pytest
import test_compact_response_independent as base
from jsonschema import Draft202012Validator

from terminal_mcp.storage.agents import AgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


@pytest.mark.asyncio
async def test_committed_run_retains_receipt_with_large_incoming(
    monkeypatch, tmp_path
):
    repository = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    record = await repository.create(
        "fixture only; never executed", cmd_hash="af01bc23", status="queued"
    )
    raw = base._run()
    raw["cmd_hash"] = record.cmd_hash
    raw["messages"] = [
        base._message("notify", text="b" * 8192, msg_id=f"fixture:msg:large-{i}")
        for i in range(8)
    ]
    raw["pending_messages"] = list(raw["messages"])
    store = AgentStore(tmp_path / "commands.sqlite3")
    for message in raw["messages"]:
        await store.create_message(
            message["message_hash"],
            "fixture-sender",
            "fixture-recipient",
            message["text"],
            base.NOW,
            ["fixture-recipient"],
            delivery_mode="notify",
        )
    stored_before = [
        (
            await store.message_record(m["message_hash"]),
            await store.recipient_record(m["message_hash"], "fixture-recipient"),
        )
        for m in raw["messages"]
    ]
    server = base._server(monkeypatch, "executor", cmd=raw)
    tool = server._tool_manager.get_tool("command_run")
    response = await tool.run({"command": "fixture only; never executed"}, convert_result=True)
    data = response.structuredContent
    Draft202012Validator(tool.output_schema).validate(data)
    assert json.loads(response.content[0].text) == data
    persisted = await repository.get(record.cmd_hash)
    assert persisted is not None and persisted.status == "queued"
    stored_after = [
        (
            await store.message_record(m["message_hash"]),
            await store.recipient_record(m["message_hash"], "fixture-recipient"),
        )
        for m in raw["messages"]
    ]
    assert stored_after == stored_before, "Projection changed stored text/read/ACK/reply state"
    summary = {
        "text_characters": 8192,
        "messages": len(raw["messages"]),
        "stored_cmd_hash": persisted.cmd_hash,
        "wire_keys": list(data),
        "wire_cmd_hash": data.get("cmd_hash"),
        "wire_error": data.get("error"),
        "structured_bytes": len(json.dumps(data, ensure_ascii=False).encode()),
    }
    print(json.dumps(summary, ensure_ascii=False))
    assert record.cmd_hash in json.dumps(data), f"Durable command receipt lost: {summary}"
    if not data.get("ok"):
        assert data.get("error", {}).get("outcome") != "not_committed", summary

    displayed = data.get("messages", [])
    originals = {m["message_hash"]: m["text"] for m in raw["messages"]}
    shortened = len(displayed) < len(raw["messages"]) or any(
        m["text"] != originals[m["id"]] for m in displayed
    )
    if shortened and data.get("ok"):
        assert data.get("warnings"), "A nonempty preview/remaining notice is mandatory"


@pytest.mark.asyncio
async def test_read_projection_preserves_real_message_text_and_ack_state(
    monkeypatch, tmp_path
):
    mode, requirement = "ack", "ack"
    path = tmp_path / "messages.sqlite3"
    repository = SqliteRepository(path, tmp_path / "output.sqlite3")
    await repository.initialize()
    store = AgentStore(path)
    msg = base._message(mode, text="immutable original message")
    await store.create_message(
        msg["message_hash"],
        "fixture-sender",
        "fixture-recipient",
        msg["text"],
        base.NOW,
        ["fixture-recipient"],
        delivery_mode=mode,
        require_reply=mode != "notify",
        alert=mode == "alert",
    )
    before = (
        await store.message_record(msg["message_hash"]),
        await store.recipient_record(msg["message_hash"], "fixture-recipient"),
    )
    raw = base._run("running")
    raw["messages"] = [msg]
    raw["pending_messages"] = [dict(msg)]
    data, _ = await base._call(
        base._server(monkeypatch, "executor", cmd=raw), "command_read", {"cmd_hash": "aabbccdd"}
    )
    assert data["ok"] is True
    expected = {"id": msg["message_hash"], "sender": base.PUBLIC_NAME, "text": msg["text"]}
    if requirement:
        expected["requirements"] = requirement
    assert data["messages"] == [expected]
    after = (
        await store.message_record(msg["message_hash"]),
        await store.recipient_record(msg["message_hash"], "fixture-recipient"),
    )
    assert after == before
    assert after[1]["read_at"] is None and after[1]["replied_at"] is None

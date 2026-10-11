"""Independent compact/chunk acceptance; isolated stores and public wire only."""

import json
from copy import deepcopy

import pytest
import test_compact_response_independent as base
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair
from test_compact_error_notices_independent import _gate_refusal
from test_message_text_chunks import _recipient, _server

from terminal_mcp.core.read_contract import CALL_TOOL_RESULT_BUDGET_BYTES
from terminal_mcp.mcp.output_contracts import serialized_call_tool_result_size


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
@pytest.mark.parametrize("profile", ["mixed", "escaped", "astral"])
async def test_independent_chunk_reconstruction_bounds_and_empty_tail(
    mesh_pair, monkeypatch, record_property, mode, profile
):
    node = mesh_pair.nodes["bacloud"]
    unit = {"mixed": "Ж🙂e\u0301漢\n", "escaped": '\x01\t\n"\\', "astral": "🧪"}[profile]
    original = (unit * 16384)[:16384]
    sent = await node.message(
        actor("alice", f"independent-{mode}-{profile}"),
        text="historical storage fixture" if profile == "astral" else original,
        target=_public_name(node.mesh.store.slots["bob"]),
        mode=mode,
    )
    assert sent["ok"], sent
    identifier = sent["message_hash"]
    if profile == "astral":
        # Seed a historical full-size record in this isolated fixture database.
        # Admission of a new 65536-byte body is separately preflight-tested below.
        with node.store.connect() as db:
            db.execute(
                "UPDATE coordination_messages SET text=? WHERE message_hash=?",
                (original, identifier),
            )
    server = _server(monkeypatch, node)
    before = _recipient(node, identifier)
    offset, pieces, sizes, names = 0, [], [], set()
    limit = 2048 if profile != "mixed" else 1023
    while True:
        data, wire = await base._call(
            server,
            "message",
            {
                "action": "read",
                "message_hash": identifier,
                "text_offset": offset,
                "text_limit": limit,
            },
        )
        assert data["ok"], data
        size = serialized_call_tool_result_size(wire)
        sizes.append(size)
        assert size <= CALL_TOOL_RESULT_BUDGET_BYTES
        assert json.loads(wire.content[0].text) == wire.structuredContent
        item = data["messages"][0]
        assert item["message_hash"] == identifier
        names.add(item["sender"])
        assert item["mode"] == mode
        assert item["text_offset"] == offset
        assert item["text_length"] == len(original)
        expected = original[offset : offset + limit]
        assert item["text"] == expected
        assert item["text"].encode() == expected.encode()
        pieces.append(item["text"])
        end = offset + len(expected)
        if end == len(original):
            assert "next_text_offset" not in item
            break
        assert item["next_text_offset"] == end and end > offset
        offset = end
    assert "".join(pieces) == original
    assert names == {_public_name(node.mesh.store.slots["alice"])}
    # Exact-end read is empty, not a repeated last page. Beyond-end is rejected.
    for offset, success in [(16384, True), (16385, False), (-1, False)]:
        data, wire = await base._call(
            server,
            "message",
            {"action": "read", "message_hash": identifier, "text_offset": offset, "text_limit": 1},
        )
        assert data["ok"] is success, data
        assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES
        if success:
            item = data["messages"][0]
            assert item["text"] == "" and item["text_offset"] == 16384
            assert "next_text_offset" not in item
    after = _recipient(node, identifier)
    for key in ("replied_at", "reply_message_hash"):
        assert after[key] == before[key]
    if mode in {"ack", "alert"}:
        assert after["read_at"] == before["read_at"]
    with node.store.connect() as db:
        assert (
            db.execute(
                "SELECT text FROM coordination_messages WHERE message_hash=?", (identifier,)
            ).fetchone()[0]
            == original
        )
    assert not mesh_pair.calls
    record_property("max_serialized_call_tool_result_bytes", max(sizes))
    record_property("original_utf8_bytes", len(original.encode()))
    record_property("chunk_count", len(pieces))
    print(
        json.dumps(
            {
                "profile": profile,
                "mode": mode,
                "max_wire_bytes": max(sizes),
                "utf8_bytes": len(original.encode()),
                "chunks": len(pieces),
            }
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "read", "cancel", "recovery"])
@pytest.mark.parametrize("profile", ["unicode", "escaped"])
async def test_accepted_command_and_recovery_receipt_survives_large_incoming(
    monkeypatch, record_property, action, profile
):
    raw = base._run("cancelled" if action == "cancel" else "failed")
    raw["exit_code"] = 7
    raw["cmd"] = "fixture only; never executed"
    raw["error"] = "fixture execution failure"
    raw["lines"] = ["stdout: fixture", "stderr: fixture"]
    raw["overall_lines_count"] = raw["displayed_lines_count"] = 2
    if action == "recovery":
        raw.pop("has_more", None)
    unit = "Ж🙂漢" if profile == "unicode" else "\x01"
    text = (unit * 16384)[:16384]
    raw["messages"] = [
        base._message("notify", text=text, msg_id=f"independent:msg:{i}") for i in range(8)
    ]
    raw["pending_messages"] = deepcopy(raw["messages"])
    before = deepcopy(raw)
    args = (
        {"cmd_hash": raw["cmd_hash"]} if action in {"read", "cancel"} else {"command": raw["cmd"]}
    )
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        f"command_{action}",
        args,
    )
    assert data["ok"], data
    receipt = data if action == "run" else data["command"]
    assert receipt["cmd_hash"] == raw["cmd_hash"]
    assert receipt["status"] == raw["status"]
    if action in {"read", "recovery"}:
        assert receipt["exit_code"] == 7 and data["lines"] == raw["lines"]
        assert receipt["error"] == raw["error"]
    assert data.get("warnings"), "Preview must explicitly identify incompleteness"
    assert "text_offset" in json.dumps(data["warnings"])
    assert data.get("messages_remaining") == 4
    assert data["messages"][0]["id"] == "independent:msg:0"
    assert "requirements" not in data["messages"][0]
    assert "coordination" not in data and "pending_messages" not in data
    assert raw == before
    size = serialized_call_tool_result_size(wire)
    assert size <= CALL_TOOL_RESULT_BUDGET_BYTES
    record_property("serialized_call_tool_result_bytes", size)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "read", "cancel", "recovery"])
@pytest.mark.parametrize(
    "mode,code,required",
    [
        ("ack", "coordination_ack_required", "ack"),
        ("alert", "coordination_alert", "reply"),
    ],
)
async def test_command_refusal_retains_action_and_bounded_unicode_notice(
    monkeypatch, record_property, action, mode, code, required
):
    message = base._message(mode, text="🧪" * 16384)
    message["sender"] = "Alpha-7 " + "s" * 70
    raw = await _gate_refusal(message, mode, code)
    before = deepcopy(raw)
    args = (
        {"cmd_hash": "aabbccdd"}
        if action in {"read", "cancel"}
        else {"command": "fixture only; never executed"}
    )
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        f"command_{action}",
        args,
    )
    assert data["ok"] is False and data["error"]["code"] == code, data
    assert data["error"]["outcome"] == "not_committed"
    incoming = data["messages"][0]
    assert incoming["id"] == message["message_hash"]
    assert incoming["sender"] == message["sender"]
    assert incoming["requirements"] == required
    if incoming["text"] != message["text"]:
        assert data.get("warnings"), "Truncated error notices require a warning"
        assert "text_offset" in json.dumps(data["warnings"])
    details = data["error"].get("details", {})
    assert "pending_messages" not in details
    assert "ack_required_pending" not in details and "alert_pending" not in details
    for key in ("message_hash", "text", "required_action"):
        assert key not in data
    size = serialized_call_tool_result_size(wire)
    assert size <= CALL_TOOL_RESULT_BUDGET_BYTES
    assert raw == before
    record_property("serialized_call_tool_result_bytes", size)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
async def test_oversized_send_preflight_rejects_before_commit(mesh_pair, mode):
    node = mesh_pair.nodes["bacloud"]
    with node.store.connect() as db:
        before = list(db.execute("SELECT * FROM coordination_messages"))
    result = await node.message(
        actor("alice", f"independent-preflight-{mode}"),
        text="🧪" * 16384,
        target=_public_name(node.mesh.store.slots["bob"]),
        mode=mode,
    )
    assert result["ok"] is False and result["code"] == "output_item_too_large"
    assert result["outcome"] == "not_committed"
    with node.store.connect() as db:
        after = list(db.execute("SELECT * FROM coordination_messages"))
    assert before == after


@pytest.mark.asyncio
async def test_recovery_large_output_plus_incoming_retains_committed_receipt(
    monkeypatch, tmp_path, record_property
):
    """Each constituent fits its budget; the mirrored combined result must too."""
    from terminal_mcp.storage.sqlite import SqliteRepository

    repository = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    record = await repository.create(
        "fixture only; never executed", cmd_hash="bc12de34", status="completed"
    )
    raw = base._run("completed")
    raw.update(cmd_hash=record.cmd_hash, cmd=record.cmd, exit_code=0)
    raw.pop("has_more", None)
    raw["lines"] = ["x" * 1024 for _ in range(22)]
    raw["overall_lines_count"] = raw["displayed_lines_count"] = 22
    raw["output_bytes"] = 22 * 1024
    raw["messages"] = [
        base._message("notify", text="\x01" * 16384, msg_id=f"budget:msg:{i}") for i in range(4)
    ]
    raw["pending_messages"] = deepcopy(raw["messages"])
    before = deepcopy(raw)
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        "command_recovery",
        {"command": "fixture only; never executed"},
    )
    assert await repository.get(record.cmd_hash) is not None
    assert record.cmd_hash in json.dumps(data), data
    if data["ok"] is False:
        assert data["error"]["outcome"] == "committed", data
    assert raw == before
    size = serialized_call_tool_result_size(wire)
    assert size <= CALL_TOOL_RESULT_BUDGET_BYTES
    record_property("serialized_call_tool_result_bytes", size)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["run", "recovery"])
async def test_already_committed_failure_keeps_receipt_and_outcome_with_notices(
    monkeypatch, tmp_path, action
):
    from terminal_mcp.core.public_errors import public_error
    from terminal_mcp.storage.sqlite import SqliteRepository

    repository = SqliteRepository(tmp_path / "commands.sqlite3", tmp_path / "output.sqlite3")
    await repository.initialize()
    record = await repository.create(
        "fixture only; never executed", cmd_hash="cd23ef45", status="failed"
    )
    raw = public_error("internal_error", outcome="committed").as_dict()
    raw["cmd_hash"] = record.cmd_hash
    raw["status"] = "failed"
    raw["messages"] = [base._message("notify", text="Ж" * 16384)]
    raw["pending_messages"] = deepcopy(raw["messages"])
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        f"command_{action}",
        {"command": "fixture only; never executed"},
    )
    assert await repository.get(record.cmd_hash) is not None
    assert data["ok"] is False
    assert data["error"]["code"] == "internal_error", data
    assert data["error"]["outcome"] == "committed", data
    assert record.cmd_hash in json.dumps(data), data
    assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES

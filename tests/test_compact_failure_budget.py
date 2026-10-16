"""Author regressions at application, legacy serialization and role wire boundaries."""

from copy import deepcopy

import pytest
import test_compact_response_independent as base

from terminal_mcp.application.base import canonical_application_result
from terminal_mcp.core.read_contract import CALL_TOOL_RESULT_BUDGET_BYTES
from terminal_mcp.mcp.output_contracts import serialized_call_tool_result_size


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code,required",
    [
        ("ack", "coordination_ack_required", "ack"),
        ("alert", "coordination_alert", "reply"),
    ],
)
async def test_real_application_normalization_preserves_long_obligation(
    monkeypatch, mode, code, required
):
    message = base._message(mode, text="\x00🙂" * 8192)
    message["sender"] = "Alpha-7 " + "s" * 80
    raw = {
        "ok": False,
        "code": code,
        "error": code,
        "details": {
            "pending_messages": [message],
            "required_action": required,
            "ack_required_pending": mode == "ack",
            "alert_pending": mode == "alert",
        },
    }
    before = deepcopy(raw)
    normalized = canonical_application_result(raw)
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=normalized),
        "command_run",
        {"command": "fixture never executed"},
    )
    assert data["error"]["code"] == code and "details" not in data["error"]
    notice = data["messages"][0]
    assert notice["id"] == message["message_hash"]
    assert notice["sender"] == message["sender"] and notice["requirements"] == required
    assert message["text"].startswith(notice["text"])
    assert data["warnings"][0]["code"] == "incoming_messages_truncated"
    assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES
    assert raw == before


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "unknown"])
async def test_error_keeps_authoritative_outcome_receipt_and_metadata(monkeypatch, outcome):
    raw = {
        "ok": False,
        "code": "output_item_too_large",
        "error": "output_item_too_large",
        "outcome": outcome,
        "cmd_hash": "accepted-fixture",
        "messages": [base._message("ack", text="\x00" * 16384)],
        "_meta": {"fixture_notice": "retain"},
    }
    normalized = canonical_application_result(raw)
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=normalized),
        "command_recovery",
        {"command": "fixture never executed"},
    )
    assert data["error"]["outcome"] == outcome and data["cmd_hash"] == "accepted-fixture"
    assert data["messages"][0]["requirements"] == "ack"
    assert wire.meta["fixture_notice"] == "retain"
    assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES


@pytest.mark.asyncio
async def test_control_char_preview_fits_wire_budget_and_keeps_all_identifiers(monkeypatch):
    raw = base._run()
    raw["messages"] = [
        base._message("ack", text="\x00" * 16384, msg_id=f"fixture:{i}") for i in range(4)
    ]
    for message in raw["messages"]:
        message["sender"] = "Alpha-7 " + "s" * 80
    data, wire = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        "command_run",
        {"command": "fixture never executed"},
    )
    assert data["ok"] and data["cmd_hash"] == raw["cmd_hash"]
    assert {m["id"] for m in data["messages"]} == {m["message_hash"] for m in raw["messages"]}
    assert all(m["requirements"] == "ack" for m in data["messages"])
    assert data["warnings"]
    assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES

"""Reviewer-owned public negative-response oracle; no live state or shell execution.

Input comes from the actual SessionGate.command_state with an in-memory backend, including
existing bounded coordination repair data. These tests assert the requested
root-only incoming contract on the actual Executor tool boundary.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import test_compact_response_independent as base

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.session_gate import SessionGate


async def _gate_refusal(message, mode, code):
    """Use the real production gate with an isolated in-memory backend."""
    state = {
        "messages": [deepcopy(message)],
        "pending_messages": [deepcopy(message)],
        "ack_required_pending": mode == "ack",
        "alert_pending": mode == "alert",
    }

    async def message_state(**_kwargs):
        return deepcopy(state)

    gate = SessionGate(backend=SimpleNamespace(message_state=message_state))
    _state, raw = await gate.command_state(
        ActorContext(endpoint_role="executor"),
        {
            "logical_agent_id": "review-only-agent",
            "work_session_id": "review-only-session",
            "session_epoch": 1,
            "authority_node_id": "review-only-node",
        },
        "run",
    )
    assert _state is None and raw["code"] == code
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,mode,code,required",
    [
        ("command_run", "ack", "coordination_ack_required", "ack"),
        ("command_run", "alert", "coordination_alert", "reply"),
        ("task_claim", "alert", "coordination_alert", "reply"),
    ],
)
async def test_rejected_operation_incoming_is_root_and_actionable(
    monkeypatch, mode, code, required, tool_name
):
    message = base._message(mode, text="Review this incoming obligation")
    raw = await _gate_refusal(message, mode, code)
    original = deepcopy(raw)
    if tool_name == "command_run":
        server = base._server(monkeypatch, "executor", cmd=raw)
        arguments = {"command": "fixture only; never executed"}
    else:
        server = base._server(monkeypatch, "executor", task=raw)
        arguments = {
            "action": "claim",
            "namespace": "acceptance",
            "task_id": "ONE",
            "claim_intent": "independent negative-response review",
        }
    data, _ = await base._call(server, tool_name, arguments)
    assert data["ok"] is False
    assert data["error"]["code"] == code
    assert data["error"]["outcome"] == "not_committed"
    assert data["error"]["retry"]
    assert raw == original, "Serialization changed a message obligation"
    assert data.get("messages") == [
        {
            "id": message["message_hash"],
            "sender": message["sender"],
            "text": message["text"],
            "requirements": required,
        }
    ], data
    repair = data["error"].get("details", {})
    assert "pending_messages" not in repair, data
    assert not {"message_hash", "text", "required_action"} & data.keys(), data
    assert "ack_required_pending" not in repair, data
    assert "alert_pending" not in repair, data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,code,required,text,sender",
    [
        ("ack", "coordination_ack_required", "ack", "x" * 500, "Alpha-7 " + "s" * 56),
        ("alert", "coordination_alert", "reply", "a" * 501, base.PUBLIC_NAME),
        ("ack", "coordination_ack_required", "ack", "Ж" * 16384, base.PUBLIC_NAME),
        (
            "alert",
            "coordination_alert",
            "reply",
            "A short actionable notice",
            "Alpha-7 " + "s" * 70,
        ),
    ],
    ids=["at-old-limits-control", "501-char-message", "max-unicode-message", "long-slot-suffix"],
)
async def test_legal_message_error_retains_code_and_actionable_id(
    monkeypatch, mode, code, required, text, sender
):
    """Legal message sizes retain the coordination error, ID, and required action."""
    message = base._message(mode, text=text)
    message["sender"] = sender
    raw = await _gate_refusal(message, mode, code)
    original = deepcopy(raw)
    data, _ = await base._call(
        base._server(monkeypatch, "executor", cmd=raw),
        "command_run",
        {"command": "fixture only; never executed"},
    )
    assert raw == original
    assert data["ok"] is False
    assert data["error"]["code"] == code, data
    assert data["error"]["outcome"] == "not_committed", data
    assert data["error"]["retry"], data
    assert any(
        item["id"] == message["message_hash"]
        and item.get("requirements") == required
        and item["sender"] == sender
        for item in data.get("messages", [])
    ), data

"""Local message application decisions with storage and Mesh ports replaced."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from terminal_mcp.application import access_mesh_messages as application
from terminal_mcp.application.actor import ActorContext
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore

NOW = datetime(2026, 10, 8, 10, tzinfo=UTC)


@pytest.fixture
def messages(monkeypatch):
    slot = SimpleNamespace(logical_agent_id="receiver", issuer_id="node-a", slot_id="slot")
    slots = SimpleNamespace(
        local_node_id="node-a",
        local_slots=lambda **kwargs: [slot],
        observed_identity=lambda *args, **kwargs: {
            "logical_agent_id": "receiver",
            "public_name": "Receiver",
            "work_session_id": "receiver-session",
            "session_lifecycle": {"state": "active"},
        },
        human_name=lambda *args, **kwargs: "Receiver",
        recipient_holds=SimpleNamespace(local_recipients=lambda: []),
    )
    mesh = SimpleNamespace(
        store=slots,
        clock=lambda: NOW,
        resolve=AsyncMock(return_value={"logical_agent_id": "sender", "public_name": "Sender"}),
    )
    store = Mock(spec=AccessMeshMessageStore)
    store.wire.return_value = None
    store.activity.return_value = None

    def receipt(message_hash):
        return {
            "ok": True,
            "message_hash": message_hash,
            "state": "delivered",
            "outcome": "committed",
        }

    store.accept.side_effect = lambda wire, *args, **kwargs: receipt(wire["message_hash"])
    store.receipt.side_effect = receipt
    monkeypatch.setattr(application, "AccessMeshMessageStore", lambda *args, **kwargs: store)
    replication = SimpleNamespace(
        peers=[SimpleNamespace(instance_id="node-b")],
        request=AsyncMock(side_effect=AssertionError("local messages must not request peers")),
    )
    return application.AccessMeshMessaging(mesh, SimpleNamespace(path="unused"), replication)


@pytest.mark.asyncio
async def test_fleet_scope_is_rejected_before_authorization_or_storage(messages):
    messages.mesh.resolve.side_effect = AssertionError("scope rejection must precede resolution")
    result = await messages.message(ActorContext(), text="message", scope="fleet")
    assert result == {
        "ok": False,
        "code": "feature_disabled",
        "error": "feature_disabled",
        "outcome": "not_committed",
    }
    messages.mesh.resolve.assert_not_awaited()
    assert messages.store.mock_calls == []
    messages.replication.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliberate_sends_with_identical_request_id_and_text_have_distinct_ids(messages):
    actor = ActorContext(principal_id="sender", request_id="same-request")
    first = await messages.message(actor, text="same text")
    second = await messages.message(actor, text="same text")
    assert first["ok"] and second["ok"]
    assert first["message_hash"] != second["message_hash"]
    assert messages.store.accept.call_count == 2
    wires = [call.args[0] for call in messages.store.accept.call_args_list]
    assert [wire["text"] for wire in wires] == ["same text", "same text"]
    assert [wire["message_hash"] for wire in wires] == [
        first["message_hash"],
        second["message_hash"],
    ]


@pytest.mark.asyncio
async def test_local_send_commits_without_requesting_or_scheduling_peers(messages):
    actor = ActorContext(principal_id="sender")
    result = await messages.message(actor, text="local message")
    assert result["ok"] and result["outcome"] == "committed"
    messages.mesh.resolve.assert_awaited_once_with(actor, ManagedOperation.MESSAGE_SEND)
    messages.store.accept.assert_called_once()
    messages.replication.request.assert_not_awaited()

"""Full Unicode reconstruction through real role wire projections and isolated stores."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair
from test_compact_response_independent import _call

from terminal_mcp.core.message_text import message_text_slice
from terminal_mcp.mcp import roles
from terminal_mcp.mcp.role_contracts import MessageInput


def _server(monkeypatch, node, identity="bob"):
    service = SimpleNamespace(persistent=None, access_mesh=node.mesh, access_mesh_messages=node)

    async def resolve(current, _code, operation):
        value = await node.mesh.resolve(current, operation)
        return SimpleNamespace(failure=None, identity=value, actor=current)

    app = SimpleNamespace(
        service=service,
        message=node.message,
        session_gate=SimpleNamespace(resolve=resolve, touch_provider=AsyncMock(return_value=None)),
    )
    monkeypatch.setattr(roles, "get_application", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(
        roles, "_actor", lambda *_args, **_kwargs: actor(identity, "read-fixture", "executor")
    )
    return roles.build_role_mcp(service, "executor")


def _recipient(node, message_id, identity="bob"):
    with node.store.connect() as db:
        return dict(
            db.execute(
                "SELECT * FROM coordination_message_recipients "
                "WHERE message_hash=? AND recipient_agent_id=?",
                (message_id, identity),
            ).fetchone()
        )


@pytest.mark.asyncio
async def test_role_read_returns_requested_unicode_text_slice(mesh_pair, monkeypatch):
    node = mesh_pair.nodes["bacloud"]
    original = "Ж🙂e\u0301漢\n"
    sent = await node.message(
        actor("alice"), text=original, target=_public_name(node.mesh.store.slots["bob"]), mode="ack"
    )
    data, _ = await _call(
        _server(monkeypatch, node),
        "message",
        {"action": "read", "message_hash": sent["message_hash"], "text_offset": 1, "text_limit": 3},
    )
    assert data["ok"]
    item = data["messages"][0]
    assert (item["text"], item["text_offset"], item["text_length"], item["next_text_offset"]) == (
        original[1:4],
        1,
        len(original),
        4,
    )


@pytest.mark.parametrize(
    "args",
    [
        {"action": "ack", "message_hash": "x", "text_offset": 0},
        {"action": "read", "message_hash": "x", "text_limit": 2049},
    ],
)
def test_chunk_request_rejects_action_ambiguity_and_excessive_slice(args):
    with pytest.raises(ValidationError):
        MessageInput.model_validate(args)


def test_exact_text_end_returns_empty_complete_slice():
    assert message_text_slice({"text": "Ж🙂"}, offset=2, limit=2) == {
        "text": "",
        "text_offset": 2,
        "text_length": 2,
    }

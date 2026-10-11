"""Full Unicode reconstruction through real role wire projections and isolated stores."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair
from test_compact_response_independent import _call

from terminal_mcp.core.message_text import message_text_slice
from terminal_mcp.core.read_contract import (
    CALL_TOOL_RESULT_BUDGET_BYTES,
)
from terminal_mcp.mcp import roles
from terminal_mcp.mcp.output_contracts import serialized_call_tool_result_size
from terminal_mcp.mcp.role_contracts import MessageInput


def _server(monkeypatch, node, identity="bob"):
    service = SimpleNamespace(persistent=None, access_mesh=node.mesh, access_mesh_messages=node)

    async def resolve(current, _code, operation):
        value = await node.mesh.resolve(current, operation)
        return SimpleNamespace(failure=None, identity=value, actor=current)

    app = SimpleNamespace(
        service=service,
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
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
async def test_16k_unicode_reconstructs_through_role_wire_without_ack(mesh_pair, monkeypatch, mode):
    node = mesh_pair.nodes["bacloud"]
    original = ("Ж🙂e\u0301漢\n" * 3000)[:16384]
    sent = await node.message(
        actor("alice", f"send-{mode}"),
        text=original,
        target=_public_name(node.mesh.store.slots["bob"]),
        mode=mode,
    )
    assert sent["ok"], sent
    identifier = sent["message_hash"]
    server = _server(monkeypatch, node)
    offset, chunks = 0, []
    while True:
        data, wire = await _call(
            server,
            "message",
            {
                "action": "read",
                "message_hash": identifier,
                "text_offset": offset,
            },
        )
        assert data["ok"], data
        assert serialized_call_tool_result_size(wire) <= CALL_TOOL_RESULT_BUDGET_BYTES
        item = data["messages"][0]
        assert item["message_hash"] == identifier
        assert item["text_offset"] == offset and item["text_length"] == len(original)
        assert len(item["text"]) <= 2048
        chunks.append(item["text"])
        if "next_text_offset" not in item:
            break
        offset = item["next_text_offset"]
        assert offset == sum(map(len, chunks))
    assert "".join(chunks) == original
    assert "".join(chunks).encode("utf-8") == original.encode("utf-8")
    receipt = _recipient(node, identifier)
    assert receipt["replied_at"] is None and receipt["reply_message_hash"] is None
    assert (receipt["read_at"] is not None) == (mode == "notify")
    with node.store.connect() as db:
        assert (
            db.execute(
                "SELECT text FROM coordination_messages WHERE message_hash=?", (identifier,)
            ).fetchone()[0]
            == original
        )
    assert not mesh_pair.calls


@pytest.mark.asyncio
async def test_foreign_missing_invalid_offset_and_preflight_have_no_receipt_writes(
    mesh_pair, monkeypatch
):
    node = mesh_pair.nodes["bacloud"]
    sent = await node.message(
        actor("alice", "send-bounds"),
        text="Ж" * 16384,
        target=_public_name(node.mesh.store.slots["bob"]),
        mode="ack",
    )
    identifier = sent["message_hash"]
    before = _recipient(node, identifier)
    server = _server(monkeypatch, node, "carol")
    data, _ = await _call(server, "message", {"action": "read", "message_hash": identifier})
    assert not data["ok"] and data["error"]["code"] == "message_not_found"
    server = _server(monkeypatch, node)
    for message_id, offset, code in (
        (identifier, 16385, "input_validation_failed"),
        ("bacloud:msg:missing", 0, "message_not_found"),
    ):
        data, _ = await _call(
            server, "message", {"action": "read", "message_hash": message_id, "text_offset": offset}
        )
        assert not data["ok"] and data["error"]["code"] == code
    rejected = await node.message(
        actor("bob"),
        read_message_hash=identifier,
        response_preflight=lambda _raw: {"ok": False, "code": "injected_preflight_failure"},
    )
    assert rejected["code"] == "injected_preflight_failure"
    assert _recipient(node, identifier) == before


@pytest.mark.asyncio
async def test_summary_history_cursor_finds_all_ids_then_reads_all_chunks(mesh_pair, monkeypatch):
    node = mesh_pair.nodes["bacloud"]
    originals = {}
    for index in range(7):
        text = str(index) + "🙂" * 4095
        sent = await node.message(
            actor("alice", f"send-many-{index}"),
            text=text,
            target=_public_name(node.mesh.store.slots["bob"]),
            mode="ack",
        )
        assert sent["ok"], sent
        originals[sent["message_hash"]] = text
    server = _server(monkeypatch, node)
    identifiers, cursor = [], None
    while True:
        args = {"action": "history", "limit": 2, "detail": "summary"}
        if cursor:
            args["cursor"] = cursor
        data, _ = await _call(server, "message", args)
        identifiers.extend(item["message_hash"] for item in data["messages"])
        cursor = data.get("next_cursor")
        if not cursor:
            break
    assert len(identifiers) == len(set(identifiers)) == 7
    assert set(identifiers) == set(originals)
    for identifier in identifiers:
        pieces, offset = [], 0
        while True:
            data, _ = await _call(
                server,
                "message",
                {"action": "read", "message_hash": identifier, "text_offset": offset},
            )
            item = data["messages"][0]
            pieces.append(item["text"])
            offset = item.get("next_text_offset")
            if offset is None:
                break
        assert "".join(pieces) == originals[identifier]
        assert _recipient(node, identifier)["read_at"] is None


@pytest.mark.parametrize(
    "args",
    [
        {"action": "ack", "message_hash": "x", "text_offset": 0},
        {"action": "reply", "message_hash": "x", "text": "reply", "text_limit": 1},
        {"action": "read", "text_limit": 1},
        {"action": "read", "message_hash": "x", "text_offset": True},
        {"action": "read", "message_hash": "x", "text_limit": 2049},
        {"action": "read", "message_hash": "x", "text_offset": -1},
        {"action": "read", "message_hash": "x", "cursor": "cursor"},
        {"action": "read", "message_hash": "x", "target": "other"},
    ],
)
def test_chunk_request_rejects_ambiguous_actions_and_coercion(args):
    with pytest.raises(ValidationError):
        MessageInput.model_validate(args)


def test_exact_end_empty_string_and_zero_offset_are_preserved():
    original = {"text": "Ж🙂"}
    before = deepcopy(original)
    assert message_text_slice(original, offset=2, limit=2) == {
        "text": "",
        "text_offset": 2,
        "text_length": 2,
    }
    assert message_text_slice({"text": ""}, offset=0, limit=2) == {
        "text": "",
        "text_offset": 0,
        "text_length": 0,
    }
    assert original == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["notify", "ack", "alert"])
async def test_legacy_explicit_read_id_does_not_turn_into_hash_only_ack(
    tmp_path, monkeypatch, mode
):
    from terminal_mcp.application.actor import ActorContext
    from terminal_mcp.application.messages import MessagingApplication
    from terminal_mcp.core.persistent_backend import PersistentBackend
    from terminal_mcp.mcp import server as legacy_server
    from terminal_mcp.storage.agents import AgentStore
    from terminal_mcp.storage.sqlite import SqliteRepository

    repo = SqliteRepository(tmp_path / "messages.sqlite", tmp_path / "outputs.sqlite")
    await repo.initialize()
    store = AgentStore(repo.path)
    identifier = "local:msg:chunked"
    original = ('\x00🙂Ж\\"' * 4000)[:16384]
    await store.create_message(
        identifier,
        "author",
        "receiver",
        original,
        "2026-10-11T12:00:00Z",
        ["receiver"],
        require_reply=mode != "notify",
        alert=mode == "alert",
        delivery_mode=mode,
    )
    service = SimpleNamespace(
        repo=repo,
        terminal=None,
        task_store=None,
        task_coordinator=None,
        agent_coordinator=SimpleNamespace(store=store),
    )
    backend = PersistentBackend(service, SimpleNamespace())
    service.persistent = backend
    backend._access_get = AsyncMock(return_value={"public_name": "Alpha-1 fixture"})
    identity = {"ok": True, "logical_agent_id": "receiver", "public_name": "Bravo-1 fixture"}

    async def resolve(current, _sender, _code, _operation):
        return SimpleNamespace(failure=None, identity=identity, actor=current)

    async def invoke(resolution, sender, **kwargs):
        return await backend.access_message(
            sender, _resolved_identity=resolution.identity, **kwargs
        )

    async def surface(resolution, sender, **kwargs):
        return await backend.surface_message_page(
            sender, _resolved_identity=resolution.identity, **kwargs
        )

    gate = SimpleNamespace(
        resolve_message_actor=resolve, message=invoke, surface_message_page=surface
    )
    capability = MessagingApplication(service, gate)
    gate.touch_provider = AsyncMock(return_value=None)
    facade = SimpleNamespace(service=service, message=capability.message, session_gate=gate)
    monkeypatch.setattr(legacy_server, "get_application", lambda *_args, **_kwargs: facade)
    monkeypatch.setattr(
        legacy_server, "_mcp_actor", lambda *_args: ActorContext(endpoint_role="legacy")
    )
    mcp = legacy_server.build_mcp(service)
    tool = mcp._tool_manager.get_tool("message")
    offset, pieces = 0, []
    while True:
        result = await tool.run(
            {"read_message_hash": identifier, "text_offset": offset}, convert_result=True
        )
        data = result.structuredContent
        assert data["ok"], data
        assert serialized_call_tool_result_size(result) <= CALL_TOOL_RESULT_BUDGET_BYTES
        item = data["messages"][0]
        pieces.append(item["text"])
        offset = item.get("next_text_offset")
        if offset is None:
            break
    assert "".join(pieces).encode("utf-8") == original.encode("utf-8")
    assert (await store.message_record(identifier))["text"] == original
    receipt = await store.recipient_record(identifier, "receiver")
    assert receipt["replied_at"] is None
    assert (receipt["read_at"] is not None) == (mode == "notify")
    rejected = await tool.run(
        {"read_message_hash": identifier, "message_hash": identifier}, convert_result=True
    )
    assert not rejected.structuredContent["ok"]
    assert await store.recipient_record(identifier, "receiver") == receipt
    if mode == "ack":
        ack = await tool.run({"message_hash": identifier}, convert_result=True)
        assert ack.structuredContent["ok"]
        assert (await store.recipient_record(identifier, "receiver"))["read_at"] is not None

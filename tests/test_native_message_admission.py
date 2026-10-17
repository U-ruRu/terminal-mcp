"""The native session and message commit share one admission boundary."""

import asyncio
import threading
from datetime import timedelta

import pytest
import pytest_asyncio
from test_access_mesh_native_lifecycle import T0, actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.storage.access_mesh_registry_upgrade import complete_upgrade
from terminal_mcp.storage.agents import AgentStore


@pytest_asyncio.fixture
async def messages(fixture):
    f = fixture
    f.store.trusted_issuers = frozenset({f.store.local_node_id})
    f.store.install_registry_scope(mesh_id="mesh-before", authority_id=f.store.local_node_id)
    complete_upgrade(f.store)
    f.messages = AccessMeshMessaging(f.app, AgentStore(f.store.path))
    f.app.messages = f.messages
    f.members = {}
    for name, code in (("sender", "1234"), ("receiver", "2345")):
        caller = actor("coordinator", name)
        grant = await f.app.issue(
            actor("access", name),
            kind="persistent",
            code=code,
            policy=SlotPolicy(30, 5, False, 10, 5),
        )
        await f.app.attach(caller, session_number=code)
        f.members[name] = {"actor": caller, "grant": grant}
    return f


async def end_member(f, name):
    grant = f.members[name]["grant"]
    slot = f.store.registry_slot(grant["slot_id"])
    await f.app.change(
        actor("access", name),
        slot_id=slot.slot_id,
        kind="SessionEnded",
        expected_revision=slot.revision,
        number_end=f.store.number_cycle(slot, f.clock[0]),
    )


async def incoming(f):
    result = await f.messages.message(
        f.members["receiver"]["actor"],
        text="Please acknowledge or reply.",
        target=f.members["sender"]["grant"]["public_name"],
        mode="ack",
    )
    assert result["ok"], result
    return result["message_hash"]


def pause_write(monkeypatch, f, *, acknowledge=False):
    """Stop the selected user mutation after admission, before BEGIN IMMEDIATE."""
    reached, resume = asyncio.Event(), threading.Event()
    attempted = {}
    loop = asyncio.get_running_loop()
    method = "acknowledge" if acknowledge else "accept"
    original = getattr(f.messages.store, method)
    sender = f.members["sender"]["grant"]["logical_agent_id"]

    def paused(*args, **kwargs):
        if acknowledge or args[0]["sender_id"] == sender:
            attempted["message_hash"] = args[0] if acknowledge else args[0]["message_hash"]
            loop.call_soon_threadsafe(reached.set)
            if not resume.wait(5):
                raise TimeoutError("message commit barrier was not released")
        return original(*args, **kwargs)

    monkeypatch.setattr(f.messages.store, method, paused)
    return reached, resume, attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "reply", "ack"])
async def test_message_commit_rechecks_session_after_end(messages, monkeypatch, operation):
    f = messages
    parent = await incoming(f) if operation != "send" else None
    args = {"message_hash": parent} if parent else {
        "target": f.members["receiver"]["grant"]["public_name"]
    }
    if operation != "ack":
        args["text"] = "Must not commit after end."
    reached, resume, attempted = pause_write(monkeypatch, f, acknowledge=operation == "ack")
    pending = asyncio.create_task(f.messages.message(f.members["sender"]["actor"], **args))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        f.clock[0] += timedelta(seconds=1)
        await end_member(f, "sender")
    finally:
        resume.set()
        result = await asyncio.wait_for(pending, 3)
    assert result["code"] == "session_not_active", result
    assert result["outcome"] == "not_committed"
    if operation == "ack":
        recipient = f.messages.store.recipient(
            parent, f.members["sender"]["grant"]["logical_agent_id"]
        )
        assert recipient["read_at"] is None and recipient["replied_at"] is None
    else:
        assert f.messages.store.wire(attempted["message_hash"]) is None


async def expiry_alert(f):
    grant = f.members["sender"]["grant"]
    slot = f.store.registry_slot(grant["slot_id"])
    cycle = f.store.merged_cycle(slot, f.clock[0])
    f.clock[0] = T0 + timedelta(seconds=31)
    await f.app.tick()
    return f.messages._session_alert_hash(f.store.local_node_id, slot, cycle["started_at"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "expiry_ack"])
async def test_message_commit_keeps_original_mesh_after_move(messages, monkeypatch, operation):
    f = messages
    if operation == "expiry_ack":
        args = {"message_hash": await expiry_alert(f)}
    else:
        args = {
            "text": "Must not enter the next Mesh.",
            "target": f.members["receiver"]["grant"]["public_name"],
        }
    reached, resume, attempted = pause_write(
        monkeypatch, f, acknowledge=operation == "expiry_ack"
    )
    pending = asyncio.create_task(f.messages.message(f.members["sender"]["actor"], **args))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        if operation == "send":
            f.clock[0] += timedelta(seconds=1)
            for name in f.members:
                await end_member(f, name)
        f.store.begin_topology_transition("move-message-race")
        f.store.finish_topology_transition(
            "move-message-race",
            namespace_id="mesh-after",
            mesh_id="mesh-after",
            authority_id=f.store.local_node_id,
        )
    finally:
        resume.set()
        result = await asyncio.wait_for(pending, 3)
    assert result["code"] == "mesh_namespace_mismatch", result
    assert result["outcome"] == "not_committed"
    assert f.messages.store.wire(attempted["message_hash"]) is None


@pytest.mark.asyncio
async def test_system_expiry_alert_remains_acknowledgeable_after_end(messages):
    f = messages
    message_hash = await expiry_alert(f)
    result = await f.messages.message(f.members["sender"]["actor"], message_hash=message_hash)
    assert result["ok"] and result["outcome"] == "committed", result
    assert result["state"] == "replied"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reply", "ack"])
async def test_message_finalization_remains_allowed_while_draining(messages, operation):
    f = messages
    parent = await incoming(f)
    f.clock[0] = T0 + timedelta(seconds=26)
    args = {"message_hash": parent}
    if operation == "reply":
        args["text"] = "Final result before the deadline."
    result = await f.messages.message(f.members["sender"]["actor"], **args)
    assert result["ok"] and result["outcome"] == "committed", result
    recipient = f.messages.store.recipient(
        parent, f.members["sender"]["grant"]["logical_agent_id"]
    )
    assert recipient["read_at"] is not None
    assert (recipient["replied_at"] is not None) is (operation == "reply")

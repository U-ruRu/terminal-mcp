"""Ordinary, isolated human-name presentation and immutable attribution."""

import json
import sqlite3
from datetime import timedelta

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as lifecycle_fixture

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.access_mesh_grants import AccessMeshError, AccessSlotEvent, SlotPolicy
from terminal_mcp.core.human_names import full_agent_name, number_agent_name
from terminal_mcp.core.orchestration import utc_now
from terminal_mcp.mcp.access_contracts import MeshAgentView
from terminal_mcp.mcp.event_store import MCPEventStore
from terminal_mcp.mcp.message_binding import MessageSubscriber
from terminal_mcp.storage.access_mesh import public_name
from terminal_mcp.storage.access_mesh_messages import MeshMessagingError
from terminal_mcp.storage.agents import AgentStore

human_fixture = lifecycle_fixture


@pytest.mark.parametrize(
    "number,expected",
    [
        ("0000", "Alpha-1"),
        ("0025", "Zulu-1"),
        ("0026", "Alpha-2"),
        ("0123", "Tango-5"),
        ("9999", "Papa-385"),
    ],
)
def test_established_alphabet_and_cycle_with_leading_zeros(number, expected):
    assert number_agent_name(number) == expected


def test_unique_number_names_and_optional_label():
    names = {number_agent_name(f"{number:04d}") for number in range(10000)}
    assert len(names) == 10000
    assert full_agent_name("Alpha-1") == "Alpha-1"
    assert full_agent_name("Alpha-1", "  ") == "Alpha-1"
    assert full_agent_name("Alpha-1", "Builder") == "Alpha-1 - Builder"


async def make_agent(f, number, conversation, label=None):
    issued = await f.app.issue(
        actor("access", conversation),
        code=number,
        policy=SlotPolicy(1380, 60),
        display_name=label,
    )
    identity = await f.app.attach(
        actor("executor", conversation), issuer_node_id="firstbyte", access_code=number
    )
    await f.app.update_intent(actor("executor", conversation), intent="Ordinary fixture work")
    return issued, identity


@pytest.mark.asyncio
async def test_name_snapshot_wire_and_overview(human_fixture):
    f = human_fixture
    issued, identity = await make_agent(f, "0026", "builder", "Builder")
    expected = "Alpha-2 - Builder"
    assert issued["public_name"] == identity["public_name"] == expected
    slot = f.store.slot(issued["issuer_node_id"], issued["slot_id"])
    assert slot.agent_name == "Alpha-2" and slot.display_suffix == "Builder"
    with sqlite3.connect(f.store.path) as db:
        wire = json.loads(
            db.execute(
                "SELECT event_json FROM access_mesh_event_log WHERE slot_id=?", (issued["slot_id"],)
            ).fetchone()[0]
        )
    assert AccessSlotEvent.from_wire(wire).agent_name == "Alpha-2"
    page = f.store.agent_overview(now=f.clock[0])
    view = MeshAgentView.model_validate(page.agents[0]).model_dump()
    assert view["display_name"] == view["public_name"] == expected
    assert view["last_server"] == "firstbyte"
    assert view["logical_agent_id"] == identity["logical_agent_id"]
    assert public_name(slot.issuer_id, slot.logical_agent_id) != expected


@pytest.mark.asyncio
async def test_short_full_and_internal_alias_route_one_identity(human_fixture):
    f = human_fixture
    _, sender = await make_agent(f, "0001", "sender")
    issued, recipient = await make_agent(f, "0026", "builder", "Builder")
    messaging = AccessMeshMessaging(f.app, AgentStore(f.store.path))
    target_slot = f.store.slot(issued["issuer_node_id"], issued["slot_id"])
    aliases = [
        "Alpha-2",
        "Alpha-2 - Builder",
        public_name(target_slot.issuer_id, target_slot.logical_agent_id),
    ]
    for alias in aliases:
        records = await messaging._targets(
            {"namespace": None, "target": alias, "sender_id": sender["logical_agent_id"]}
        )
        assert [item["logical_agent_id"] for item in records] == [recipient["logical_agent_id"]]
        assert records[0]["public_name"] == "Alpha-2 - Builder"
    public = await messaging.recipients(sender, scope="local", limit=20, cursor=None)
    assert public["recipients"][0]["public_name"] == "Alpha-2 - Builder"
    assert "_name_aliases" not in public["recipients"][0]


@pytest.mark.asyncio
async def test_ambiguous_short_name_never_misroutes(human_fixture):
    f = human_fixture
    _, sender = await make_agent(f, "0001", "sender")
    issued_a, _ = await make_agent(f, "0026", "a", "Builder")
    issued_b, _ = await make_agent(f, "0027", "b", "Reviewer")
    with sqlite3.connect(f.store.path) as db:
        db.execute(
            "UPDATE access_mesh_slot_replicas SET agent_name=? WHERE slot_id=?",
            ("Alpha-2", issued_b["slot_id"]),
        )
    messaging = AccessMeshMessaging(f.app, AgentStore(f.store.path))
    with pytest.raises(MeshMessagingError, match="ambiguous_agent_name"):
        await messaging._targets(
            {"namespace": None, "target": "Alpha-2", "sender_id": sender["logical_agent_id"]}
        )
    full = await messaging._targets(
        {"namespace": None, "target": "Alpha-2 - Builder", "sender_id": sender["logical_agent_id"]}
    )
    assert full[0]["logical_agent_id"] == issued_a["logical_agent_id"]


@pytest.mark.asyncio
async def test_events_use_snapshot_names_after_label_changes(human_fixture):
    f = human_fixture
    events = MCPEventStore(f.store.path, "firstbyte")
    await events.initialize()

    # The fixture clock is intentionally detached from wall time.
    async def note_attach(agent, session, epoch, **kwargs):
        return await events.note_local_attach(agent, session, epoch, now=f.clock[0], **kwargs)

    f.app.attach_notifier = note_attach
    issued, identity = await make_agent(f, "0026", "builder", "Builder")
    with sqlite3.connect(f.store.path) as db:
        attached = db.execute(
            "SELECT data_json FROM mcp_event_records WHERE name='agent.attached'"
        ).fetchone()
        assert json.loads(attached[0]) == {"agent_name": "Alpha-2 - Builder", "server": "firstbyte"}
        db.execute(
            "UPDATE logical_agents SET display_name='Renamed' WHERE logical_agent_id=?",
            (identity["logical_agent_id"],),
        )
    await events.pump(now=f.clock[0])
    await events.pump(now=f.clock[0] + timedelta(seconds=181))
    await f.app.change(
        actor("access", "builder"),
        slot_id=issued["slot_id"],
        kind="SessionEnded",
        expected_revision=1,
    )
    await events.pump(now=f.clock[0])
    with sqlite3.connect(f.store.path) as db:
        rows = db.execute(
            "SELECT name,data_json FROM mcp_event_records "
            "WHERE name IN ('agent.attached','agent.inactive','session.ended')"
        ).fetchall()
    assert {kind for kind, _ in rows} == {"agent.attached", "agent.inactive", "session.ended"}
    assert all(json.loads(raw)["agent_name"] == "Alpha-2 - Builder" for _, raw in rows)
    assert all(json.loads(raw)["server"] == "firstbyte" for _, raw in rows)


@pytest.mark.asyncio
async def test_unavailable_name_has_explicit_error(human_fixture):
    f = human_fixture
    event = AccessSlotEvent(
        "firstbyte",
        "unmapped",
        "unmapped-agent",
        1,
        "unmapped-event",
        "SlotIssued",
        policy=SlotPolicy(1380, 60),
        code_tag=f.store.code_tag("firstbyte", "0300"),
        effective_at=f.clock[0],
    )
    f.store.apply_event(event, authenticated_peer_id="firstbyte", issued_kind="legacy")
    slot = f.store.slot("firstbyte", "unmapped")
    with pytest.raises(AccessMeshError, match="agent_name_unavailable"):
        f.store.human_name(slot)


@pytest.mark.asyncio
async def test_message_event_retains_delivered_names_after_rename(human_fixture):
    f = human_fixture
    f.clock[0] = utc_now()
    events = MCPEventStore(f.store.path, "firstbyte")
    await events.initialize()
    _, sender = await make_agent(f, "0001", "sender", "Sender")
    issued, recipient = await make_agent(f, "0026", "builder", "Builder")
    messaging = AccessMeshMessaging(f.app, AgentStore(f.store.path))

    async def authorized(candidate):
        return True

    await events.save_subscription(
        sub_id="isolated-display-fixture",
        principal="fixture-user",
        credential_id="fixture-credential",
        auth_mode="bearer",
        name="message.received",
        callback="https://receiver.example.test/events",
        secret="fixture-only",
        message_binding=MessageSubscriber(
            recipient["logical_agent_id"],
            recipient["work_session_id"],
            recipient["session_epoch"],
            f.app.connection_key(actor("executor", "builder")),
            issued["issuer_node_id"],
            issued["slot_id"],
            "executor",
        ),
        authorization_checker=authorized,
    )
    f.clock[0] = utc_now()
    result = await messaging._send(
        actor("executor", "sender"),
        sender,
        text="Ordinary fixture message",
        target="Alpha-2",
        mode="notify",
        scope="local",
        require_reply=False,
        alert=False,
        namespace=None,
        task_id=None,
    )
    assert result["ok"] and result["recipient_count"] == 1
    with sqlite3.connect(f.store.path) as db:
        db.execute(
            "UPDATE logical_agents SET display_name='Changed label' WHERE logical_agent_id=?",
            (recipient["logical_agent_id"],),
        )
        db.execute(
            "UPDATE access_mesh_slot_replicas SET display_suffix='Changed label' WHERE slot_id=?",
            (issued["slot_id"],),
        )
    await events.pump(now=f.clock[0])
    with sqlite3.connect(f.store.path) as db:
        row = db.execute(
            "SELECT data_json FROM mcp_event_records WHERE name='message.received'"
        ).fetchone()
    data = json.loads(row[0])
    assert data["sender"] == "Bravo-1 - Sender"
    assert data["recipient"] == "Alpha-2 - Builder"
    assert data["server"] == "firstbyte"


@pytest.mark.asyncio
async def test_assigned_name_survives_end_restart_and_reuse_history(human_fixture):
    f = human_fixture
    issued, identity = await make_agent(f, "0000", "worker", "Worker")
    await f.app.change(
        actor("access", "worker"),
        slot_id=issued["slot_id"],
        kind="SessionEnded",
        expected_revision=1,
    )
    await f.app.change(
        actor("access", "worker"),
        slot_id=issued["slot_id"],
        kind="SessionStarted",
        expected_revision=2,
    )
    restarted = await f.app.attach(
        actor("executor", "worker"), issuer_node_id="firstbyte", access_code="0000"
    )
    assert restarted["logical_agent_id"] == identity["logical_agent_id"]
    assert restarted["public_name"] == "Alpha-1 - Worker"
    assert f.store.numbers.number_for_slot("firstbyte", issued["slot_id"]) == "0000"


def test_production_mcp_serialization_has_one_human_identity(tmp_path):
    from fastapi.testclient import TestClient
    from test_access_mesh_mcp_runtime import call, rpc, settings

    from terminal_mcp.app import create_app

    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        a_number = call(client, "access", "session", {"action": "start"}, conversation="issuer-a")[
            "session_number"
        ]
        b_number = call(client, "access", "session", {"action": "start"}, conversation="issuer-b")[
            "session_number"
        ]
        call(
            client,
            "coordinator",
            "session",
            {"session_number": a_number},
            conversation="coordinator-a",
        )
        call(client, "executor", "session", {"session_number": b_number}, conversation="executor-b")
        result = rpc(
            client,
            "coordinator",
            "tools/call",
            {"name": "agent_observe", "arguments": {}},
            conversation="coordinator-a",
        ).json()["result"]
        body = result["structuredContent"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == body
        names = {item["public_name"] for item in body["agents"]}
        assert names == {number_agent_name(a_number), number_agent_name(b_number)}
        assert all(item["public_name"] == item["display_name"] for item in body["agents"])
        recipients = call(
            client,
            "coordinator",
            "message",
            {"action": "recipients", "scope": "local"},
            conversation="coordinator-a",
        )
        assert recipients["recipients"][0]["public_name"] == number_agent_name(b_number)
        expected = number_agent_name(b_number)
        sent = call(
            client,
            "coordinator",
            "message",
            {"action": "send", "text": "Ordinary serialization fixture", "target": expected},
            conversation="coordinator-a",
        )
        assert sent["ok"] and sent["message"]["delivered_to"] == [expected]
        assert sent["message"]["target"] == expected


@pytest.mark.asyncio
async def test_replica_snapshot_retains_human_metadata_and_stable_id(human_fixture, tmp_path):
    from terminal_mcp.storage.access_mesh import AccessMeshStore
    from terminal_mcp.storage.sqlite import SqliteRepository

    f = human_fixture
    issued, _ = await make_agent(f, "0123", "worker", "Worker")
    path = tmp_path / "replica.db"
    await SqliteRepository(path, tmp_path / "replica-output.db").initialize()
    replica = AccessMeshStore(
        path,
        local_node_id="bacloud",
        trusted_issuers=f.store.trusted_issuers,
        proof_key=b"mesh-native-tests-only-never-production-key",
    )
    snapshot = f.store.snapshot_page(issuer_id="firstbyte")[0]
    assert replica.apply_snapshot(snapshot, authenticated_peer_id="firstbyte") == "applied"
    assert replica.apply_snapshot(snapshot, authenticated_peer_id="firstbyte") == "duplicate"
    slot = replica.slot("firstbyte", issued["slot_id"])
    assert slot.logical_agent_id == issued["logical_agent_id"]
    assert replica.human_name(slot) == "Tango-5 - Worker"

"""Ordinary, isolated human-name presentation and immutable attribution."""

import sqlite3

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as lifecycle_fixture

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.access_mesh_grants import SlotPolicy
from terminal_mcp.core.human_names import full_agent_name, number_agent_name
from terminal_mcp.storage.access_mesh_messages import MeshMessagingError
from terminal_mcp.storage.agents import AgentStore

human_fixture = lifecycle_fixture


@pytest.mark.parametrize(
    "number,expected",
    [
        ("0000", "Alpha-1"),
        ("0025", "Zulu-1"),
        ("0026", "Alpha-2"),
        ("9999", "Papa-385"),
    ],
)
def test_established_alphabet_and_cycle_with_leading_zeros(number, expected):
    assert number_agent_name(number) == expected


def test_display_label_is_appended_to_current_agent_name():
    assert full_agent_name("Alpha-1", "Builder") == "Alpha-1 - Builder"


async def make_agent(f, number, conversation, label=None):
    issued = await f.app.issue(
        actor("access", conversation),
        code=number,
        kind="persistent",
        policy=SlotPolicy(1380, 60),
        display_name=label,
    )
    await f.app.attach(actor("executor", conversation), session_number=number)
    identity = f.store.observed_identity(f.store.registry_slot(issued["slot_id"]), now=f.clock[0])
    await f.app.update_intent(actor("executor", conversation), intent="Ordinary fixture work")
    return issued, identity


@pytest.mark.asyncio
async def test_full_display_name_routes_to_current_agent(human_fixture):
    f = human_fixture
    _, sender = await make_agent(f, "0001", "sender")
    _, recipient = await make_agent(f, "0026", "builder", "Builder")
    messaging = AccessMeshMessaging(f.app, AgentStore(f.store.path))
    records = await messaging._targets(
        {"namespace": None, "target": "Alpha-2 - Builder", "sender_id": sender["logical_agent_id"]}
    )
    assert [item["logical_agent_id"] for item in records] == [recipient["logical_agent_id"]]


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
    slot = replica.slot("firstbyte", issued["slot_id"])
    assert slot.session.logical_agent_id == issued["logical_agent_id"]
    assert replica.human_name(slot) == "Tango-5 - Worker"

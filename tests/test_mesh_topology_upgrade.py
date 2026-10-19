"""One-time topology conversion and idle runtime lifecycle checks."""

import hashlib
import json
import sqlite3
from collections import Counter

import pytest
from test_fleet_control_hardening import PolicyProbe, keypair
from test_fleet_control_storage import POLICY

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.control_plane import ManagedFleetControl
from terminal_mcp.fleet.control_storage import FleetControlError, FleetControlStore
from terminal_mcp.fleet.topology_migration import expected_single_mesh_rows


@pytest.fixture
async def control(tmp_path):
    store = FleetControlStore(
        tmp_path / "control.db",
        fleet_id="old-domain",
        node_id="home",
        control_node_id="home",
    )
    await store.initialize()
    return store


async def old_mesh(store):
    await store.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[{"node_id": "home"}],
        policy=POLICY,
    )
    async with store._connect("test_old_schema") as db:
        await db.execute("UPDATE control_schema SET version=4")
        await db.commit()


async def test_multiple_populated_meshes_require_explicit_migration_without_data_loss(control):
    await old_mesh(control)
    async with control._connect("test_ambiguous_history") as db:
        await db.execute(
            "INSERT INTO managed_nodes(node_id,mesh_id,state,updated_at) "
            "VALUES('foreign','mesh-b','active','retained')"
        )
        await db.commit()
    before = (await control.managed_nodes(), await control.access_policy())

    with pytest.raises(FleetControlError, match="mesh_topology_migration_required") as failure:
        await control.initialize()

    assert {row["mesh_id"] for row in failure.value.blockers} == {"mesh-a", "mesh-b"}
    assert (await control.managed_nodes(), await control.access_policy()) == before
    assert await control.schema_version() == 4


async def test_single_mesh_upgrade_makes_its_existing_primary_a_member(control):
    await old_mesh(control)
    async with control._connect("test_old_external_controller") as db:
        await db.execute("UPDATE managed_nodes SET mesh_id=NULL WHERE node_id='home'")
        await db.execute(
            "INSERT INTO managed_nodes(node_id,mesh_id,state,updated_at) "
            "VALUES('member','mesh-a','active','retained')"
        )
        await db.commit()
    policy = await control.access_policy()

    await control.initialize()

    assert (await control.managed_node("home"))["mesh_id"] == "mesh-a"
    assert (await control.managed_node("member"))["mesh_id"] == "mesh-a"
    assert await control.access_policy() == policy
    assert await control.schema_version() == 6


async def test_empty_old_grouping_does_not_create_a_mesh_on_standalone_upgrade(control):
    await old_mesh(control)
    async with control._connect("test_old_empty_grouping") as db:
        await db.execute("UPDATE managed_nodes SET mesh_id=NULL")
        await db.commit()

    await control.initialize()

    assert (await control.control_state())["managed"] is False
    assert (await control.managed_meshes(include_inactive=True))[0]["adopted"] is False


async def test_new_mesh_cannot_leave_members_under_an_external_primary(control):
    await control.adopt_managed(
        mesh_id="mesh-a",
        display_name="Alpha",
        nodes=[{"node_id": "home"}, {"node_id": "member"}],
        policy=POLICY,
    )

    with pytest.raises(FleetControlError, match="mesh_primary_handoff_required"):
        await control.adopt_managed(
            mesh_id="mesh-b",
            display_name="Beta",
            nodes=[{"node_id": "home"}],
            policy=POLICY,
        )

    assert (await control.managed_node("member"))["mesh_id"] == "mesh-a"
    assert [mesh["mesh_id"] for mesh in await control.managed_meshes()] == ["mesh-a"]


async def test_standalone_has_no_periodic_reconcile_until_a_peer_joins(control):
    private, public = keypair()
    controller = ManagedFleetControl(
        control,
        FleetConfig("home", private, (), 1.0, 1.0),
        PolicyProbe(),
        public_base_url="https://home.example",
    )
    await controller.start()
    assert controller._reconcile_task is None
    await controller.adopt(mesh_id="mesh-a")
    assert controller._reconcile_task is None
    await control.upsert_managed_node(
        node_id="peer",
        mesh_id="mesh-a",
        origin="https://peer.example",
        public_key=public,
        auth_token="test-peer-token",
    )

    try:
        await controller._apply_local(await controller.snapshot())
        assert controller._reconcile_task is not None
    finally:
        await controller.stop()


async def test_aborted_transition_restarts_its_durable_prepare_phase(control):
    request = dict(
        node_id="home",
        operation_id="retry-busy",
        target_mesh_id="mesh-a",
        target_authority_id="home",
        coordinator_id="home",
        payload={"incoming": True},
    )
    await control.prepare_topology_transition(**request)
    await control.complete_topology_transition("home", "retry-busy", "aborted")

    retried = await control.prepare_topology_transition(**request)

    assert retried["phase"] == "prepared"


async def test_preservation_receipt_allows_only_the_exact_topology_conversion(control):
    await old_mesh(control)
    async with control._connect("test_old_primary") as db:
        await db.execute("UPDATE managed_nodes SET mesh_id=NULL WHERE node_id='home'")
        await db.execute(
            "INSERT INTO managed_nodes(node_id,mesh_id,state,updated_at) "
            "VALUES('member','mesh-a','active','retained')"
        )
        await db.commit()
    before = sqlite3.connect(":memory:")
    with sqlite3.connect(control.path) as original:
        original.backup(before)
    await control.initialize()

    with sqlite3.connect(control.path) as after:
        for table in ("control_meta", "managed_nodes", "managed_meshes"):
            columns = [row[1] for row in before.execute(f"PRAGMA table_info({table})")]
            expected = expected_single_mesh_rows(before, after, table, columns)
            actual = Counter(
                hashlib.sha256(
                    json.dumps(
                        list(row),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                for row in after.execute(
                    "SELECT " + ",".join(columns) + " FROM " + table,
                )
            )
            assert actual == expected
        after.execute("UPDATE managed_nodes SET auth_token='unrelated-change' WHERE node_id='home'")
        columns = [row[1] for row in before.execute("PRAGMA table_info(managed_nodes)")]
        allowed = expected_single_mesh_rows(before, after, "managed_nodes", columns)
        changed = Counter(
            hashlib.sha256(
                json.dumps(
                    list(row),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for row in after.execute(
                "SELECT " + ",".join(columns) + " FROM managed_nodes",
            )
        )
        assert changed != allowed
    before.close()

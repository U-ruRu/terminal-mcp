"""Membership changes retire derived caches while retaining node-owned history."""

import sqlite3
from types import SimpleNamespace

import pytest
from test_fleet_control_storage import POLICY

from terminal_mcp.core.persistent_policy import PersistentPolicyController
from terminal_mcp.fleet.control_storage import FleetControlStore
from terminal_mcp.fleet.namespace import finish_namespace_transition
from terminal_mcp.fleet.projection_storage import PROJECTION_CACHE_TABLES, FleetProjectionStore
from terminal_mcp.fleet.source_meta import FleetNodeMetaStore


async def staged_namespace(tmp_path, destination="mesh-y"):
    control = FleetControlStore(
        tmp_path / "control.db", fleet_id="mesh-x", node_id="moving", control_node_id="moving"
    )
    await control.initialize()
    await control.prepare_topology_transition(
        node_id="moving",
        operation_id="move-to-new-mesh",
        target_mesh_id="mesh-y",
        target_authority_id="moving",
        coordinator_id="moving",
        payload={"incoming": True},
    )
    with sqlite3.connect(control.path) as db:
        db.execute("UPDATE control_meta SET fleet_id=?", (destination,))
        db.execute(
            "INSERT INTO control_namespace VALUES(1,'mesh-x',?,'transition-stamp')",
            (destination,),
        )
    projection = FleetProjectionStore(
        tmp_path / "projection.db",
        fleet_id="mesh-x",
        node_id="moving",
        owner_node_id="old-primary",
        role="follower",
    )
    await projection.initialize()
    with sqlite3.connect(projection.path) as db:
        for table in PROJECTION_CACHE_TABLES:
            columns = list(db.execute(f"PRAGMA table_info({table})"))
            data = {
                "source_node_id": "old-peer",
                "payload_json": '{"private":"old-mesh"}',
                "freshness": "fresh",
                "status": "LIVE",
            }
            values = [data.get(row[1], 1 if row[2] == "INTEGER" else "old") for row in columns]
            db.execute(f"INSERT INTO {table} VALUES(" + ",".join("?" for _ in values) + ")", values)
        db.execute("UPDATE projection_meta SET projection_seq=12")
    source = FleetNodeMetaStore(tmp_path / "source.db", fleet_id="mesh-x", node_id="moving")
    await source.initialize()
    await source.observe_journal(42)
    main = tmp_path / "main.db"
    with sqlite3.connect(main) as db:
        db.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,body TEXT)")
        db.execute("INSERT INTO tasks VALUES('task-owned-by-node','retained application data')")
    settings = SimpleNamespace(
        fleet_id="mesh-x",
        database_path=main,
        env_file_path=tmp_path / "config.env",
        effective_fleet_control_path=lambda: control.path,
        effective_fleet_node_id=lambda: "moving",
        effective_fleet_node_meta_path=lambda: source.path,
        effective_fleet_projection_path=lambda: projection.path,
    )
    return settings, control, projection, source


@pytest.mark.parametrize("destination", ["mesh-y", "mesh-x"])
async def test_namespace_commit_erases_only_derived_mesh_rows(tmp_path, destination):
    settings, _, projection, source = await staged_namespace(tmp_path, destination)
    source_before = await source.get()
    main_before = settings.database_path.read_bytes()

    assert finish_namespace_transition(settings) == destination

    with sqlite3.connect(projection.path) as db:
        for table in PROJECTION_CACHE_TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    meta = await projection.meta()
    assert meta["fleet_id"] == destination
    assert meta["projection_epoch"] == 2
    assert meta["projection_seq"] == 0
    assert meta["owner_node_id"] == "old-primary" and meta["role"] == "follower"
    after = await source.get()
    assert after.source_stream_generation == source_before.source_stream_generation
    assert after.served_high_water == source_before.served_high_water == 42
    assert settings.database_path.read_bytes() == main_before
    assert finish_namespace_transition(settings) is None


async def test_projection_reset_receipt_survives_interrupted_config_write(tmp_path, monkeypatch):
    settings, control, projection, _ = await staged_namespace(tmp_path)
    persist = PersistentPolicyController._persist_env_updates

    def interrupted(*_):
        raise OSError("interrupted after projection commit")

    monkeypatch.setattr(PersistentPolicyController, "_persist_env_updates", interrupted)
    with pytest.raises(OSError, match="projection commit"):
        finish_namespace_transition(settings)
    assert await control.pending_namespace() == "mesh-y"
    with sqlite3.connect(projection.path) as db:
        db.execute(
            "INSERT INTO projection_scope_status VALUES('moving','tasks','LIVE','rebuilt','now')"
        )
    monkeypatch.setattr(PersistentPolicyController, "_persist_env_updates", persist)

    assert finish_namespace_transition(settings) == "mesh-y"

    with sqlite3.connect(projection.path) as db:
        assert db.execute("SELECT reason FROM projection_scope_status").fetchone() == ("rebuilt",)
    assert (await projection.meta())["projection_epoch"] == 2
    assert await control.pending_namespace() is None


async def test_same_label_create_and_detach_still_queue_scope_completion(tmp_path):
    control = FleetControlStore(
        tmp_path / "same-label.db",
        fleet_id="local-moving",
        node_id="moving",
        control_node_id="moving",
    )
    await control.initialize()
    await control.adopt_managed(
        mesh_id="local-moving", display_name="Mesh", nodes=[], policy=POLICY
    )
    assert await control.pending_namespace() == "local-moving"
    await control.namespace_completed("local-moving")

    await control.detach_managed_node("moving")
    await control.claim_local_control_authority()

    assert await control.pending_namespace() == "local-moving"

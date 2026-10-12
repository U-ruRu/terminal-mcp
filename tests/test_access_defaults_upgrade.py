"""Native policy upgrades preserve effective defaults and already-open windows."""

import asyncio
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from test_access_mesh_mcp_runtime import settings

from terminal_mcp.app import create_app
from terminal_mcp.application.access_mesh import AccessMeshApplication
from terminal_mcp.core.access_mesh_grants import ACCESS_MESH_SCHEMA_VERSION, SlotPolicy
from terminal_mcp.storage.access_mesh import AccessMeshStore, local_cycle

NATIVE = SlotPolicy(1200, 60, True, 120, 30)
MESH = SlotPolicy(82800, 120, True, 10800, 3600)
T0 = datetime(2030, 1, 1, tzinfo=UTC)


def old_native_database(path, *, six_columns=False, persisted=None):
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=24")
        db.execute("""CREATE TABLE access_mesh_operator_defaults (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
            policy_json TEXT NOT NULL, legacy_enabled INTEGER NOT NULL
        )""")
        if six_columns:
            db.execute(
                "ALTER TABLE access_mesh_operator_defaults "
                "ADD COLUMN inherit_policy INTEGER NOT NULL DEFAULT 1"
            )
            db.execute("ALTER TABLE access_mesh_operator_defaults ADD COLUMN parent_json TEXT")
        if persisted is not None:
            db.execute(
                "INSERT INTO access_mesh_operator_defaults "
                "(singleton,revision,policy_json,legacy_enabled) VALUES(1,7,?,0)",
                (json.dumps(asdict(persisted)),),
            )
        db.execute("""CREATE TABLE access_mesh_slot_replicas (
            issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, logical_agent_id TEXT NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
            event_id TEXT NOT NULL, policy_json TEXT NOT NULL, code_tag TEXT NOT NULL,
            anchor TEXT, deadline_at TEXT, fence_revision INTEGER NOT NULL DEFAULT 1,
            agent_name TEXT, display_suffix TEXT,
            PRIMARY KEY(issuer_id,slot_id)
        ) WITHOUT ROWID""")
        db.execute(
            "INSERT INTO access_mesh_slot_replicas "
            "(issuer_id,slot_id,logical_agent_id,kind,state,revision,event_id,policy_json,"
            "code_tag,anchor,deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "firstbyte",
                "old-slot",
                "old-agent",
                "persistent",
                "active",
                1,
                "old-event",
                json.dumps(asdict(NATIVE)),
                "0" * 64,
                T0.isoformat(),
                (T0 + timedelta(seconds=1200)).isoformat(),
            ),
        )


def store(path, cls=AccessMeshStore, **kwargs):
    return cls(
        path,
        local_node_id="firstbyte",
        trusted_issuers=frozenset({"firstbyte"}),
        proof_key=b"synthetic-upgrade-key-not-production",
        clock=lambda: T0,
        **kwargs,
    )


def application(value):
    return AccessMeshApplication(
        value,
        task_store=None,
        execution_fence=None,
        defaults=NATIVE,
        legacy_enabled=True,
        clock=lambda: T0,
    )


def fleet_state():
    return {
        "managed": True,
        "control_node_id": "firstbyte",
        "nodes": [{"node_id": "firstbyte", "mesh_id": "mesh", "state": "active"}],
        "policy": {
            "revision": 1,
            "duration_seconds": MESH.duration_seconds,
            "rearm_after_seconds": MESH.cooldown_seconds,
            "warning_after_seconds": MESH.duration_seconds - MESH.warning_seconds,
            "alert_after_seconds": MESH.duration_seconds - MESH.draining_seconds,
            "legacy_admission_enabled": True,
        },
    }


@pytest.mark.parametrize("six_columns", [False, True])
def test_absent_native_defaults_are_snapshotted_before_mesh_startup(tmp_path, six_columns):
    path = tmp_path / "runtime.sqlite3"
    old_native_database(path, six_columns=six_columns)
    value = store(path, upgrade_defaults=NATIVE, upgrade_legacy_enabled=True)
    before = value.slot("firstbyte", "old-slot")
    window = local_cycle(before, T0)
    app = application(value)
    asyncio.run(app.apply_fleet_policy(fleet_state()))
    defaults = value.defaults()
    assert defaults["policy"] == asdict(NATIVE)
    assert defaults["legacy_enabled"] is True
    assert defaults["inherit_policy"] is False
    assert defaults["parent"]["policy"] == asdict(MESH)
    assert app.defaults == NATIVE
    assert value.slot("firstbyte", "old-slot") == before
    assert local_cycle(value.slot("firstbyte", "old-slot"), T0) == window


def test_snapshot_survives_later_start_failure_and_changed_configuration(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    old_native_database(path)

    class LaterFailure(AccessMeshStore):
        def _initialize(self):
            super()._initialize()
            raise RuntimeError("synthetic later startup failure")

    with pytest.raises(RuntimeError, match="synthetic later startup"):
        store(path, LaterFailure, upgrade_defaults=NATIVE, upgrade_legacy_enabled=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == ACCESS_MESH_SCHEMA_VERSION
    value = store(path, upgrade_defaults=MESH, upgrade_legacy_enabled=False)
    assert value.defaults()["policy"] == asdict(NATIVE)
    assert value.defaults()["legacy_enabled"] is True
    assert value.defaults()["inherit_policy"] is False


def test_existing_explicit_native_defaults_are_not_overwritten(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    old_native_database(path, persisted=SlotPolicy(7200, 90, True, 300, 15))
    value = store(path, upgrade_defaults=NATIVE, upgrade_legacy_enabled=True)
    assert value.defaults()["revision"] == 7
    assert value.defaults()["policy"]["duration_seconds"] == 7200
    assert value.defaults()["legacy_enabled"] is False
    assert value.defaults()["inherit_policy"] is False


def test_unknown_effective_upgrade_defaults_fail_before_schema_changes(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    old_native_database(path)
    with pytest.raises(RuntimeError, match="requires its configured effective defaults"):
        store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
        assert db.execute("SELECT COUNT(*) FROM access_mesh_operator_defaults").fetchone()[0] == 0
        columns = {row[1] for row in db.execute("PRAGMA table_info(access_mesh_slot_replicas)")}
        assert "code_generation" not in columns
        assert "inherit_policy" not in columns


def test_fresh_install_and_pre_lifespan_restart_keep_mesh_inheritance(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = store(path)
    assert first.defaults() is None
    second = store(path)
    assert second.defaults() is None
    app = application(second)
    asyncio.run(app.apply_fleet_policy(fleet_state()))
    assert second.defaults()["inherit_policy"] is True
    assert app.defaults == MESH


def test_create_app_supplies_actual_native_configuration_to_upgrade(tmp_path):
    config = settings(tmp_path).model_copy(
        update={
            "access_mesh_duration_sec": 1200,
            "access_mesh_cooldown_sec": 60,
            "access_mesh_warning_sec": 120,
            "access_mesh_draining_sec": 30,
            "access_mesh_legacy_enabled": True,
        }
    )
    old_native_database(config.database_path)
    app = create_app(config)
    assert app.state.access_mesh.defaults == NATIVE
    assert app.state.access_mesh.store.defaults()["inherit_policy"] is False
